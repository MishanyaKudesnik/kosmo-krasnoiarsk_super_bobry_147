"""The analysis engine: one request in, one traceable result out.

`run_analysis` is the only entry point.  It takes a geometry, two years and a
configuration, and returns the whole result with its provenance.  It knows
nothing about HTTP, nothing about roles and nothing about the four areas shipped
with the case: a contour is matched to data by its bounds, so a polygon drawn by
hand on the map goes down exactly the same path.

Order of the steps, which is also the order the interface shows them in:

    validate -> discover -> weight pixels -> read biomass -> carbon
             -> detect change -> date it -> gather evidence -> uncertainty
             -> baseline -> potential units -> ledger
"""

from __future__ import annotations

import hashlib
import math
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from .baseline.table import BaselineTable, get_baseline_table
from .carbon.stock import POOL, SIGN_CONVENTION, compute_carbon
from .change import detect as chg
from .core.config import Settings, get_settings
from .core.status import (
    AOI_TOO_LARGE,
    AOI_TOO_SMALL,
    INVALID_GEOMETRY,
    INVALID_PERIOD,
    NO_BIOMASS_DATA,
    OK,
    Quantity,
    RequestError,
    Status,
)
from .dal.catalog import CCI_AGB, CCI_CHANGE, GFC, MODIS_BURN, Catalog, get_catalog
from .dal.discovery import assess as assess_readiness
from .evidence.ladder import (
    Evidence,
    competence_table,
    evaluate_ladder,
    may_support,
    modis_area_guard,
)
from .evidence.ledger import Ledger, LedgerNode, provenance_line
from .geo.ellipsoid import meters_per_degree, multipolygon_area_m2
from .geo.grid import (
    RasterGrid,
    aggregate_to_grid,
    footprint_in_source,
    pixel_weights,
    precompute_footprints,
)
from .geo.polygon import MultiPolygon, multipolygon_bbox, normalise_geometry
from .quality import scenes as sq
from .rio.tiff import GeoTiff
from .uncertainty import propagate as unc
from .units.potential import compute_units

CCI_STEP = 1.0 / 1125.0


# ---------------------------------------------------------------------------
@dataclass
class AnalysisRequest:
    geometry: MultiPolygon
    year_start: int
    year_end: int
    label: str = ""
    aoi_id: Optional[str] = None
    baseline_variant: str = "case"
    raw_geometry: Optional[dict] = None

    @classmethod
    def parse(cls, payload: dict) -> "AnalysisRequest":
        settings = get_settings()
        limits = settings.limits
        geom_raw = payload.get("geometry")
        if not geom_raw:
            raise RequestError(INVALID_GEOMETRY, "Не передана геометрия запроса")
        try:
            geometry = normalise_geometry(geom_raw)
        except ValueError as exc:
            raise RequestError(INVALID_GEOMETRY, str(exc)) from exc

        vertices = sum(len(r) for poly in geometry for r in poly)
        if vertices > int(limits.get("max_vertices", 2000)):
            raise RequestError(
                INVALID_GEOMETRY,
                f"Слишком много вершин: {vertices} при пределе {limits['max_vertices']}",
            )

        try:
            y0 = int(payload.get("year_start"))
            y1 = int(payload.get("year_end"))
        except (TypeError, ValueError) as exc:
            raise RequestError(INVALID_PERIOD, "Годы должны быть целыми числами") from exc
        if y1 <= y0:
            raise RequestError(
                INVALID_PERIOD,
                f"Конечный год ({y1}) должен быть больше начального ({y0})",
            )

        area_m2 = multipolygon_area_m2(geometry)
        if area_m2 <= 0:
            raise RequestError(INVALID_GEOMETRY, "Площадь контура равна нулю")
        area_km2 = area_m2 / 1e6
        if area_km2 > float(limits["max_area_km2"]) + 1e-9:
            raise RequestError(
                AOI_TOO_LARGE,
                f"Площадь контура {area_km2:.3f} км² превышает предел "
                f"{limits['max_area_km2']} км²",
                area_km2=area_km2,
            )
        if area_m2 / 10000.0 < float(limits.get("min_area_ha", 0.1)):
            raise RequestError(AOI_TOO_SMALL, f"Площадь {area_m2 / 10000.0:.4f} га слишком мала")

        return cls(
            geometry=geometry,
            year_start=y0,
            year_end=y1,
            label=str(payload.get("label") or ""),
            aoi_id=payload.get("aoi_id"),
            baseline_variant=str(payload.get("baseline_variant") or "case"),
            raw_geometry=geom_raw,
        )


# ---------------------------------------------------------------------------
def _aligned_grid(bbox: Tuple[float, float, float, float], step: float = CCI_STEP) -> RasterGrid:
    """A window of the global biomass grid that contains the request."""
    west, south, east, north = bbox
    c0 = math.floor((west + 180.0) / step)
    c1 = math.ceil((east + 180.0) / step)
    r0 = math.floor((90.0 - north) / step)
    r1 = math.ceil((90.0 - south) / step)
    return RasterGrid(
        origin_x=-180.0 + c0 * step,
        origin_y=90.0 - r0 * step,
        dx=step,
        dy=-step,
        width=max(1, c1 - c0),
        height=max(1, r1 - r0),
        crs=4326,
    )


def _paste(target: RasterGrid, assets, band: int, dtype=np.float64) -> Tuple[np.ndarray, np.ndarray]:
    """Read a band of every asset into one array on the target grid."""
    out = np.zeros((target.height, target.width), dtype=dtype)
    have = np.zeros((target.height, target.width), dtype=bool)
    for asset in assets:
        g = asset.grid
        if abs(g.dx - target.dx) > 1e-12 or abs(g.dy - target.dy) > 1e-12:
            continue
        col_off = int(round((g.origin_x - target.origin_x) / target.dx))
        row_off = int(round((g.origin_y - target.origin_y) / target.dy))
        r0 = max(0, row_off)
        c0 = max(0, col_off)
        r1 = min(target.height, row_off + g.height)
        c1 = min(target.width, col_off + g.width)
        if r1 <= r0 or c1 <= c0:
            continue
        with GeoTiff(asset.path) as t:
            block = t.read(band=band, window=(r0 - row_off, c0 - col_off, r1 - r0, c1 - c0))
        out[r0:r1, c0:c1] = np.where(have[r0:r1, c0:c1], out[r0:r1, c0:c1], block.astype(dtype))
        have[r0:r1, c0:c1] = True
    return out, have


def _valid_cci(agb: np.ndarray, sd: np.ndarray, have: np.ndarray) -> np.ndarray:
    """Validity rule for a product that ships without a NoData tag.

    The data description states that zero is a legitimate value: a pixel with
    AGB = 0 (and SD = 0) is a treeless or cleared pixel, not a gap.  Dropping
    those pixels would remove exactly the areas that lost their cover from both
    dates and inflate the mean stock -- the boundaries must be identical on both
    dates.  The only gap is a pixel that no raster covers (`have` is False).
    """
    return have.copy()


# ---------------------------------------------------------------------------
@dataclass
class AnalysisResult:
    analysis_id: str
    request: Dict[str, object]
    status: Status
    created_at: str
    readiness: Dict[str, object] = field(default_factory=dict)
    geometry_info: Dict[str, object] = field(default_factory=dict)
    carbon: Dict[str, object] = field(default_factory=dict)
    change: Dict[str, object] = field(default_factory=dict)
    uncertainty: Dict[str, object] = field(default_factory=dict)
    baseline: Dict[str, object] = field(default_factory=dict)
    baseline_scenarios: List[Dict[str, object]] = field(default_factory=list)
    units: Dict[str, object] = field(default_factory=dict)
    evidence: Dict[str, object] = field(default_factory=dict)
    ledger: List[Dict[str, object]] = field(default_factory=list)
    provenance: Dict[str, str] = field(default_factory=dict)
    manifest: Dict[str, object] = field(default_factory=dict)
    grid_payload: Dict[str, object] = field(default_factory=dict)
    timings_ms: Dict[str, int] = field(default_factory=dict)
    warnings: List[Dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> Dict[str, object]:
        return {
            "analysis_id": self.analysis_id,
            "created_at": self.created_at,
            "request": self.request,
            "status": self.status.as_dict(),
            "readiness": self.readiness,
            "geometry": self.geometry_info,
            "carbon": self.carbon,
            "change": self.change,
            "uncertainty": self.uncertainty,
            "baseline": self.baseline,
            "baseline_scenarios": self.baseline_scenarios,
            "units": self.units,
            "evidence": self.evidence,
            "ledger": self.ledger,
            "provenance": self.provenance,
            "grid": self.grid_payload,
            "manifest": self.manifest,
            "timings_ms": self.timings_ms,
            "warnings": self.warnings,
        }


ProgressFn = Callable[[str, str, float], None]


def run_analysis(
    request: AnalysisRequest,
    catalog: Optional[Catalog] = None,
    settings: Optional[Settings] = None,
    progress: Optional[ProgressFn] = None,
    analysis_id: Optional[str] = None,
) -> AnalysisResult:
    settings = settings or get_settings()
    catalog = catalog or get_catalog()
    baseline_table = get_baseline_table()
    aid = analysis_id or uuid.uuid4().hex[:16]
    t_start = time.time()
    timings: Dict[str, int] = {}

    def step(stage: str, message: str, fraction: float) -> None:
        if progress:
            progress(stage, message, fraction)

    def mark(name: str, t0: float) -> None:
        timings[name] = int((time.time() - t0) * 1000)

    ledger = Ledger(aid, code_version=_code_version())
    result = AnalysisResult(
        analysis_id=aid,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        status=Status(OK),
        request={
            "geometry": request.raw_geometry,
            "year_start": request.year_start,
            "year_end": request.year_end,
            "label": request.label,
            "aoi_id": request.aoi_id,
            "baseline_variant": request.baseline_variant,
        },
    )

    # -- 1. geometry -------------------------------------------------------
    step("VALIDATING", "Проверка контура и периода", 0.05)
    t0 = time.time()
    bbox = multipolygon_bbox(request.geometry)
    area_ha = multipolygon_area_m2(request.geometry) / 10000.0
    years = list(range(request.year_start, request.year_end + 1))
    grid = _aligned_grid(bbox)
    weights = pixel_weights(grid, request.geometry)
    if weights is None or weights.total_ha <= 0:
        result.status = Status(AOI_TOO_SMALL)
        return result
    lat0 = (bbox[1] + bbox[3]) / 2.0
    px_w, px_h = grid.pixel_size_m(lat0)
    result.geometry_info = {
        "area_ha": area_ha,
        "area_km2": area_ha / 100.0,
        "bbox": list(bbox),
        "analysis_grid": {
            "crs": "EPSG:4326",
            "pixel_deg": abs(grid.dx),
            "pixel_m": [px_w, px_h],
            "pixel_area_ha": px_w * px_h / 10000.0,
            "naive_1ha_overestimate_factor": 10000.0 / (px_w * px_h),
        },
        "weights": weights.as_dict(),
        "geodesic_method": "WGS 84 authalic sphere; areas reproduce areas.csv to 1e-6",
    }
    ledger.value(
        "A", "Площадь контура", area_ha, "га",
        formula="A = Σ aᵢ — сумма геодезических пересечений пикселей с контуром",
        substituted=f"{weights.full_pixels} полных + {weights.partial_pixels} краевых пикселей",
        note=(
            f"Пиксель сетки анализа здесь {px_w:.1f}×{px_h:.1f} м = "
            f"{px_w * px_h / 10000.0:.4f} га, а не 1 га"
        ),
    )
    mark("geometry", t0)

    # -- 2. readiness ------------------------------------------------------
    step("SEARCHING DATA", "Поиск доступных источников", 0.15)
    t0 = time.time()
    readiness = assess_readiness(
        catalog, request.geometry, years, area_ha, baseline_table, probe_live=True
    )
    result.readiness = readiness.as_dict()
    result.warnings.extend(readiness.warnings)
    mark("readiness", t0)

    # -- 3. biomass --------------------------------------------------------
    step("PROCESSING RASTERS", "Чтение карт биомассы", 0.3)
    t0 = time.time()
    annual_agb: Dict[int, np.ndarray] = {}
    annual_valid: Dict[int, np.ndarray] = {}
    annual_sd: Dict[int, np.ndarray] = {}
    used_assets: List = []
    for year in years:
        assets = catalog.query(CCI_AGB, bbox=bbox, year=year)
        if not assets:
            continue
        agb, have = _paste(grid, assets, band=1)
        sd, _ = _paste(grid, assets, band=2)
        annual_agb[year] = agb
        annual_sd[year] = sd
        annual_valid[year] = _valid_cci(agb, sd, have)
        used_assets.extend(assets)

    if request.year_start not in annual_agb or request.year_end not in annual_agb:
        # A contour outside the biomass coverage is a normal outcome, not a
        # crash: the payload keeps its shape so every consumer can render a
        # reason where a number would have been.
        result.status = Status(
            NO_BIOMASS_DATA,
            "Нет карт биомассы на одну из границ периода для этого контура",
        )
        _fill_unavailable(result, request, area_ha, settings, NO_BIOMASS_DATA)
        _finalise(result, ledger, used_assets, settings, timings, t_start)
        return result

    w2 = weights.weights_ha
    sl = (slice(weights.row0, weights.row0 + weights.nrows),
          slice(weights.col0, weights.col0 + weights.ncols))
    agb0 = annual_agb[request.year_start][sl]
    agb1 = annual_agb[request.year_end][sl]
    sd0 = annual_sd[request.year_start][sl]
    sd1 = annual_sd[request.year_end][sl]
    v0 = annual_valid[request.year_start][sl]
    v1 = annual_valid[request.year_end][sl]

    for year, tag in ((request.year_start, "t0"), (request.year_end, "t1")):
        for asset in catalog.query(CCI_AGB, bbox=bbox, year=year):
            ledger.raster_read(
                f"raster:{tag}:{os.path.basename(asset.relative_path)}",
                f"Биомасса {year}",
                asset,
                "AGB + AGB_SD",
                window={
                    "row0": weights.row0,
                    "col0": weights.col0,
                    "nrows": weights.nrows,
                    "ncols": weights.ncols,
                },
                note=f"origin={asset.origin}",
            )
    mark("read_biomass", t0)

    # -- 4. carbon ---------------------------------------------------------
    step("CALCULATING CARBON", "Запас углерода и его изменение", 0.45)
    t0 = time.time()
    cf = settings.cf_agb
    k_co2 = settings.co2_per_c
    annual_for_series = {
        y: (annual_agb[y][sl], annual_valid[y][sl]) for y in sorted(annual_agb)
    }
    carbon = compute_carbon(
        request.year_start,
        request.year_end,
        agb0,
        agb1,
        w2,
        v0,
        v1,
        cf,
        k_co2,
        area_ha,
        annual=annual_for_series,
    )
    result.carbon = carbon.as_dict()
    mark("carbon", t0)

    if carbon.state_start and carbon.state_end:
        ledger.value(
            "C_t0", f"Суммарный запас {request.year_start}", carbon.state_start.total_tc, "т C",
            formula="C(t) = Σ aᵢ · CF · bᵢ(t)",
            substituted=f"CF = {cf}; пикселей {carbon.state_start.valid_pixels}",
            pool=POOL,
        )
        ledger.value(
            "C_t1", f"Суммарный запас {request.year_end}", carbon.state_end.total_tc, "т C",
            formula="C(t) = Σ aᵢ · CF · bᵢ(t)",
            substituted=f"CF = {cf}; пикселей {carbon.state_end.valid_pixels}",
            pool=POOL,
        )
        ledger.value(
            "dC", "Разность запасов", carbon.delta_c_tc, "т C",
            formula="ΔC = C(t₁) − C(t₀)",
            substituted=f"{carbon.state_end.total_tc:.3f} − {carbon.state_start.total_tc:.3f}",
            inputs={"C_t1": "C_t1", "C_t0": "C_t0"}, pool=POOL,
        )
        ledger.value(
            "E_proj", "Изменение запаса за период", carbon.e_total.value, "т CO2-экв.",
            formula="E = −ΔC · 44/12",
            substituted=f"−({carbon.delta_c_tc:.3f}) · {k_co2:.6f}",
            inputs={"dC": "dC"},
            params=[{"id": "CO2_per_C", "value": "44/12", "source_id": "IPCC_GENERIC_2006"}],
            pool=POOL, sign_convention=SIGN_CONVENTION,
        )
        carbon.e_total.node_id = "E_proj"

    # -- 5. change ---------------------------------------------------------
    step("DETECTING CHANGES", "Выделение очагов изменений", 0.6)
    t0 = time.time()
    change = chg.build_patches(
        agb0, agb1, sd0, sd1, w2, v0 & v1, grid, weights.row0, weights.col0,
        cf, k_co2, carbon.e_total.value or 0.0,
    )
    mark("change", t0)

    # -- 6. scenes and dating ---------------------------------------------
    step("BUILDING EVIDENCE", "Сбор подтверждений по источникам", 0.72)
    t0 = time.time()
    scene_list = sq.assess_all(catalog, request.geometry, bbox, years)
    selected = sq.select_per_year(scene_list)
    scene_nbr: List[Dict[str, object]] = []
    footprint_cache: Dict[Tuple[float, ...], Dict] = {}
    for year in sorted(selected):
        s = selected[year]
        if s is None:
            continue
        stack = sq.read_index_stack(s, request.geometry)
        if stack is None:
            continue
        sgrid = stack["grid"]
        key = (sgrid.origin_x, sgrid.origin_y, sgrid.dx, sgrid.dy, sgrid.width, sgrid.height)
        if key not in footprint_cache:
            footprint_cache[key] = precompute_footprints(grid, weights, sgrid)
        nbr, cov = aggregate_to_grid(
            grid, weights, sgrid, _place(stack["NBR"], stack["window"], sgrid),
            mode="mean",
            valid=_place(stack["valid"], stack["window"], sgrid, fill=False),
            footprints=footprint_cache[key],
        )
        scene_nbr.append(
            {"scene": s, "date": s.datetime_utc[:10], "scene_id": s.scene_id, "nbr": nbr, "cov": cov}
        )
    mark("scenes", t0)

    gfc_assets = catalog.query(GFC, bbox=bbox)
    modis_assets = catalog.query(MODIS_BURN, bbox=bbox)

    exhibits: List[Dict[str, object]] = []
    rung_counts: Dict[str, int] = {}
    confirmed_abs = 0.0
    total_abs = sum(abs(p.e_tco2e) for p in change.patches) or 1.0

    for patch in change.patches:
        windows: List[chg.DateWindow] = []
        evidences: List[Evidence] = []

        w_cci = chg.window_from_cci(
            patch, {y: annual_agb[y][sl] for y in annual_agb}, w2,
            request.year_start, request.year_end,
        )
        windows.append(w_cci)
        evidences.append(
            Evidence(
                source_id="CCI_V7", product="CCI_AGB", title="ESA CCI Biomass",
                claim="fact", observed=True,
                detail=f"Δb = {patch.delta_agb_t_ha:+.1f} т/га на площади {patch.area_ha:.1f} га",
                human=(
                    f"Оценка биомассы на этом участке изменилась на "
                    f"{patch.delta_agb_t_ha:+.1f} т/га"
                ),
                observation_time=f"{request.year_start}→{request.year_end}",
                spatial_support_m=100.0,
                date_window=w_cci.as_dict(),
                limitations="Годовые модельные оценки без внутригодового разрешения",
            )
        )

        # Sentinel-2
        s2_change: Optional[bool] = None
        per_scene = []
        for entry in scene_nbr:
            vals = []
            covs = []
            for r, c in patch.cells:
                if np.isfinite(entry["nbr"][r, c]):
                    vals.append(entry["nbr"][r, c])
                covs.append(entry["cov"][r, c])
            share = float(np.mean(covs)) if covs else 0.0
            per_scene.append(
                {
                    "scene_id": entry["scene_id"],
                    "date": entry["date"],
                    "patch_nbr": float(np.mean(vals)) if vals else None,
                    "patch_valid_share": share,
                }
            )
        w_s2 = chg.window_from_sentinel2(patch, per_scene)
        if per_scene:
            usable = [p for p in per_scene if p["patch_valid_share"] >= 0.2]
            if len(usable) >= 2:
                s2_change = w_s2 is not None
        if w_s2:
            windows.append(w_s2)
        evidences.append(
            Evidence(
                source_id="S2_L2A", product="S2_REFLECTANCE", title="Sentinel-2 L2A",
                claim="date", observed=s2_change is not None,
                detail=(
                    w_s2.note if w_s2 else
                    "Недостаточно годных наблюдений внутри очага для датировки"
                ),
                human=(
                    f"Снимки до и после: {w_s2.start} → {w_s2.end}" if w_s2
                    else "Снимки не позволяют датировать изменение на этом очаге"
                ),
                observation_time=w_s2.start if w_s2 else None,
                spatial_support_m=20.0,
                date_window=w_s2.as_dict() if w_s2 else None,
                limitations="Остаточные эффекты облаков и теней; SCL получен автоматически",
            )
        )

        # GFC
        gfc_loss: Optional[bool] = None
        counts: Dict[int, int] = {}
        if gfc_assets:
            counts = _gfc_loss_years(patch, grid, weights, gfc_assets)
            in_period = {y: n for y, n in counts.items()
                         if request.year_start < y <= request.year_end}
            gfc_loss = bool(in_period)
            w_gfc = chg.window_from_gfc(patch, counts, request.year_start, request.year_end)
            if w_gfc:
                windows.append(w_gfc)
            area_gfc = _gfc_loss_area_ha(patch, grid, weights, gfc_assets,
                                         request.year_start, request.year_end)
            patch.area_gfc_ha = area_gfc
            evidences.append(
                Evidence(
                    source_id="GFC_2025_V113", product="GFC", title="Hansen Global Forest Change",
                    claim="area", observed=True,
                    # `_gfc_loss_years` already decodes lossyear into calendar
                    # years; adding the epoch again here produced "4021".
                    detail=(
                        "годы потери в очаге: "
                        + (", ".join(f"{y}: {n} пикс." for y, n in sorted(counts.items())) or "нет")
                    ),
                    human=(
                        f"Потеря древесного покрова отмечена в {min(in_period)} году"
                        if in_period else "Потеря древесного покрова в этом периоде не отмечена"
                    ),
                    spatial_support_m=30.0,
                    limitations="Указан один год потери для пикселя; причина продуктом не определяется",
                )
            )

        # MODIS
        modis_burn: Optional[bool] = None
        burn_info = None
        if modis_assets:
            burn_info = _modis_burn(patch, grid, weights, modis_assets)
            modis_burn = burn_info["cells"] > 0
            w_modis = chg.window_from_modis(burn_info)
            if w_modis:
                windows.append(w_modis)
            evidences.append(
                Evidence(
                    source_id="MODIS_MCD64A1_061", product="MODIS_BURN", title="MODIS MCD64A1",
                    claim="date", observed=True,
                    detail=(
                        f"{burn_info['cells']} ячеек 463 м с признаком горения, "
                        f"{burn_info.get('first_date')}…{burn_info.get('last_date')}"
                        if modis_burn else "признак горения не отмечен"
                    ),
                    human=(
                        f"Продукт гарей отметил горение {burn_info.get('first_date')} — "
                        f"{burn_info.get('last_date')}"
                        if modis_burn else "Продукт гарей горения здесь не отметил"
                    ),
                    spatial_support_m=463.3,
                    date_window=w_modis.as_dict() if w_modis else None,
                    limitations=(
                        "Шаг сетки около 463 м: продукт даёт время и признак события, "
                        "но не геометрию и не площадь"
                    ),
                )
            )

        chg.assign_windows(patch, windows)
        verdict = evaluate_ladder(
            cci_change=True,
            s2_change=s2_change,
            gfc_loss=gfc_loss,
            modis_burn=modis_burn,
            windows_consistent=not patch.conflict,
            kind=patch.kind,
        )
        patch.rung = verdict.rung
        patch.rung_label = verdict.label
        patch.cause = verdict.cause
        patch.missing_conditions = verdict.missing
        patch.evidence = [e.as_dict() for e in evidences]
        rung_counts[patch.rung] = rung_counts.get(patch.rung, 0) + 1
        if patch.rung.startswith("A"):
            confirmed_abs += abs(patch.e_tco2e)

        if patch is change.patches[0]:
            exhibits = _exhibits_for(patch, evidences, request)

    result.change = change.as_dict()
    result.change["scenes"] = [s.as_dict() for s in scene_list]
    result.change["selected_by_year"] = {
        str(y): (s.scene_id if s else None) for y, s in sorted(selected.items())
    }

    # -- 7. uncertainty ----------------------------------------------------
    step("CALCULATING UNCERTAINTY", "Перенос ошибки и слои неопределённости", 0.82)
    t0 = time.time()
    years_without = [y for y in years if selected.get(y) is None]
    window_days = min(
        (p.final_window.days for p in change.patches if p.final_window.days is not None),
        default=None,
    )
    layers = [
        unc.layer_data_quality(years, selected, scene_list),
        unc.layer_model_error(None, carbon.e_total.value),
        unc.layer_spatial_coverage(
            carbon.coverage_fraction,
            weights.partial_area_ha / weights.total_ha if weights.total_ha else 0.0,
            carbon.area_gap_ha,
        ),
        unc.layer_temporal_coverage(window_days, years_without, carbon.delta_years),
        unc.layer_causal_evidence(confirmed_abs / total_abs, rung_counts),
    ]
    cross = _cross_check_change_product(catalog, bbox, grid, weights, sl, annual_sd, cf)
    extra = []
    if cross and cross.get("implied_rho_t_clipped") is not None:
        rho = float(cross["implied_rho_t_clipped"])
        extra.append(
            {
                "id": "esa_reconciled",
                "label": f"Согласовано с продуктом разности ESA (ρ_t = {rho:.2f})",
                "spatial_model": "exponential",
                "correlation_length_m": settings.methods["uncertainty"].get(
                    "correlation_length_m", 1000.0
                ),
                "temporal_correlation": rho,
            }
        )
    u = unc.compute_uncertainty(
        carbon.e_total.value, sd0, sd1, w2, grid, weights.row0, weights.col0,
        lat0, cf, k_co2, layers, change_product=cross, extra_scenarios=extra,
    )
    layers[1] = unc.layer_model_error(u.sigma_e, carbon.e_total.value)
    u.layers = layers
    result.uncertainty = u.as_dict()
    mark("uncertainty", t0)

    if u.sigma_e is not None:
        ledger.value(
            "sigma_E", "Стандартное отклонение результата", u.sigma_e, "т CO2-экв.",
            formula="σ(E) = CF · 44/12 · √(Var(C₁) + Var(C₀) − 2ρ_t√(Var(C₁)Var(C₀)))",
            substituted=f"сценарий {u.active_scenario}",
            note="Сценарный диапазон, не доверительный интервал",
        )
        subst = f"{carbon.e_total.value:.3f} ∓ {u.k_multiplier:g}·{u.sigma_e:.3f}"
        ledger.value("L", "Нижняя граница", u.lower.value, "т CO2-экв.",
                     formula="L = E − k·σ(E)", substituted=subst,
                     inputs={"E_proj": "E_proj", "sigma": "sigma_E"},
                     note="Граница сценарного диапазона, не доверительного интервала")
        ledger.value("U", "Верхняя граница", u.upper.value, "т CO2-экв.",
                     formula="U = E + k·σ(E)", substituted=subst,
                     inputs={"E_proj": "E_proj", "sigma": "sigma_E"},
                     note="Граница сценарного диапазона, не доверительного интервала")

    # -- 8. baseline and units --------------------------------------------
    step("CALCULATING UNITS", "Базовая линия и потенциальные единицы", 0.9)
    t0 = time.time()
    controls = baseline_table.control_candidates()
    base = baseline_table.evaluate(
        request.geometry, request.year_start, request.year_end, area_ha, k_co2,
        variant=request.baseline_variant,
        control_aoi=controls[0] if controls else None,
    )
    result.baseline = base.as_dict()
    if base.e_base.value is not None:
        ledger.value(
            "E_base", "Результат базовой линии", base.e_base.value, "т CO2-экв.",
            formula="E_base = −Σ (c_base(t₁) − c_base(t₀)) · A_part · 44/12",
            substituted="; ".join(
                f"{p.aoi_id}: Δ={p.delta_tc_ha:+.4f} т C/га × {p.area_ha:.2f} га"
                for p in base.parts
            ),
            params=[{"id": "baseline", "value": "HIST-AGB-2015-2019-v1", "source_id": "CASE_RULES_V1"}],
        )

    units = compute_units(
        e_proj=carbon.e_total.value,
        e_base=base.e_base.value,
        lower=u.lower.value,
        upper=u.upper.value,
        leakage=settings.leakage,
        allowance=settings.unc_allowance,
        stop_ratio=settings.unc_stop_ratio,
        buffer_share=settings.buffer_share,
        coverage_fraction=carbon.coverage_fraction,
        prices=settings.prices,
        baseline_available=base.status.ok,
    )
    result.units = units.as_dict()
    if units.r.value is not None:
        ledger.value("R", "Результат относительно базовой линии", units.r.value, "т CO2-экв.",
                     formula="R = E_base − E_proj − LK",
                     substituted=f"{base.e_base.value:.3f} − {carbon.e_total.value:.3f} − {settings.leakage}",
                     inputs={"E_base": "E_base", "E_proj": "E_proj"})
    if units.q.value is not None:
        ledger.value("Q", "Потенциальные единицы", units.q.value, "единиц",
                     formula="Q = ⌊R_adj − B⌋",
                     substituted=(
                         f"UNC = {units.unc.value if units.unc.value is not None else '—'}; "
                         f"R_adj = {units.r_adj.value if units.r_adj.value is not None else '—'}"
                     ),
                     inputs={"R": "R", "H": "sigma_E"},
                     note=units.disclaimer)

    # baseline stress test: recompute units under research variants
    for variant in ("flat", "control"):
        alt = baseline_table.evaluate(
            request.geometry, request.year_start, request.year_end, area_ha, k_co2,
            variant=variant, control_aoi=controls[0] if controls else None,
        )
        alt_units = compute_units(
            e_proj=carbon.e_total.value, e_base=alt.e_base.value,
            lower=u.lower.value, upper=u.upper.value,
            leakage=settings.leakage, allowance=settings.unc_allowance,
            stop_ratio=settings.unc_stop_ratio, buffer_share=settings.buffer_share,
            coverage_fraction=carbon.coverage_fraction, prices=settings.prices,
            baseline_available=alt.status.ok,
        )
        result.baseline_scenarios.append(
            {
                "variant": variant,
                "label": alt.variant_label,
                "counts_for_units": False,
                "E_base": alt.e_base.as_dict(),
                "R": alt_units.r.as_dict(),
                "H_over_R": alt_units.h_over_r.as_dict(),
                "Q": alt_units.q.as_dict(),
                "status": alt_units.status.as_dict(),
            }
        )
    # the same numbers under the three correlation scenarios
    for sc in u.scenarios:
        su = compute_units(
            e_proj=carbon.e_total.value, e_base=base.e_base.value,
            lower=sc.lower, upper=sc.upper,
            leakage=settings.leakage, allowance=settings.unc_allowance,
            stop_ratio=settings.unc_stop_ratio, buffer_share=settings.buffer_share,
            coverage_fraction=carbon.coverage_fraction, prices=settings.prices,
            baseline_available=base.status.ok,
        )
        result.baseline_scenarios.append(
            {
                "variant": f"uncertainty:{sc.id}",
                "label": f"Модель ошибки: {sc.label}",
                "counts_for_units": sc.id == u.active_scenario,
                "H": sc.h,
                "H_over_R": su.h_over_r.as_dict(),
                "Q": su.q.as_dict(),
                "status": su.status.as_dict(),
            }
        )
    mark("units", t0)

    # -- 8b. map payload ---------------------------------------------------
    d_map = change.delta_map
    west = grid.origin_x + weights.col0 * grid.dx
    north = grid.origin_y + weights.row0 * grid.dy
    east = west + weights.ncols * grid.dx
    south = north + weights.nrows * grid.dy
    vals: List[Optional[float]] = []
    mask: List[int] = []
    for r in range(weights.nrows):
        for c in range(weights.ncols):
            inside = w2[r, c] > 0
            mask.append(1 if inside else 0)
            vals.append(round(float(d_map[r, c]), 3) if inside else None)
    finite = [abs(v) for v in vals if v is not None]
    result.grid_payload = {
        "west": west, "south": south, "east": east, "north": north,
        "rows": weights.nrows, "cols": weights.ncols,
        "unit": "т сухого вещества/га",
        "label": f"Изменение биомассы {request.year_start}→{request.year_end}",
        "abs_max": (sorted(finite)[int(0.97 * (len(finite) - 1))] if finite else 1.0),
        "values": vals,
        "mask": mask,
        "modis_cells": _modis_footprints(catalog, bbox, modis_assets),
    }

    # -- 9. evidence package ----------------------------------------------
    result.evidence = {
        "case_file": ledger.case_file(exhibits),
        "competence_matrix": competence_table(),
        "rung_counts": rung_counts,
        "confirmed_share_of_change": confirmed_abs / total_abs if change.patches else 0.0,
        "modis_area_guard": modis_area_guard(),
        "sources": _sources_used(catalog, used_assets, gfc_assets, modis_assets, scene_list),
    }

    step("READY", "Готово", 1.0)
    _finalise(result, ledger, used_assets + list(gfc_assets) + list(modis_assets),
              settings, timings, t_start)
    return result


# ---------------------------------------------------------------------------
def _place(arr: np.ndarray, window, grid: RasterGrid, fill=np.nan) -> np.ndarray:
    """Expand a windowed array back onto the full source grid."""
    r0, c0, nr, nc = window
    full = np.full((grid.height, grid.width), fill, dtype=arr.dtype)
    full[r0 : r0 + nr, c0 : c0 + nc] = arr
    return full


def _gfc_loss_years(patch, grid, weights, gfc_assets) -> Dict[int, int]:
    counts: Dict[int, int] = {}
    for asset in gfc_assets:
        with GeoTiff(asset.path) as t:
            for r, c in patch.cells:
                win = footprint_in_source(grid, weights.row0 + r, weights.col0 + c, asset.grid)
                if win is None:
                    continue
                sr, sc, nr, nc = win
                block = t.read(band=2, window=(sr, sc, nr, nc))
                for v in np.unique(block):
                    if v > 0:
                        counts[int(v) + 2000] = counts.get(int(v) + 2000, 0) + int(
                            np.count_nonzero(block == v)
                        )
    return counts


def _gfc_loss_area_ha(patch, grid, weights, gfc_assets, y0: int, y1: int) -> Optional[float]:
    """Area of loss inside a patch, measured on the 30 m grid."""
    if not gfc_assets:
        return None
    total = 0.0
    for asset in gfc_assets:
        lat = asset.bounds_wgs84[1]
        px_w, px_h = asset.grid.pixel_size_m((asset.bounds_wgs84[1] + asset.bounds_wgs84[3]) / 2)
        cell_ha = px_w * px_h / 10000.0
        with GeoTiff(asset.path) as t:
            for r, c in patch.cells:
                win = footprint_in_source(grid, weights.row0 + r, weights.col0 + c, asset.grid)
                if win is None:
                    continue
                sr, sc, nr, nc = win
                block = t.read(band=2, window=(sr, sc, nr, nc)).astype(int) + 2000
                hit = (block > y0) & (block <= y1)
                total += float(np.count_nonzero(hit)) * cell_ha
    return total


def _modis_burn(patch, grid, weights, modis_assets) -> Dict[str, object]:
    by_layer = {a.layer: a for a in modis_assets if a.layer}
    info: Dict[str, object] = {"cells": 0}
    burn_asset = by_layer.get("Burn_Date")
    if burn_asset is None:
        return info
    year = burn_asset.year or 2021
    firsts: List[int] = []
    lasts: List[int] = []
    uncs: List[int] = []
    seen = set()
    with GeoTiff(burn_asset.path) as tb:
        first_a = by_layer.get("First_Day")
        last_a = by_layer.get("Last_Day")
        unc_a = by_layer.get("Burn_Date_Uncertainty")
        tf = GeoTiff(first_a.path) if first_a else None
        tl = GeoTiff(last_a.path) if last_a else None
        tu = GeoTiff(unc_a.path) if unc_a else None
        try:
            for r, c in patch.cells:
                win = footprint_in_source(grid, weights.row0 + r, weights.col0 + c, burn_asset.grid)
                if win is None:
                    continue
                sr, sc, nr, nc = win
                block = tb.read(band=1, window=(sr, sc, nr, nc))
                for i in range(block.shape[0]):
                    for j in range(block.shape[1]):
                        key = (sr + i, sc + j)
                        if key in seen or block[i, j] <= 0:
                            continue
                        seen.add(key)
                        firsts.append(
                            int(tf.read(band=1, window=(key[0], key[1], 1, 1))[0, 0]) if tf else int(block[i, j])
                        )
                        lasts.append(
                            int(tl.read(band=1, window=(key[0], key[1], 1, 1))[0, 0]) if tl else int(block[i, j])
                        )
                        uncs.append(
                            int(tu.read(band=1, window=(key[0], key[1], 1, 1))[0, 0]) if tu else 0
                        )
        finally:
            for t in (tf, tl, tu):
                if t:
                    t.close()
    if not firsts:
        return info
    info["cells"] = len(seen)
    info["first_date"] = _from_doy(year, max(1, min(firsts)))
    info["last_date"] = _from_doy(year, max(lasts))
    info["uncertainty_days"] = max(uncs) if uncs else 0
    info["nominal_area_ha"] = len(seen) * (463.3127165279167 ** 2) / 10000.0
    return info


def _from_doy(year: int, doy: int) -> str:
    return date.fromordinal(date(year, 1, 1).toordinal() + int(doy) - 1).isoformat()


def _modis_footprints(catalog, bbox, modis_assets) -> List[List[float]]:
    """Burned 463 m cells as lon/lat boxes, so the map can show how coarse they are."""
    from .geo.crs import sinusoidal_to_wgs84

    burn = next((a for a in modis_assets if a.layer == "Burn_Date"), None)
    if burn is None:
        return []
    out: List[List[float]] = []
    with GeoTiff(burn.path) as t:
        arr = t.read(band=1)
    g = burn.grid
    rr, cc = np.nonzero(arr > 0)
    for r, c in zip(rr.tolist(), cc.tolist()):
        x0 = g.origin_x + c * g.dx
        x1 = g.origin_x + (c + 1) * g.dx
        y0 = g.origin_y + r * g.dy
        y1 = g.origin_y + (r + 1) * g.dy
        lon0, lat0 = sinusoidal_to_wgs84(x0, y0)
        lon1, lat1 = sinusoidal_to_wgs84(x1, y1)
        w, e = min(lon0, lon1), max(lon0, lon1)
        s_, n_ = min(lat0, lat1), max(lat0, lat1)
        if e < bbox[0] or w > bbox[2] or n_ < bbox[1] or s_ > bbox[3]:
            continue
        out.append([w, s_, e, n_])
    return out


def _cross_check_change_product(catalog, bbox, grid, weights, sl, annual_sd, cf):
    """Compare our propagated sigma with ESA's own difference uncertainty."""
    assets = catalog.query(CCI_CHANGE, bbox=bbox)
    if not assets or 2019 not in annual_sd or 2020 not in annual_sd:
        return None
    sd_diff, have = _paste(grid, assets, band=2)
    w = weights.weights_ha
    sub = sd_diff[sl]
    esa_sigma = float(np.sqrt(np.sum((w * sub) ** 2))) * cf
    s19 = annual_sd[2019][sl]
    s20 = annual_sd[2020][sl]
    var19 = float(np.sum((w * s19) ** 2))
    var20 = float(np.sum((w * s20) ** 2))
    our_sigma = cf * math.sqrt(var19 + var20)
    return unc.reconcile_with_change_product(our_sigma, esa_sigma, var19, var20, cf)


def _exhibits_for(patch, evidences, request) -> List[Dict[str, object]]:
    out = []
    for ev in evidences:
        out.append(
            {
                "title": ev.title,
                "plain": ev.human or ev.detail,
                "source_id": ev.source_id,
                "file": ev.file,
                "date": ev.observation_time,
                "detail": ev.detail,
            }
        )
    out.append(
        {
            "title": "Вклад в итог",
            "plain": (
                f"Этот очаг площадью {patch.area_ha:.1f} га даёт "
                f"{patch.e_tco2e:,.0f} т CO₂-экв., то есть "
                f"{patch.contribution_share * 100:.0f} % итогового изменения"
            ).replace(",", " "),
            "detail": f"ΔC = {patch.delta_c_tc:.1f} т C",
        }
    )
    return out


def _sources_used(catalog, cci_assets, gfc_assets, modis_assets, scenes) -> List[Dict[str, object]]:
    from .dal.catalog import load_sources_table

    table = {r["source_id"]: r for r in load_sources_table()}
    used = set()
    for a in list(cci_assets) + list(gfc_assets) + list(modis_assets):
        used.add(a.source_id)
    if scenes:
        used.add("S2_L2A")
    used.update({"IPCC_FOREST_2006", "IPCC_GENERIC_2006", "CASE_RULES_V1"})
    out = []
    for sid in sorted(used):
        row = table.get(sid)
        if row:
            out.append(
                {
                    "source_id": sid,
                    "product": row.get("product"),
                    "version": row.get("version"),
                    "doi": row.get("doi"),
                    "license_url": row.get("license_url"),
                    "attribution": row.get("required_attribution"),
                    "limitations": row.get("limitations"),
                    "access_date": row.get("access_date"),
                    "url": row.get("primary_url"),
                }
            )
    return out


def _fill_unavailable(result, request, area_ha: float, settings, code: str) -> None:
    """Populate every section with a well-formed "not available" payload."""
    from .carbon.stock import CarbonResult
    from .change.detect import ChangeResult
    from .uncertainty.propagate import UncertaintyResult

    st = Status(code)
    carbon = CarbonResult(
        cf=settings.cf_agb,
        co2_per_c=settings.co2_per_c,
        year_start=request.year_start,
        year_end=request.year_end,
        delta_years=request.year_end - request.year_start,
        area_requested_ha=area_ha,
        status=st,
    )
    carbon.e_total.status = st
    carbon.e_per_ha_year.status = st
    result.carbon = carbon.as_dict()
    result.change = ChangeResult().as_dict()
    result.change["scenes"] = []
    result.change["selected_by_year"] = {}
    result.uncertainty = UncertaintyResult().as_dict()
    baseline = get_baseline_table().evaluate(
        request.geometry, request.year_start, request.year_end, area_ha,
        settings.co2_per_c,
    )
    result.baseline = baseline.as_dict()
    result.units = compute_units(
        e_proj=None, e_base=baseline.e_base.value, lower=None, upper=None,
        leakage=settings.leakage, allowance=settings.unc_allowance,
        stop_ratio=settings.unc_stop_ratio, buffer_share=settings.buffer_share,
        coverage_fraction=0.0, prices=settings.prices,
        baseline_available=baseline.status.ok,
    ).as_dict()
    result.evidence = {
        "case_file": [],
        "competence_matrix": competence_table(),
        "rung_counts": {},
        "confirmed_share_of_change": 0.0,
        "modis_area_guard": modis_area_guard(),
        "sources": [],
    }
    result.grid_payload = {}


_CODE_VERSION: Optional[str] = None


def _code_version() -> str:
    """Identify the code that produced a result.

    A commit hash when there is one; otherwise a checksum of the sources
    themselves, which is what the reader actually needs -- "unversioned" in an
    audit trail means the trail stops one step short of the code.  Computed once
    per process, since the sources cannot change under a running server.
    """
    global _CODE_VERSION
    if _CODE_VERSION is not None:
        return _CODE_VERSION

    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    head = os.path.join(root, ".git", "HEAD")
    try:
        with open(head, "r", encoding="utf-8") as fh:
            ref = fh.read().strip()
        if ref.startswith("ref:"):
            with open(os.path.join(root, ".git", ref[4:].strip()), encoding="utf-8") as fh:
                ref = fh.read().strip()
        _CODE_VERSION = "git:" + ref[:40]
        return _CODE_VERSION
    except Exception:
        pass

    digest = hashlib.sha256()
    src = os.path.join(root, "src")
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        for name in sorted(filenames):
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            digest.update(os.path.relpath(path, src).encode("utf-8"))
            with open(path, "rb") as fh:
                digest.update(fh.read())
    _CODE_VERSION = "src-sha256:" + digest.hexdigest()[:16]
    return _CODE_VERSION


def _finalise(result, ledger, assets, settings, timings, t_start) -> None:
    files = []
    seen = set()
    for a in assets:
        if a.relative_path in seen:
            continue
        seen.add(a.relative_path)
        files.append(
            {
                "file": a.relative_path,
                "sha256": a.sha256(),
                "product": a.product,
                "source_id": a.source_id,
                "version": a.version,
                "origin": a.origin,
            }
        )
    result.manifest = {
        "analysis_id": result.analysis_id,
        "created_at": result.created_at,
        "code_version": ledger.code_version,
        "request": result.request,
        "parameters": {
            name: {"value": p.value, "unit": p.unit, "source_id": p.source_id}
            for name, p in settings.parameters.items()
        },
        "methods": settings.methods,
        "limits": settings.limits,
        "inputs": files,
        "replay": f"python3 -m canopy.cli replay {result.analysis_id}",
    }
    result.ledger = ledger.as_list()
    result.provenance = {
        node.node_id: provenance_line(node)
        for node in ledger.nodes.values()
        if node.kind == "value"
    }
    timings["total"] = int((time.time() - t_start) * 1000)
    result.timings_ms = timings
