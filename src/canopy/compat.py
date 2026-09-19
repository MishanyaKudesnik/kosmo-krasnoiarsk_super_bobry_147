"""Compatibility API for the Forest Beaver frontend.

The calculation engine remains the original case engine.  This module only
adapts its stable result schema to the newer Forest Beaver UI schema.
"""
from __future__ import annotations

import csv
import io
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .baseline.table import get_baseline_table
from .core.status import RequestError
from .dal import stac
from .dal.catalog import get_catalog, load_sources_table
from .engine import AnalysisRequest, run_analysis
from .geo.ellipsoid import multipolygon_area_m2

_ROLES = {
    "verifier": {
        "title": "Верификатор",
        "description": "Проверяет заявленный результат проекта по данным, доказательствам и расчётам.",
        "focus": ["EVIDENCE", "AUDIT", "неопределённость", "происхождение данных"],
        "landing_tab": "checklist",
    },
    "investor": {
        "title": "Инвестор",
        "description": "Оценивает результат проекта, качество данных и факторы риска.",
        "focus": ["RESULT", "риски", "потенциальные единицы", "прогноз-сценарий"],
        "landing_tab": "risks",
    },
    "owner": {
        "title": "Владелец проекта",
        "description": "Задаёт территорию и получает картину изменений и подготовки к верификации.",
        "focus": ["MAP", "динамика", "изменения", "рекомендации"],
        "landing_tab": "recommendations",
    },
}

_OVERVIEW_CACHE: Optional[Dict[str, Any]] = None
_OVERVIEW_LOCK = threading.Lock()


def roles() -> Dict[str, Dict[str, Any]]:
    return _ROLES


def _aoi_features() -> List[Dict[str, Any]]:
    from .core.config import get_settings
    import json
    path = get_settings().data_path("areas.geojson")
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh).get("features", [])


def aois_flat() -> List[Dict[str, Any]]:
    table = get_baseline_table()
    out: List[Dict[str, Any]] = []
    for f in _aoi_features():
        p = f.get("properties", {})
        aid = p.get("aoi_id")
        info = table.areas.get(aid)
        out.append({
            "aoi_id": aid,
            "name": p.get("name") or aid,
            "region": p.get("region") or "Прочее",
            "selection_role": p.get("selection_role"),
            "role": p.get("selection_role"),
            "area_ha": p.get("area_ha"),
            "years": [p.get("analysis_start_year"), p.get("analysis_end_year")],
            "bbox": [p.get("bbox_west"), p.get("bbox_south"), p.get("bbox_east"), p.get("bbox_north")],
            "geometry": f.get("geometry"),
            "baseline": info.as_dict() if info else None,
        })
    return out


def _resolve_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    p = dict(payload)
    if not p.get("geometry") and p.get("aoi_id"):
        for a in aois_flat():
            if a["aoi_id"] == p["aoi_id"]:
                p["geometry"] = a["geometry"]
                p.setdefault("label", a["name"])
                break
    if not p.get("geometry"):
        raise RequestError("INVALID_GEOMETRY", "Не передана геометрия запроса")
    return p


def run_compat(payload: Dict[str, Any]) -> Dict[str, Any]:
    p = _resolve_payload(payload)
    req = AnalysisRequest.parse(p)
    result = run_analysis(req)
    adapted = adapt_result(result.as_dict())
    if p.get("region_name"):
        adapted["request"]["region_name"] = p.get("region_name")
    return adapted


def _param(params: Dict[str, Any], key: str, default: float) -> float:
    v = params.get(key, default)
    if isinstance(v, dict):
        v = v.get("value", default)
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _period_from_file(path: str, request: Dict[str, Any]) -> str:
    dates = re.findall(r"20\d{2}(?:\d{2}\d{2})?", path)
    if dates:
        return dates[0]
    return f"{request.get('year_start')}–{request.get('year_end')}"


def _provenance(manifest: Dict[str, Any], request: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = []
    for x in manifest.get("inputs", []):
        path = x.get("file") or ""
        rows.append({
            "relative_path": path,
            "product_version": x.get("version") or x.get("product") or "—",
            "period": _period_from_file(path, request),
            "retrieved_or_created_date": manifest.get("created_at", "")[:10] or "—",
            "sha256": x.get("sha256"),
            "verified": bool(x.get("sha256")),
            "product": x.get("product"),
            "source_id": x.get("source_id"),
            "origin": x.get("origin"),
        })
    return rows


def _geometry_union(patches: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    coords = []
    for p in patches:
        geom = p.get("geometry") or {}
        if geom.get("type") == "MultiPolygon":
            coords.extend(geom.get("coordinates", []))
        elif geom.get("type") == "Polygon":
            coords.append(geom.get("coordinates", []))
    return {"type": "MultiPolygon", "coordinates": coords} if coords else None


def _cause(patches: List[Dict[str, Any]]) -> str:
    losses = [p for p in patches if p.get("kind") == "loss"]
    if not losses:
        return "изменение не выявлено"
    p = max(losses, key=lambda x: abs(float(x.get("e_tco2e") or 0)))
    return p.get("cause") or "причина не установлена"


def _change_detection(d: Dict[str, Any], request: Dict[str, Any]) -> Dict[str, Any]:
    ch = d.get("change") or {}
    patches = ch.get("patches") or []
    losses = [p for p in patches if p.get("kind") == "loss"]
    gains = [p for p in patches if p.get("kind") == "gain"]
    loss_area = sum(float(p.get("area_ha") or 0) for p in losses)
    gain_area = sum(float(p.get("area_ha") or 0) for p in gains)
    main = max(losses, key=lambda x: abs(float(x.get("e_tco2e") or 0)), default=None)
    cause = _cause(patches)
    evidence = []
    date_range = None
    if main:
        fw = main.get("final_window") or {}
        if fw.get("start") and fw.get("end"):
            date_range = [fw["start"], fw["end"]]
        for e in main.get("evidence", []):
            evidence.append({
                "source": e.get("title") or e.get("product") or e.get("source_id") or "Источник",
                "summary": e.get("human") or e.get("detail") or "",
                "confirms": True if main.get("rung", "D").startswith("A") else None,
            })
    geom = _geometry_union(losses)
    cd = {
        "status": "ok" if patches else "no_change",
        "reason": "Изменения выше порога не обнаружены." if not patches else "",
        "analyzed_area_ha": float(d.get("area_ha") or 0),
        "loss_area_ha": loss_area,
        "gain_area_ha": gain_area,
        "pre_period_loss_ha": None,
        "post_period_loss_ha": None,
        "unexplained_area_ha": max(0.0, loss_area - sum(float(p.get("area_ha") or 0) for p in losses if p.get("cause") and p.get("cause") != "причина не установлена")),
        "confirmed_cause": cause,
        "date_range": date_range,
        "source_aoi_id": request.get("aoi_id") or request.get("region_name") or "custom",
        "evidence": evidence,
        "notes": [ch.get("threshold_note")] if ch.get("threshold_note") else [],
        "loss_footprint_geojson": geom,
        "carbon_contribution": None,
        "burn_index": None,
    }
    if main:
        sd = d.get("carbon", {})
        total_delta = float(sd.get("delta_c_tc") or 0)
        contrib = float(main.get("delta_c_tc") or 0)
        cd["carbon_contribution"] = {
            "delta_c_tc": contrib,
            "share_of_total_delta_c": (contrib / total_delta) if total_delta else None,
            "emissions_tco2e": float(main.get("e_tco2e") or 0),
        }
    return cd


def _risk_from_uncertainty(u: Dict[str, Any]) -> Dict[str, Any]:
    layers = {x.get("id"): x for x in (u.get("layers") or [])}
    cats = {}
    for key, title in [
        ("data_quality", "Качество данных"),
        ("model_error", "Ошибка продукта и модели"),
        ("spatial_coverage", "Пространственное покрытие"),
        ("temporal_coverage", "Временное покрытие"),
        ("causal_evidence", "Причинные свидетельства"),
    ]:
        layer = layers.get(key)
        score_good = float(layer.get("score", 0.0)) if layer else 0.0
        risk_score = round(max(0.0, min(100.0, (1.0 - score_good) * 100.0)))
        level = "low" if risk_score < 34 else "medium" if risk_score < 67 else "high"
        cats[key] = {
            "title": title,
            "score": risk_score,
            "level": level,
            "level_label": {"low": "низкий", "medium": "средний", "high": "высокий"}[level],
            "factors": [{"name": d.get("label", "Фактор"), "points": 0, "detail": d.get("value", "")} for d in (layer.get("details", []) if layer else [])[:3]],
        }
    vri = float(u.get("vri") if u.get("vri") is not None else 0.0)
    overall_score = round(max(0.0, min(100.0, (1.0 - vri) * 100.0)))
    overall_level = "low" if overall_score < 34 else "medium" if overall_score < 67 else "high"
    return {
        "overall_score": overall_score,
        "overall_level": overall_level,
        "overall_label": {"low": "низкий", "medium": "средний", "high": "высокий"}[overall_level],
        "categories": cats,
    }


def _checklist(d: Dict[str, Any], cd: Dict[str, Any], prov: List[Dict[str, Any]]) -> Dict[str, Any]:
    cov = float(d.get("carbon", {}).get("coverage_fraction") or 0)
    u = d.get("uncertainty") or {}
    b = d.get("baseline") or {}
    items = [
        {"title": "Геометрия и площадь", "status": "pass", "detail": "Контур прошёл геодезическую проверку и ограничение площади."},
        {"title": "Покрытие ESA CCI Biomass", "status": "pass" if cov >= .999 else "warn", "detail": f"Покрытие {cov * 100:.1f}% территории."},
        {"title": "Целостность входных файлов", "status": "pass" if prov and all(x["verified"] for x in prov) else "warn", "detail": f"Проверено {sum(x['verified'] for x in prov)} из {len(prov)} файлов."},
        {"title": "Причинные свидетельства", "status": "pass" if cd.get("confirmed_cause") not in (None, "причина не установлена", "изменение не выявлено") else "warn", "detail": cd.get("confirmed_cause") or "Причина не установлена."},
        {"title": "Базовая линия", "status": "pass" if b.get("status", {}).get("code") == "OK" else "warn", "detail": b.get("reason") or "Базовая линия доступна."},
        {"title": "Неопределённость", "status": "warn" if float(u.get("H", {}).get("value") or 0) >= abs(float(d.get("units", {}).get("R", {}).get("value") or 0)) else "pass", "detail": u.get("interval_kind_note", "Сценарный диапазон неопределённости.")},
    ]
    counts = {k: sum(1 for x in items if x["status"] == k) for k in ("pass", "warn", "fail", "info")}
    verdict = "fail" if counts["fail"] else "warn" if counts["warn"] else "pass"
    return {"items": items, "counts": counts, "verdict": verdict}


def _recommendations(d: Dict[str, Any], cd: Dict[str, Any]) -> List[Dict[str, Any]]:
    u = d.get("units", {})
    q = u.get("Q", {}).get("value")
    recs = [
        {"severity": "info", "title": "Проверьте EVIDENCE", "text": "Откройте цепочку источников и убедитесь, что причина изменения подтверждается независимыми наблюдениями.", "actions": ["Проверить даты и ограничения источников"], "audience": ["verifier", "investor"]},
        {"severity": "warning" if q in (None, 0) else "positive", "title": "Потенциальные единицы", "text": "Единицы являются сценарием кейса и возникают только при выполнении условий базовой линии и неопределённости.", "actions": ["Посмотреть R и H в AUDIT"], "audience": ["investor", "verifier", "owner"]},
        {"severity": "warning" if cd.get("loss_area_ha", 0) > 0.1 else "positive", "title": "Изменения территории", "text": f"Зафиксированная площадь изменений: {cd.get('loss_area_ha', 0):.1f} га. Сверьте очаги с картой и доказательствами.", "actions": ["Открыть MAP → слой потерь", "Открыть EVIDENCE"], "audience": ["owner", "verifier"]},
    ]
    return recs


def adapt_result(d: Dict[str, Any]) -> Dict[str, Any]:
    # Some legitimate analyses (for example, a contour outside the biomass
    # catalogue) have unavailable quantities represented by None in the raw
    # engine result.  The compatibility layer must preserve that state instead
    # of calling .get() on None and turning a truthful "no data" result into a
    # HTTP 500.
    def as_dict(value: Any) -> Dict[str, Any]:
        return value if isinstance(value, dict) else {}

    request = as_dict(d.get("request"))
    carbon = as_dict(d.get("carbon"))
    u = as_dict(d.get("uncertainty"))
    baseline = as_dict(d.get("baseline"))
    units = as_dict(d.get("units"))
    manifest = as_dict(d.get("manifest"))
    geometry_meta = as_dict(d.get("geometry"))
    area = float((carbon.get("area_requested_ha") or geometry_meta.get("area_ha") or 0))
    cov = float(carbon.get("coverage_fraction") or 0)
    t0 = as_dict(carbon.get("state_start"))
    t1 = as_dict(carbon.get("state_end"))
    # Keep the requested years visible even when biomass values are unavailable.
    t0.setdefault("year", request.get("year_start"))
    t1.setdefault("year", request.get("year_end"))
    params = manifest.get("parameters", {})
    cd = _change_detection(d, request)
    prov = _provenance(manifest, request)
    sources = load_sources_table()
    sd = {
        "delta_c_tc": float(carbon.get("delta_c_tc") or 0),
        "emissions_tco2e": float(as_dict(carbon.get("E")).get("value") or 0),
        "annual_rate_tco2e_ha_yr": float(as_dict(carbon.get("e_per_ha_year")).get("value") or 0),
    }
    baseline_status = baseline.get("status", {}).get("code", "UNKNOWN")
    baseline_out = {
        "status": "ok" if baseline_status == "OK" else "partial" if baseline_status else "missing",
        "covered_fraction": float(baseline.get("covered_area_ha", 0) or 0) / max(float(baseline.get("requested_area_ha", area) or area), 1e-12),
        "unavailable_fraction": 1.0 - (float(baseline.get("covered_area_ha", 0) or 0) / max(float(baseline.get("requested_area_ha", area) or area), 1e-12)),
        "emissions_tco2e": float(as_dict(baseline.get("E_base")).get("value") or 0),
        "parts": [],
        "reason": baseline.get("status", {}).get("message") if baseline_status != "OK" else "",
    }
    for part in baseline.get("parts", []) or []:
        baseline_out["parts"].append({
            **part,
            "fraction": float(part.get("area_ha", 0) or 0) / max(area, 1e-12),
            "emissions_tco2e": float(part.get("e_base_tco2e", 0) or 0),
        })
    pu = {
        "r_tco2e": as_dict(units.get("R")).get("value"),
        "h_tco2e": as_dict(units.get("H")).get("value"),
        "h_over_r": as_dict(units.get("H_over_R")).get("value"),
        "q_units": as_dict(units.get("Q")).get("value"),
        "b_tco2e": as_dict(units.get("B")).get("value"),
        "reason": as_dict(units.get("status")).get("message") or units.get("disclaimer", ""),
        "status": "ok" if as_dict(units.get("status")).get("code") == "OK" else "limited",
        "v_low": (units.get("value_scenarios") or [{}])[0].get("value", 0) if units.get("value_scenarios") else 0,
        "v_base": (units.get("value_scenarios") or [{}, {}])[1].get("value", 0) if len(units.get("value_scenarios") or []) > 1 else 0,
        "v_high": (units.get("value_scenarios") or [{}, {}, {}])[2].get("value", 0) if len(units.get("value_scenarios") or []) > 2 else 0,
    }
    sens = next((x for x in u.get("scenarios", []) if x.get("id") == "independent"), None) or {}
    uncertainty = {
        "l": float(as_dict(u.get("L")).get("value") or 0),
        "u": float(as_dict(u.get("U")).get("value") or 0),
        "sd_e": float(u.get("sigma_e") or 0),
        "h_tco2e": float(as_dict(u.get("H")).get("value") or 0),
        "h_over_r": pu["h_over_r"],
        "r_tco2e": pu["r_tco2e"],
        "q_units": pu["q_units"],
        "status": "ok",
        "method": str(u.get("active_scenario", "scenario")),
        "assumptions": "; ".join(u.get("assumptions", [])),
        "sensitivity_independent": {"l": sens.get("L"), "u": sens.get("U"), "sd_e": sens.get("sigma_e")},
    }
    risk = _risk_from_uncertainty(u)
    checklist = _checklist(d, cd, prov)
    start_stock = t0.get("mean_tc_ha")
    end_stock = t1.get("mean_tc_ha")
    if start_stock is not None and end_stock is not None and float(start_stock) != 0:
        pct = ((float(end_stock) - float(start_stock)) / float(start_stock)) * 100
        headline = (
            f"Запас углерода изменился с {float(start_stock):.1f} до {float(end_stock):.1f} т C/га ({pct:+.1f}%). "
            f"Итог изменения запаса: {sd['emissions_tco2e']:+.0f} т CO₂-экв."
        )
    else:
        pct = None
        status_message = as_dict(carbon.get("status")).get("message") or "Данные о запасе биомассы недоступны для этого контура."
        headline = f"Расчёт запаса углерода недоступен: {status_message}"
    insights = {
        "headline": headline,
        "key_facts": [{"label": "Изменение среднего запаса", "value": "—" if pct is None else f"{pct:+.1f}%"}],
        "risk": risk,
        "checklist": checklist,
        "recommendations": _recommendations(d, cd),
        "disclaimer": "Сценарный расчёт по условиям кейса. Это не сертифицированные углеродные единицы и не подтверждение дополнительности проекта.",
    }
    retro = [
        {"year": x.get("year"), "mean_stock_tc_ha": x.get("mean_tc_ha"), "total_stock_tc": x.get("total_tc")}
        for x in carbon.get("annual_series", [])
    ]
    return {
        "meta": {
            "calculation_id": d.get("analysis_id"),
            "computed_at": d.get("created_at"),
            "params": {
                "cf_agb": _param(params, "CF_AGB", 0.47),
                "co2_per_c": 44 / 12,
                "lk": _param(params, "LK", 0),
                "unc_allowance": _param(params, "UNC_allowance", 0.10),
                "buf": _param(params, "BUF", 0.15),
            },
        },
        "request": request,
        "sel_label": request.get("label") or request.get("aoi_id") or "произвольный контур",
        "area_ha": area,
        "covered_fraction": cov,
        "territory_geometry": request.get("geometry"),
        "stock_t0": {"year": t0.get("year"), "mean_stock_tc_ha": t0.get("mean_tc_ha"), "total_stock_tc": t0.get("total_tc")},
        "stock_t1": {"year": t1.get("year"), "mean_stock_tc_ha": t1.get("mean_tc_ha"), "total_stock_tc": t1.get("total_tc")},
        "stock_difference": sd,
        "change_detection": cd,
        "baseline": baseline_out,
        "potential_units": pu,
        "uncertainty": uncertainty,
        "data_provenance": prov,
        "sources": sources,
        "insights": insights,
        "retrospective_2019_2024": retro,
        "status": d.get("status"),
        "readiness": d.get("readiness"),
        "warnings": d.get("warnings", []),
        "raw_engine_result": d,
    }


def _headline_from_result(r: Dict[str, Any]) -> str:
    return r.get("insights", {}).get("headline", "")


def overview() -> Dict[str, Any]:
    global _OVERVIEW_CACHE
    with _OVERVIEW_LOCK:
        if _OVERVIEW_CACHE is not None:
            return _OVERVIEW_CACHE
        aois = aois_flat()
        results: List[Dict[str, Any]] = []
        def one(a):
            return run_compat({"geometry": a["geometry"], "aoi_id": a["aoi_id"], "label": a["name"], "year_start": 2019, "year_end": 2024})
        with ThreadPoolExecutor(max_workers=min(4, len(aois))) as ex:
            futs = {ex.submit(one, a): a for a in aois}
            for f in as_completed(futs):
                try:
                    results.append(f.result())
                except Exception:
                    pass
        by_id = {r["request"].get("aoi_id"): r for r in results}
        regions = []
        for a in aois:
            r = by_id.get(a["aoi_id"])
            if not r:
                continue
            sd = r["stock_difference"]; cd = r["change_detection"]
            regions.append({
                "aoi_id": a["aoi_id"], "name": a["name"], "region": a["region"], "area_ha": r["area_ha"], "geometry": a["geometry"],
                "risk": r["insights"]["risk"], "series": r["retrospective_2019_2024"],
                "stock_change_pct": r["insights"]["key_facts"][0]["value"],
                "emissions_tco2e": sd["emissions_tco2e"], "loss_area_ha": cd["loss_area_ha"], "cause": cd["confirmed_cause"], "headline": _headline_from_result(r),
            })
        totals = {
            "regions": len(regions),
            "area_ha": sum(float(r["area_ha"]) for r in regions),
            "loss_area_ha": sum(float(r["loss_area_ha"]) for r in regions),
            "net_emissions_tco2e": sum(float(r["emissions_tco2e"]) for r in regions),
            "risk_levels": {k: sum(1 for r in regions if r["risk"]["overall_level"] == k) for k in ("low", "medium", "high")},
            "units": sum(float((by_id[r["aoi_id"]]["potential_units"].get("q_units") or 0)) for r in regions if r["aoi_id"] in by_id),
        }
        _OVERVIEW_CACHE = {"regions": regions, "totals": totals}
        return _OVERVIEW_CACHE


def control_area(calculation_id: str) -> Dict[str, Any]:
    return {"status": "unavailable", "reason": "Сравнение с кольцом окружения не входит в исходный расчётный движок этого архива; основной анализ и его доказательства доступны."}


def forecast(payload: Dict[str, Any]) -> Dict[str, Any]:
    r = run_compat(payload)
    end = int(payload.get("year_end"))
    horizon = int(payload.get("horizon_year"))
    if horizon <= end:
        raise RequestError("INVALID_PERIOD", "Горизонт прогноза должен быть позже конечного года анализа")
    years = horizon - end
    annual = float(r["stock_difference"]["annual_rate_tco2e_ha_yr"]) * float(r["area_ha"])
    e_proj = max(0.0, annual * years)
    base_annual = float(r["baseline"]["emissions_tco2e"]) / max(int(r["request"]["year_end"]) - int(r["request"]["year_start"]), 1)
    e_base = base_annual * years
    H0 = float(r["uncertainty"].get("h_tco2e") or 0)
    H = H0 * math.sqrt(max(years, 1) / max(int(r["request"]["year_end"]) - int(r["request"]["year_start"]), 1))
    buf = float(r["meta"]["params"]["buf"])
    def scenario(mult):
        proj = e_proj * mult
        R = e_base - proj
        q = max(0, math.floor(max(0, R - H) * (1 - buf)))
        return {"e_proj_tco2e": proj, "e_base_tco2e": e_base, "r_tco2e": R, "h_over_r": (H / R if R > 0 else None), "q_units": q, "v_low": q * 500, "v_base": q * 1500, "v_high": q * 4000}
    return {
        "status": "scenario",
        "observed_annual_rate_tc_ha_yr": float(r["stock_difference"]["annual_rate_tco2e_ha_yr"]) / (44 / 12 * 0.47),
        "year_start": end, "horizon_year": horizon,
        "scenarios": [{"title": "Продолжение наблюдаемого темпа", "at_horizon": {"correlated": scenario(1.0), "independent": scenario(1.0)}}],
        "risks": ["Сценарий линейно продолжает наблюдаемый удельный темп; это не прогноз поведения леса.", "Базовая линия и неопределённость экстраполируются за пределы наблюдаемого периода."],
        "uncertainty_assumption": "H масштабируется как sqrt(длины горизонта / длины наблюдаемого периода) — сценарное допущение.",
        "disclaimer": "Прогноз является демонстрационным сценарием, а не предсказанием рынка или сертифицированным объёмом единиц.",
    }


def sentinel2(payload: Dict[str, Any]) -> Dict[str, Any]:
    p = _resolve_payload(payload)
    from .geo.polygon import multipolygon_bbox, normalise_geometry
    geom = normalise_geometry(p["geometry"])
    bbox = multipolygon_bbox(geom)
    y0, y1 = int(p.get("year_start", 2019)), int(p.get("year_end", 2024))
    res = stac.search_sentinel2(bbox, f"{y0}-01-01", f"{y1}-12-31", max_cloud=80, limit=40, timeout_s=4)
    if not res.status.ok:
        return {"status": "unavailable", "reason": res.error or res.status.message, "source": "Earth Search STAC"}
    local_ids = set()
    for a in get_catalog().assets:
        if a.product == "S2_REFLECTANCE" and a.scene_id:
            local_ids.add(a.scene_id)
    scenes = []
    for x in res.items:
        d = x.as_dict()
        d["in_local_dataset"] = x.item_id in local_ids
        scenes.append(d)
    return {"status": "live", "source": "Earth Search STAC", "scenes": scenes}


def export_csv(result: Dict[str, Any]) -> bytes:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["metric", "value", "unit"])
    w.writerow(["area", result.get("area_ha"), "ha"])
    w.writerow(["stock_start", result.get("stock_t0", {}).get("mean_stock_tc_ha"), "t C/ha"])
    w.writerow(["stock_end", result.get("stock_t1", {}).get("mean_stock_tc_ha"), "t C/ha"])
    w.writerow(["delta_carbon", result.get("stock_difference", {}).get("delta_c_tc"), "t C"])
    w.writerow(["emissions", result.get("stock_difference", {}).get("emissions_tco2e"), "t CO2e"])
    w.writerow(["loss_area", result.get("change_detection", {}).get("loss_area_ha"), "ha"])
    w.writerow(["potential_units", result.get("potential_units", {}).get("q_units"), "units"])
    return out.getvalue().encode("utf-8-sig")
