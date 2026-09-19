"""Data readiness: what exists for this contour, before anything is computed.

Most services compute first and apologise afterwards.  This one answers "what
will I be able to tell you, and what will I not" before the user spends any
time, which is what the case means by reporting the reason and the part of the
result that could be obtained.

For every product the screen shows availability, the covered share of the
contour, the period actually present, a quality figure and the source with its
version.  It also lists, in plain words, which downstream numbers will be
refused and why -- for example that units cannot be issued when coverage is
below one, or that dating will be wide because a year has no usable scene.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from ..core.config import get_settings
from ..core.status import (
    NO_BIOMASS_DATA,
    NO_VALID_OBSERVATIONS,
    OK,
    OUT_OF_BASELINE_COVERAGE,
    PARTIAL_COVERAGE,
    SOURCE_UNREACHABLE,
    Status,
    UNITS_UNAVAILABLE_PARTIAL_COVERAGE,
)
from ..baseline.table import BaselineTable
from ..geo.ellipsoid import polygon_area_m2
from ..geo.polygon import MultiPolygon, clip_polygon_to_rect, multipolygon_bbox
from . import stac
from .catalog import (
    CCI_AGB,
    CCI_CHANGE,
    GFC,
    MODIS_BURN,
    PRODUCT_LABELS,
    S2_REFLECTANCE,
    S2_SCL,
    Catalog,
)

AVAILABLE = "AVAILABLE"
PARTIAL = "PARTIAL"
UNAVAILABLE = "UNAVAILABLE"


@dataclass
class ProductReadiness:
    product: str
    label: str
    state: str
    coverage_fraction: float = 0.0
    files: int = 0
    years: List[int] = field(default_factory=list)
    period: Optional[str] = None
    quality: Optional[str] = None
    source_id: str = ""
    version: str = ""
    origin: Dict[str, int] = field(default_factory=dict)
    note: str = ""
    live: Optional[Dict[str, object]] = None

    def as_dict(self) -> Dict[str, object]:
        return {
            "product": self.product,
            "label": self.label,
            "state": self.state,
            "coverage_fraction": self.coverage_fraction,
            "files": self.files,
            "years": self.years,
            "period": self.period,
            "quality": self.quality,
            "source_id": self.source_id,
            "version": self.version,
            "origin": self.origin,
            "note": self.note,
            "live": self.live,
        }


@dataclass
class Readiness:
    products: List[ProductReadiness] = field(default_factory=list)
    baseline: Optional[ProductReadiness] = None
    blocked: List[Dict[str, str]] = field(default_factory=list)
    warnings: List[Dict[str, str]] = field(default_factory=list)
    uses_fixtures: bool = False
    fixture_notice: Optional[Dict[str, object]] = None
    area_ha: float = 0.0
    bbox: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)

    def as_dict(self) -> Dict[str, object]:
        return {
            "products": [p.as_dict() for p in self.products]
            + ([self.baseline.as_dict()] if self.baseline else []),
            "blocked": self.blocked,
            "warnings": self.warnings,
            "uses_fixtures": self.uses_fixtures,
            "fixture_notice": self.fixture_notice,
            "area_ha": self.area_ha,
            "bbox": list(self.bbox),
        }

    def can_compute_carbon(self) -> bool:
        for p in self.products:
            if p.product == CCI_AGB:
                return p.state != UNAVAILABLE
        return False

    def can_compute_units(self) -> bool:
        if not self.can_compute_carbon():
            return False
        if self.baseline is None or self.baseline.state != AVAILABLE:
            return False
        for p in self.products:
            if p.product == CCI_AGB and p.coverage_fraction < 1.0 - 1e-9:
                return False
        return True


def _coverage_fraction(geometry: MultiPolygon, assets, area_ha: float) -> float:
    """Share of the contour inside the union of a product's footprints.

    Footprints are rectangles in WGS 84, so clipping is exact.  Overlaps between
    footprints are resolved by clipping each piece and keeping the largest, which
    is correct for the tiled layout of every product used here.
    """
    if not assets or area_ha <= 0:
        return 0.0
    # Assets of the same product for different years share one footprint; summing
    # over assets would count the same ground several times and report full
    # coverage for a contour that is only half inside the data.
    seen = set()
    covered_ha = 0.0
    for asset in assets:
        key = tuple(round(v, 9) for v in asset.bounds_wgs84)
        if key in seen:
            continue
        seen.add(key)
        w, s, e, n = asset.bounds_wgs84
        covered = 0.0
        for poly in geometry:
            clipped = clip_polygon_to_rect(poly, w, s, e, n)
            if clipped:
                covered += polygon_area_m2(clipped)
        covered_ha += covered / 10000.0
    return min(1.0, covered_ha / area_ha)


def assess(
    catalog: Catalog,
    geometry: MultiPolygon,
    years: Sequence[int],
    area_ha: float,
    baseline_table: Optional[BaselineTable] = None,
    probe_live: bool = True,
) -> Readiness:
    bbox = multipolygon_bbox(geometry)
    out = Readiness(area_ha=area_ha, bbox=bbox)
    year_set = list(years)

    for product in (CCI_AGB, S2_REFLECTANCE, GFC, MODIS_BURN, CCI_CHANGE):
        assets = catalog.query(product, bbox=bbox)
        in_period = [
            a for a in assets if a.year is None or a.year in year_set or product == GFC
        ]
        pr = ProductReadiness(
            product=product,
            label=PRODUCT_LABELS.get(product, product),
            state=UNAVAILABLE,
            files=len(in_period),
        )
        if in_period:
            pr.source_id = in_period[0].source_id
            pr.version = in_period[0].version
            pr.years = sorted({a.year for a in in_period if a.year})
            pr.coverage_fraction = _coverage_fraction(geometry, in_period, area_ha)
            pr.state = AVAILABLE if pr.coverage_fraction >= 1.0 - 1e-6 else PARTIAL
            if pr.coverage_fraction <= 1e-9:
                pr.state = UNAVAILABLE
            if pr.years:
                pr.period = f"{min(pr.years)}–{max(pr.years)}"
            for a in in_period:
                pr.origin[a.origin] = pr.origin.get(a.origin, 0) + 1
            if pr.origin.get("fixture"):
                out.uses_fixtures = True
        else:
            box = catalog.coverage_bbox(product)
            pr.note = (
                "Нет файлов, пересекающих контур"
                if box
                else "Продукт отсутствует в каталоге"
            )
        out.products.append(pr)

    # Sentinel-2 scene classification travels with the reflectance
    scl_assets = catalog.query(S2_SCL, bbox=bbox)
    refl = next((p for p in out.products if p.product == S2_REFLECTANCE), None)
    if refl is not None:
        n_scenes = len({a.scene_id for a in scl_assets if a.year in year_set})
        refl.quality = f"{n_scenes} сцен в периоде"
        if n_scenes == 0 and refl.state != UNAVAILABLE:
            refl.state = PARTIAL
            refl.note = "Нет сцен внутри выбранного периода"

    cci = next((p for p in out.products if p.product == CCI_AGB), None)
    if cci is not None and cci.years:
        missing = [y for y in year_set if y not in cci.years]
        if missing:
            cci.note = "Нет карт биомассы за: " + ", ".join(str(y) for y in missing)
            cci.state = PARTIAL if cci.state == AVAILABLE else cci.state
        cci.quality = f"{len(cci.years)} годовых карт"

    # -- baseline ----------------------------------------------------------
    bt = baseline_table or BaselineTable()
    parts = bt.parts_for(geometry)
    covered = sum(a for _area, a in parts)
    bpr = ProductReadiness(
        product="BASELINE",
        label="Базовая линия кейса",
        state=UNAVAILABLE,
        source_id="CASE_RULES_V1",
        files=len(parts),
    )
    if parts:
        bpr.coverage_fraction = min(1.0, covered / area_ha) if area_ha else 0.0
        bpr.state = AVAILABLE if bpr.coverage_fraction >= 1.0 - 1e-6 else PARTIAL
        span = sorted({y for area, _a in parts for y in area.stock_by_year})
        bpr.years = span
        bpr.period = f"{min(span)}–{max(span)}" if span else None
        bpr.quality = ", ".join(sorted({area.aoi_id for area, _a in parts}))
        bpr.version = next(iter({area.baseline_id for area, _a in parts}), "")
    else:
        bpr.note = "Контур вне покрытия таблицы базовой линии"
    out.baseline = bpr

    # -- live source probe -------------------------------------------------
    if probe_live and refl is not None:
        info = stac.probe()
        refl.live = info
        if not info.get("reachable"):
            out.warnings.append(
                {
                    "code": SOURCE_UNREACHABLE,
                    "message": (
                        "Живой источник Sentinel-2 (Earth Search STAC) недоступен из этой "
                        "среды. Анализ выполняется по сохранённому набору — это "
                        "предусмотренный режим воспроизведения."
                    ),
                    "detail": str(info.get("error", "")),
                }
            )

    # -- what will be refused ---------------------------------------------
    if cci is None or cci.state == UNAVAILABLE:
        out.blocked.append(
            {
                "what": "Запас углерода, его изменение, E и e",
                "code": NO_BIOMASS_DATA,
                "why": "Для контура нет карт биомассы — расчёт запаса невозможен",
            }
        )
        out.blocked.append(
            {
                "what": "Потенциальные единицы",
                "code": NO_BIOMASS_DATA,
                "why": "Нет результата проекта, сравнивать с базовой линией нечего",
            }
        )
    elif cci.coverage_fraction < 1.0 - 1e-9:
        out.blocked.append(
            {
                "what": "Потенциальные единицы",
                "code": UNITS_UNAVAILABLE_PARTIAL_COVERAGE,
                "why": (
                    f"Покрытие биомассой {cci.coverage_fraction * 100:.1f} % — по правилу "
                    "кейса при неполном покрытии единицы не рассчитываются"
                ),
            }
        )
        out.warnings.append(
            {
                "code": PARTIAL_COVERAGE,
                "message": (
                    f"Данные о биомассе покрывают {cci.coverage_fraction * 100:.1f} % контура; "
                    "рассчитанная площадь и пропуски будут показаны отдельно"
                ),
            }
        )

    if bpr.state != AVAILABLE:
        out.blocked.append(
            {
                "what": "Потенциальные единицы",
                "code": OUT_OF_BASELINE_COVERAGE,
                "why": bpr.note or "Базовая линия покрывает контур не полностью",
            }
        )

    modis = next((p for p in out.products if p.product == MODIS_BURN), None)
    if modis is None or modis.state == UNAVAILABLE:
        out.warnings.append(
            {
                "code": "CAUSE_UNDETERMINED",
                "message": (
                    "Продукт гарей для этого контура отсутствует: причина изменений "
                    "сможет получить статус «не установлена» — это штатный исход, "
                    "а не ошибка"
                ),
            }
        )

    if out.uses_fixtures:
        out.fixture_notice = _fixture_notice()
    return out


def _fixture_notice() -> Dict[str, object]:
    import json
    import os

    path = os.path.join(get_settings().fixture_root(), "FIXTURE_NOTICE.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            pass
    return {
        "kind": "SYNTHETIC FIXTURE",
        "warning": "Часть растров синтетическая; значения пикселей не являются наблюдениями.",
    }
