"""The web service.

Standard library only: `http.server` with a thread pool.  The case asks for a
deployed service reachable through a browser and for a calculation that can be
repeated on saved data; it does not ask for a framework, and every dependency
this project avoids is one less thing between a jury and a running site.

Analyses run in a background thread and report progress through the stages the
interface shows, so a request never blocks and the user watches the pipeline
advance instead of a spinner.  Results are written to `runs/<analysis_id>/` and
survive a restart, which is also what `canopy replay` reads.
"""

from __future__ import annotations

import json
import mimetypes
import os
import threading
import traceback
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple

from .baseline.table import get_baseline_table
from .core.config import get_settings, project_root
from .core.status import RequestError, Status
from .dal.catalog import get_catalog, load_sources_table
from .dal.discovery import assess as assess_readiness
from .dal import stac
from .engine import AnalysisRequest, run_analysis
from .evidence.ladder import competence_table
from .geo.ellipsoid import multipolygon_area_m2
from .geo.polygon import normalise_geometry
from .report.builder import build_html_report
from .compat import roles as compat_roles, aois_flat as compat_aois, run_compat, overview as compat_overview, control_area as compat_control_area, forecast as compat_forecast, sentinel2 as compat_sentinel2, export_csv as compat_export_csv

APP_DIR = os.path.join(project_root(), "app")

STAGES = [
    "VALIDATING",
    "SEARCHING DATA",
    "PROCESSING RASTERS",
    "CALCULATING CARBON",
    "DETECTING CHANGES",
    "BUILDING EVIDENCE",
    "CALCULATING UNCERTAINTY",
    "CALCULATING UNITS",
    "READY",
]


class AnalysisStore:
    """In-memory index over `runs/`, with the running jobs' progress."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: Dict[str, Dict[str, Any]] = {}
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="canopy")
        self.root = get_settings().runs_root()
        os.makedirs(self.root, exist_ok=True)
        self._load_existing()

    def _load_existing(self) -> None:
        for name in sorted(os.listdir(self.root)):
            path = os.path.join(self.root, name, "result.json")
            if os.path.exists(path):
                self._jobs[name] = {
                    "analysis_id": name,
                    "state": "done",
                    "stage": "READY",
                    "fraction": 1.0,
                    "message": "Готово",
                    "result_path": path,
                    "result": None,
                }

    # -- lifecycle ---------------------------------------------------------
    def submit(self, payload: Dict[str, Any]) -> str:
        request = AnalysisRequest.parse(payload)
        aid = uuid.uuid4().hex[:16]
        with self._lock:
            self._jobs[aid] = {
                "analysis_id": aid,
                "state": "queued",
                "stage": "VALIDATING",
                "fraction": 0.0,
                "message": "Запрос принят",
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "label": payload.get("label") or "",
                "result": None,
            }
        self._pool.submit(self._run, aid, request)
        return aid

    def _run(self, aid: str, request: AnalysisRequest) -> None:
        def progress(stage: str, message: str, fraction: float) -> None:
            with self._lock:
                job = self._jobs.get(aid)
                if job is not None:
                    job.update(
                        state="running", stage=stage, message=message, fraction=fraction
                    )

        try:
            result = run_analysis(request, progress=progress, analysis_id=aid)
            data = result.as_dict()
            out_dir = os.path.join(self.root, aid)
            os.makedirs(out_dir, exist_ok=True)
            _write_json(os.path.join(out_dir, "request.json"), data["request"])
            _write_json(os.path.join(out_dir, "result.json"), data)
            _write_json(os.path.join(out_dir, "manifest.json"), data["manifest"])
            with open(os.path.join(out_dir, "ledger.jsonl"), "w", encoding="utf-8") as fh:
                for node in data["ledger"]:
                    fh.write(json.dumps(node, ensure_ascii=False) + "\n")
            with self._lock:
                self._jobs[aid].update(
                    state="done",
                    stage="READY",
                    fraction=1.0,
                    message="Готово",
                    result=data,
                    result_path=os.path.join(out_dir, "result.json"),
                )
        except RequestError as exc:
            with self._lock:
                self._jobs[aid].update(
                    state="error", stage="VALIDATING", fraction=1.0,
                    message=exc.status.message, error=exc.status.as_dict(),
                )
        except Exception as exc:  # noqa: BLE001 - surfaced to the user, never hidden
            with self._lock:
                self._jobs[aid].update(
                    state="error", stage="FAILED", fraction=1.0,
                    message=f"Внутренняя ошибка: {exc}",
                    error={"code": "INTERNAL_ERROR", "message": str(exc),
                           "trace": traceback.format_exc(limit=6)},
                )

    # -- reading -----------------------------------------------------------
    def status(self, aid: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            job = self._jobs.get(aid)
            if job is None:
                return None
            out = {k: v for k, v in job.items() if k != "result"}
        return out

    def result(self, aid: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            job = self._jobs.get(aid)
        if job is None:
            return None
        if job.get("result") is not None:
            return job["result"]
        path = job.get("result_path")
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            with self._lock:
                self._jobs[aid]["result"] = data
            return data
        return None

    def list(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            jobs = list(self._jobs.values())
        out = []
        for job in jobs:
            out.append(
                {
                    "analysis_id": job["analysis_id"],
                    "state": job.get("state"),
                    "stage": job.get("stage"),
                    "created_at": job.get("created_at"),
                    "label": job.get("label", ""),
                }
            )
        out.sort(key=lambda j: j.get("created_at") or "", reverse=True)
        return out[:limit]


STORE: Optional[AnalysisStore] = None


def get_store() -> AnalysisStore:
    global STORE
    if STORE is None:
        STORE = AnalysisStore()
    return STORE


def _write_json(path: str, payload: Any) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------------------
# demo scenarios: shortcuts into the normal pipeline, never a replacement
# ---------------------------------------------------------------------------
def demo_scenarios() -> List[Dict[str, Any]]:
    import json as _json

    path = get_settings().data_path("areas.geojson")
    features = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            features = _json.load(fh).get("features", [])
    by_id = {f.get("id"): f for f in features}

    def geom(aoi: str):
        f = by_id.get(aoi)
        return f["geometry"] if f else None

    out = [
        {
            "id": "fire_mordovia",
            "title": "Пожар в Мордовии, 2019–2024",
            "subtitle": "Полная цепочка: очаги, датировка по MODIS, единицы",
            "aoi_id": "RU_MORDOVIA_03",
            "geometry": geom("RU_MORDOVIA_03"),
            "year_start": 2019,
            "year_end": 2024,
            "why": "Показывает сужение окна датировки и влияние базовой линии на вердикт",
        },
        {
            "id": "control_tver",
            "title": "Контрольный участок, Тверская область",
            "subtitle": "Как выглядит результат без нарушений",
            "aoi_id": "RU_TVER_01",
            "geometry": geom("RU_TVER_01"),
            "year_start": 2019,
            "year_end": 2024,
            "why": "Контроль для сравнения и для исследовательской части",
        },
        {
            "id": "cause_unknown_vologda",
            "title": "Вологда: причина не установлена",
            "subtitle": "Честный отказ вместо выдуманной причины",
            "aoi_id": "RU_VOLOGDA_02",
            "geometry": geom("RU_VOLOGDA_02"),
            "year_start": 2019,
            "year_end": 2024,
            "why": "Нет продукта гарей — сервис не называет причину",
        },
        {
            "id": "subarea_transfer",
            "title": "Подучасток внутри Вологды, 2020–2024",
            "subtitle": "Удельная базовая линия родителя к площади запроса",
            "aoi_id": None,
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [
                        [40.701750000000004, 59.43],
                        [40.701750000000004, 59.47],
                        [40.66975, 59.47],
                        [40.66975, 59.43],
                        [40.701750000000004, 59.43],
                    ]
                ],
            },
            "year_start": 2020,
            "year_end": 2024,
            "why": "Проверка CHECK_TRANSFER_01 из sample_requests.geojson",
        },
        {
            # The most important thing a verification service can demonstrate is
            # what it does when it cannot verify. This contour lies outside every
            # product in the catalogue, so the answer is a reasoned refusal.
            "id": "outside_coverage",
            "title": "Контур вне покрытия данными",
            "subtitle": "Как выглядит честный отказ",
            "aoi_id": None,
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [
                        [37.600, 55.700],
                        [37.640, 55.700],
                        [37.640, 55.722],
                        [37.600, 55.722],
                        [37.600, 55.700],
                    ]
                ],
            },
            "year_start": 2019,
            "year_end": 2024,
            "why": "Сервис отвечает кодом и причиной, а не выдуманным числом",
        },
    ]
    return [s for s in out if s["geometry"]]


# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "FOREST-BEAVER"
    protocol_version = "HTTP/1.1"

    # -- plumbing ----------------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:
        if os.environ.get("CANOPY_QUIET"):
            return
        super().log_message(fmt, *args)

    def _send(self, code: int, body: bytes, content_type: str, extra: Optional[Dict[str, str]] = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: Any, code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False, default=_default).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def _error(self, code: int, message: str, extra: Optional[Dict[str, Any]] = None) -> None:
        payload = {"error": {"code": code, "message": message}}
        if extra:
            payload["error"].update(extra)
        self._json(payload, code)

    def _body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise RequestError("INVALID_GEOMETRY", f"Некорректный JSON: {exc}") from exc

    # -- routing -----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        try:
            # New Forest Beaver frontend compatibility API.
            if path == "/aois":
                self._json(compat_aois())
            elif path == "/regions":
                self._json([])
            elif path == "/roles":
                self._json(compat_roles())
            elif path == "/overview":
                self._json(compat_overview())
            elif path.startswith("/report/"):
                aid = path.split("/")[2]
                result = _load_any_result(aid)
                if result is None:
                    self._error(404, "Расчёт не найден")
                else:
                    self._send(200, build_html_report(result.get("raw_engine_result", result)).encode("utf-8"), "text/html; charset=utf-8")
            elif path.startswith("/export/") and path.endswith(".csv"):
                aid = path.split("/")[2][:-4]
                result = _load_any_result(aid)
                if result is None:
                    self._error(404, "Расчёт не найден")
                else:
                    self._send(200, compat_export_csv(_compat_adapt_from_store(result)), "text/csv; charset=utf-8", {"Content-Disposition": f"attachment; filename=forest-beaver-{aid}.csv"})
            elif path.startswith("/export/") and path.endswith(".geojson"):
                aid = path.split("/")[2][:-8]
                result = _load_any_result(aid)
                if result is None:
                    self._error(404, "Расчёт не найден")
                else:
                    self._json(_changes_geojson(result.get("raw_engine_result", result)))
            elif path.startswith("/api/"):
                self._api_get(path, query)
            else:
                self._static(path)
        except RequestError as exc:
            self._error(400, exc.status.message, {"status": exc.status.as_dict()})
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001
            self._error(500, str(exc), {"trace": traceback.format_exc(limit=5)})

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            body = self._body()
            if path == "/analysis":
                data = run_compat(body)
                # Persist the adapted result under a normal analysis id so report/export/audit remain reproducible.
                aid = data.get("meta", {}).get("calculation_id")
                if aid:
                    out_dir = os.path.join(get_store().root, aid)
                    os.makedirs(out_dir, exist_ok=True)
                    _write_json(os.path.join(out_dir, "request.json"), data.get("request", {}))
                    _write_json(os.path.join(out_dir, "result.json"), data)
                self._json(data)
            elif path == "/control-area":
                self._json({"control_area": compat_control_area(body.get("calculation_id", ""))})
            elif path == "/forecast":
                self._json(compat_forecast(body))
            elif path == "/data/sentinel2":
                self._json(compat_sentinel2(body))
            elif path == "/api/analyses":
                aid = get_store().submit(body)
                self._json({"analysis_id": aid, "stages": STAGES}, 202)
            elif path == "/api/readiness":
                self._json(self._readiness(body))
            elif path.startswith("/api/analyses/") and path.endswith("/replay"):
                aid = path.split("/")[3]
                self._json(self._replay(aid))
            else:
                self._error(404, f"Неизвестный маршрут {path}")
        except RequestError as exc:
            self._error(400, exc.status.message, {"status": exc.status.as_dict()})
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001
            self._error(500, str(exc), {"trace": traceback.format_exc(limit=5)})

    # -- GET handlers ------------------------------------------------------
    def _api_get(self, path: str, query: Dict[str, List[str]]) -> None:
        store = get_store()
        if path == "/api/health":
            catalog = get_catalog()
            self._json(
                {
                    "ok": True,
                    "service": "CANOPY",
                    "catalog": catalog.summary(),
                    "live_source": stac.probe(),
                    "stages": STAGES,
                }
            )
        elif path == "/api/aois":
            self._json(self._aois())
        elif path == "/api/sources":
            self._json({"sources": load_sources_table(), "competence": competence_table()})
        elif path == "/api/config":
            settings = get_settings()
            self._json(
                {
                    "limits": settings.limits,
                    "methods": settings.methods,
                    "parameters": {
                        k: {
                            "value": p.value,
                            "unit": p.unit,
                            "kind": p.kind,
                            "source_id": p.source_id,
                            "applicability": p.applicability,
                        }
                        for k, p in settings.parameters.items()
                    },
                    "prices": settings.prices,
                }
            )
        elif path == "/api/demo":
            self._json({"scenarios": demo_scenarios()})
        elif path == "/api/analyses":
            self._json({"analyses": store.list()})
        elif path.startswith("/api/analyses/"):
            parts = [p for p in path.split("/") if p]
            aid = parts[2] if len(parts) > 2 else ""
            tail = parts[3] if len(parts) > 3 else ""
            job = store.status(aid)
            if job is None:
                self._error(404, f"Анализ {aid} не найден")
                return
            if not tail:
                payload = dict(job)
                if job.get("state") == "done":
                    payload["result"] = store.result(aid)
                self._json(payload)
                return
            result = store.result(aid)
            if result is None:
                self._error(409, "Анализ ещё не завершён", {"job": job})
                return
            if tail == "ledger":
                node = (query.get("trace") or [None])[0]
                if node:
                    self._json({"trace": _trace_from_list(result["ledger"], node)})
                else:
                    self._json({"ledger": result["ledger"], "provenance": result["provenance"]})
            elif tail == "manifest":
                self._json(result["manifest"])
            elif tail == "changes":
                self._json(_changes_geojson(result.get("raw_engine_result", result)))
            elif tail == "report":
                fmt = (query.get("format") or ["html"])[0]
                if fmt == "json":
                    self._json(result)
                else:
                    html = build_html_report(result)
                    self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            else:
                self._error(404, f"Неизвестный подресурс {tail}")
        else:
            self._error(404, f"Неизвестный маршрут {path}")

    def _aois(self) -> Dict[str, Any]:
        path = get_settings().data_path("areas.geojson")
        features: List[Dict[str, Any]] = []
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                features = json.load(fh).get("features", [])
        table = get_baseline_table()
        regions: Dict[str, Dict[str, Any]] = {}
        for f in features:
            props = f.get("properties", {})
            region = props.get("region") or "Прочее"
            entry = regions.setdefault(region, {"region": region, "aois": []})
            info = table.areas.get(props.get("aoi_id"))
            entry["aois"].append(
                {
                    "aoi_id": props.get("aoi_id"),
                    "name": props.get("name"),
                    "role": props.get("selection_role"),
                    "area_ha": props.get("area_ha"),
                    "years": [props.get("analysis_start_year"), props.get("analysis_end_year")],
                    "bbox": [
                        props.get("bbox_west"),
                        props.get("bbox_south"),
                        props.get("bbox_east"),
                        props.get("bbox_north"),
                    ],
                    "geometry": f.get("geometry"),
                    "baseline": info.as_dict() if info else None,
                }
            )
        return {
            "regions": sorted(regions.values(), key=lambda r: r["region"]),
            "note": (
                "Эти участки — только удобные заготовки. Сервис принимает любой "
                "допустимый контур: анализ выбирает данные по границам, а не по "
                "идентификатору участка."
            ),
        }

    def _readiness(self, body: Dict[str, Any]) -> Dict[str, Any]:
        geom = body.get("geometry")
        if not geom:
            raise RequestError("INVALID_GEOMETRY", "Не передана геометрия")
        geometry = normalise_geometry(geom)
        y0 = int(body.get("year_start", 2019))
        y1 = int(body.get("year_end", 2024))
        if y1 <= y0:
            raise RequestError("INVALID_PERIOD", "Конечный год должен быть больше начального")
        area_ha = multipolygon_area_m2(geometry) / 10000.0
        limits = get_settings().limits
        warn: List[Dict[str, str]] = []
        if area_ha / 100.0 > float(limits["max_area_km2"]):
            raise RequestError(
                "AOI_TOO_LARGE",
                f"Площадь {area_ha / 100.0:.3f} км² превышает предел {limits['max_area_km2']} км²",
            )
        readiness = assess_readiness(
            get_catalog(), geometry, list(range(y0, y1 + 1)), area_ha,
            get_baseline_table(), probe_live=True,
        )
        payload = readiness.as_dict()
        payload["can_compute_carbon"] = readiness.can_compute_carbon()
        payload["can_compute_units"] = readiness.can_compute_units()
        payload["area_ha"] = area_ha
        payload["area_km2"] = area_ha / 100.0
        payload["warnings"].extend(warn)
        return payload

    def _replay(self, aid: str) -> Dict[str, Any]:
        from .cli import replay_analysis

        return replay_analysis(aid)

    # -- static ------------------------------------------------------------
    def _static(self, path: str) -> None:
        if path in ("/", "", "/index.html"):
            rel = "index.html"
        else:
            rel = path.lstrip("/")
        target = os.path.normpath(os.path.join(APP_DIR, rel))
        if not target.startswith(APP_DIR) or not os.path.isfile(target):
            index = os.path.join(APP_DIR, "index.html")
            if os.path.isfile(index):
                with open(index, "rb") as fh:
                    self._send(200, fh.read(), "text/html; charset=utf-8")
                return
            self._error(404, "Файл не найден")
            return
        ctype, _ = mimetypes.guess_type(target)
        if ctype and ctype.startswith("text/"):
            ctype = f"{ctype}; charset=utf-8"
        with open(target, "rb") as fh:
            self._send(200, fh.read(), ctype or "application/octet-stream")


def _load_any_result(aid: str) -> Optional[Dict[str, Any]]:
    result = get_store().result(aid)
    if result is not None:
        return result
    path = os.path.join(get_store().root, aid, "result.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return None
    return None


def _compat_adapt_from_store(result: Dict[str, Any]) -> Dict[str, Any]:
    if "meta" in result and "stock_difference" in result:
        return result
    from .compat import adapt_result
    return adapt_result(result)


def _default(obj: Any) -> Any:
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    if hasattr(obj, "as_dict"):
        return obj.as_dict()
    if isinstance(obj, float):
        return obj
    return str(obj)


def _trace_from_list(ledger: List[Dict[str, Any]], node_id: str, depth: int = 12) -> Optional[Dict[str, Any]]:
    index = {n["node_id"]: n for n in ledger}
    node = index.get(node_id)
    if node is None:
        return None
    out = dict(node)
    if depth > 0 and node.get("inputs"):
        out["children"] = [
            _trace_from_list(ledger, child, depth - 1) or {"node_id": child, "missing": True}
            for child in node["inputs"].values()
        ]
    return out


def _changes_geojson(result: Dict[str, Any]) -> Dict[str, Any]:
    feats = []
    for p in result.get("change", {}).get("patches", []):
        feats.append(
            {
                "type": "Feature",
                "id": p["patch_id"],
                "geometry": p["geometry"],
                "properties": {k: v for k, v in p.items() if k != "geometry"},
            }
        )
    return {"type": "FeatureCollection", "features": feats}


def serve(host: str = "0.0.0.0", port: int = 8000) -> None:
    get_catalog()
    get_store()
    httpd = ThreadingHTTPServer((host, port), Handler)
    shown = "localhost" if host in ("0.0.0.0", "") else host
    print(f"CANOPY готов:  http://{shown}:{port}")
    print("Ctrl+C для остановки")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nостановлено")
    finally:
        httpd.server_close()
