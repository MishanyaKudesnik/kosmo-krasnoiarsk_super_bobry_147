"""Command line entry points.

    python3 -m canopy.cli serve [--host --port]
    python3 -m canopy.cli run --aoi RU_MORDOVIA_03 --from 2019 --to 2024
    python3 -m canopy.cli run --geojson contour.json --from 2020 --to 2024
    python3 -m canopy.cli readiness --aoi RU_TVER_01 --from 2019 --to 2024
    python3 -m canopy.cli replay <analysis_id>
    python3 -m canopy.cli catalog

The engine is reachable without the web layer on purpose: that is what makes the
calculation reproducible on saved data, and `replay` is the command the report
prints for a verifier to run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from typing import Any, Dict, List, Optional

from .core.config import get_settings
from .core.status import RequestError
from .dal.catalog import get_catalog
from .engine import AnalysisRequest, run_analysis


def _load_geometry(args: argparse.Namespace) -> Dict[str, Any]:
    if args.geojson:
        with open(args.geojson, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data
    if args.aoi:
        path = get_settings().data_path("areas.geojson")
        with open(path, "r", encoding="utf-8") as fh:
            fc = json.load(fh)
        for f in fc.get("features", []):
            if f.get("id") == args.aoi or f.get("properties", {}).get("aoi_id") == args.aoi:
                return f["geometry"]
        raise SystemExit(f"участок {args.aoi} не найден в areas.geojson")
    raise SystemExit("нужен --aoi или --geojson")


def cmd_run(args: argparse.Namespace) -> int:
    payload = {
        "geometry": _load_geometry(args),
        "year_start": args.year_from,
        "year_end": args.year_to,
        "aoi_id": args.aoi,
        "label": args.label or "",
    }
    try:
        request = AnalysisRequest.parse(payload)
    except RequestError as exc:
        print(f"ОТКАЗ [{exc.status.code}] {exc.status.message}")
        return 2

    def progress(stage: str, message: str, fraction: float) -> None:
        if not args.quiet:
            print(f"  [{fraction*100:3.0f}%] {stage}: {message}")

    result = run_analysis(request, progress=progress)
    data = result.as_dict()
    out_dir = os.path.join(get_settings().runs_root(), result.analysis_id)
    os.makedirs(out_dir, exist_ok=True)
    for name, payload_obj in (
        ("request.json", data["request"]),
        ("result.json", data),
        ("manifest.json", data["manifest"]),
    ):
        with open(os.path.join(out_dir, name), "w", encoding="utf-8") as fh:
            json.dump(payload_obj, fh, ensure_ascii=False, indent=1)
    with open(os.path.join(out_dir, "ledger.jsonl"), "w", encoding="utf-8") as fh:
        for node in data["ledger"]:
            fh.write(json.dumps(node, ensure_ascii=False) + "\n")

    _print_summary(data)
    print(f"\nсохранено в {out_dir}")
    return 0


def _print_summary(data: Dict[str, Any]) -> None:
    c = data["carbon"]
    u = data["units"]
    un = data["uncertainty"]
    geo = data["geometry"]
    print("\n" + "=" * 68)
    print(f"analysis_id {data['analysis_id']}   статус {data['status']['code']}")
    print(f"площадь {geo['area_ha']:.4f} га   пиксель "
          f"{geo['analysis_grid']['pixel_area_ha']:.4f} га "
          f"(наивный 1 га завысил бы в {geo['analysis_grid']['naive_1ha_overestimate_factor']:.2f} раза)")
    if c.get("state_start"):
        print(f"запас {c['state_start']['mean_tc_ha']:.3f} -> {c['state_end']['mean_tc_ha']:.3f} т C/га")
        print(f"ΔC = {c['delta_c_tc']:.1f} т C    E = {c['E']['value']:.1f} т CO2-экв.    "
              f"e = {c['e_per_ha_year']['value']:.4f}")
    print(f"покрытие {c.get('coverage_fraction', 0)*100:.4f} %")
    print(f"очагов {data['change'].get('patch_count', 0)}, диффузная доля "
          f"{data['change'].get('diffuse_share', 0)*100:.1f} %")
    for p in data["change"].get("patches", [])[:5]:
        fw = p["final_window"]
        print(f"   {p['patch_id']} {p['area_ha']:7.1f} га  {p['e_tco2e']:10.0f} тCO2e  "
              f"{p['rung']:<2} {fw['start']}…{fw['end']} ({fw['days']} дн, {p.get('narrowing_source')})")
    if un.get("scenarios"):
        print("сценарии неопределённости:")
        for s in un["scenarios"]:
            mark = "*" if s["id"] == un.get("active_scenario") else " "
            print(f"  {mark} {s['id']:<18} H = {s['H']:12.0f}")
    print(f"E_base = {(data['baseline'].get('E_base') or {}).get('value')}")
    print(f"R = {(u.get('R') or {}).get('value')}   H/R = {(u.get('H_over_R') or {}).get('value')}   "
          f"Q = {(u.get('Q') or {}).get('value')}   [{u['status']['code']}]")
    for s in data.get("baseline_scenarios", []):
        print(f"   стресс: {s['label'][:46]:<46} Q = {(s.get('Q') or {}).get('value')}")
    print("=" * 68)


def cmd_readiness(args: argparse.Namespace) -> int:
    from .baseline.table import get_baseline_table
    from .dal.discovery import assess
    from .geo.ellipsoid import multipolygon_area_m2
    from .geo.polygon import normalise_geometry

    geometry = normalise_geometry(_load_geometry(args))
    area_ha = multipolygon_area_m2(geometry) / 10000.0
    years = list(range(args.year_from, args.year_to + 1))
    r = assess(get_catalog(), geometry, years, area_ha, get_baseline_table())
    print(f"площадь {area_ha:.4f} га, годы {years[0]}–{years[-1]}")
    for p in r.products + ([r.baseline] if r.baseline else []):
        print(f"  {p.label:<44} {p.state:<12} покрытие {p.coverage_fraction*100:6.2f} %  "
              f"файлов {p.files:<4} {p.period or ''} {p.note}")
    for b in r.blocked:
        print(f"  БУДЕТ НЕДОСТУПНО: {b['what']} — {b['why']} [{b['code']}]")
    for w in r.warnings:
        print(f"  предупреждение: {w['message']}")
    return 0


def replay_analysis(analysis_id: str) -> Dict[str, Any]:
    """Recompute a saved analysis and compare it field by field."""
    root = os.path.join(get_settings().runs_root(), analysis_id)
    man_path = os.path.join(root, "manifest.json")
    res_path = os.path.join(root, "result.json")
    if not os.path.exists(man_path) or not os.path.exists(res_path):
        return {"ok": False, "error": f"анализ {analysis_id} не найден в runs/"}

    with open(man_path, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)
    with open(res_path, "r", encoding="utf-8") as fh:
        original = json.load(fh)

    # 1. verify the inputs are the same bytes
    file_checks: List[Dict[str, Any]] = []
    settings = get_settings()
    for item in manifest.get("inputs", []):
        found = None
        for base in (settings.data_root(), settings.fixture_root()):
            candidate = os.path.join(base, item["file"])
            if os.path.exists(candidate):
                found = candidate
                break
        if found is None:
            file_checks.append({"file": item["file"], "ok": False, "reason": "файл отсутствует"})
            continue
        h = hashlib.sha256()
        with open(found, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        same = h.hexdigest() == item["sha256"]
        file_checks.append(
            {
                "file": item["file"],
                "ok": same,
                "expected": item["sha256"][:16],
                "actual": h.hexdigest()[:16],
            }
        )

    # 2. recompute
    request = AnalysisRequest.parse(manifest["request"])
    fresh = run_analysis(request).as_dict()

    # 3. compare the values a verifier cares about
    def pick(payload: Dict[str, Any]) -> Dict[str, Any]:
        c, u, un = payload["carbon"], payload["units"], payload["uncertainty"]
        return {
            "area_ha": payload["geometry"]["area_ha"],
            "C_t0": (c.get("state_start") or {}).get("total_tc"),
            "C_t1": (c.get("state_end") or {}).get("total_tc"),
            "delta_c_tc": c.get("delta_c_tc"),
            "E": (c.get("E") or {}).get("value"),
            "e": (c.get("e_per_ha_year") or {}).get("value"),
            "coverage": c.get("coverage_fraction"),
            "patches": payload["change"].get("patch_count"),
            "changed_area_ha": payload["change"].get("changed_area_ha"),
            "sigma_E": un.get("sigma_e"),
            "L": (un.get("L") or {}).get("value"),
            "U": (un.get("U") or {}).get("value"),
            "E_base": (payload["baseline"].get("E_base") or {}).get("value"),
            "R": (u.get("R") or {}).get("value"),
            "H_over_R": (u.get("H_over_R") or {}).get("value"),
            "Q": (u.get("Q") or {}).get("value"),
            "units_status": u["status"]["code"],
        }

    before, after = pick(original), pick(fresh)
    rows = []
    identical = 0
    for key in before:
        a, b = before[key], after[key]
        same = (a == b) or (
            isinstance(a, float) and isinstance(b, float) and abs(a - b) <= 1e-9 * max(1.0, abs(a))
        )
        identical += int(same)
        rows.append({"field": key, "original": a, "replay": b, "identical": same})

    # 4. say whether the code itself is the same code
    # Identical numbers from different code is still a reproduction, but the
    # reader is entitled to know which of the two they are looking at.
    code_before = (manifest.get("code_version") or "").strip()
    code_after = (fresh.get("manifest", {}).get("code_version") or "").strip()

    return {
        "ok": all(r["identical"] for r in rows) and all(f["ok"] for f in file_checks),
        "analysis_id": analysis_id,
        "files_checked": len(file_checks),
        "files_ok": sum(1 for f in file_checks if f["ok"]),
        "file_checks": file_checks,
        "fields_checked": len(rows),
        "fields_identical": identical,
        "comparison": rows,
        "replay_analysis_id": fresh["analysis_id"],
        "code_version_original": code_before,
        "code_version_replay": code_after,
        "same_code": bool(code_before) and code_before == code_after,
    }


def cmd_replay(args: argparse.Namespace) -> int:
    out = replay_analysis(args.analysis_id)
    if not out.get("ok") and "error" in out:
        print(out["error"])
        return 2
    print(f"файлы: {out['files_ok']}/{out['files_checked']} совпали по sha256")
    for row in out["comparison"]:
        flag = "OK " if row["identical"] else "!! "
        print(f"  {flag}{row['field']:<16} {row['original']}  ->  {row['replay']}")
    if not out.get("same_code"):
        print(
            f"\n  версия кода изменилась: {out.get('code_version_original') or '—'}"
            f"  ->  {out.get('code_version_replay') or '—'}"
        )
    verdict = "REPLAY OK" if out["ok"] else "REPLAY MISMATCH"
    print(f"\n{verdict}: {out['fields_identical']}/{out['fields_checked']} значений идентичны")
    return 0 if out["ok"] else 1


def cmd_catalog(args: argparse.Namespace) -> int:
    catalog = get_catalog()
    summary = catalog.summary()
    for product, entry in sorted(summary["products"].items()):
        print(f"{product:<16} {entry['files']:>4} файлов  {entry['origins']}  годы {entry['years']}")
    if summary["errors"]:
        print("\nошибки чтения:")
        for e in summary["errors"][:10]:
            print("  ", e)
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .server import serve

    serve(args.host, args.port)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="canopy", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("serve", help="запустить веб-сервис")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(func=cmd_serve)

    for name, fn, helptext in (
        ("run", cmd_run, "выполнить анализ"),
        ("readiness", cmd_readiness, "проверить доступность данных"),
    ):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--aoi")
        p.add_argument("--geojson")
        p.add_argument("--from", dest="year_from", type=int, required=True)
        p.add_argument("--to", dest="year_to", type=int, required=True)
        p.add_argument("--label", default="")
        p.add_argument("--quiet", action="store_true")
        p.set_defaults(func=fn)

    p = sub.add_parser("replay", help="воспроизвести сохранённый анализ")
    p.add_argument("analysis_id")
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("catalog", help="показать каталог данных")
    p.set_defaults(func=cmd_catalog)

    args = ap.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
