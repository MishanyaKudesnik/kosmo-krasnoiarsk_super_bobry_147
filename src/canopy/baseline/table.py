"""The common baseline of the case, applied to an arbitrary contour.

`data/methodology/baseline.csv` gives, per area and per year, a stock per hectare
for the scenario without a project.  The case adds two rules that this module
implements literally:

* for a sub-area, the parent area's per-hectare trajectory is applied to the
  *actual* area of the request;
* when a request crosses several areas, the results for the parts are summed.

Coverage is decided geometrically, never by an identifier: the request polygon
is clipped by each known area and the geodesic area of each piece is what the
trajectory is multiplied by.  A request that reaches outside every known area
gets `OUT_OF_BASELINE_COVERAGE` for the part that falls outside, and the unit
calculation is then refused rather than approximated.
"""

from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ..core.config import Settings, get_settings
from ..core.status import OK, OUT_OF_BASELINE_COVERAGE, Quantity, Status
from ..geo.ellipsoid import polygon_area_m2
from ..geo.polygon import MultiPolygon, clip_polygon_to_rect, multipolygon_bbox


@dataclass
class BaselineArea:
    aoi_id: str
    name: str
    region: str
    baseline_id: str
    west: float
    south: float
    east: float
    north: float
    declared_area_ha: float
    role: str
    historical_rate_tc_ha_yr: float
    reference_2015: float
    reference_2019: float
    stock_by_year: Dict[int, float] = field(default_factory=dict)

    def stock(self, year: int) -> Optional[float]:
        return self.stock_by_year.get(year)

    def as_dict(self) -> Dict[str, object]:
        return {
            "aoi_id": self.aoi_id,
            "name": self.name,
            "region": self.region,
            "baseline_id": self.baseline_id,
            "role": self.role,
            "declared_area_ha": self.declared_area_ha,
            "historical_rate_tc_ha_yr": self.historical_rate_tc_ha_yr,
            "reference_mean_2015_tc_ha": self.reference_2015,
            "reference_mean_2019_tc_ha": self.reference_2019,
            "years": sorted(self.stock_by_year),
            "bbox": [self.west, self.south, self.east, self.north],
        }


@dataclass
class BaselinePart:
    aoi_id: str
    area_ha: float
    stock_start_tc_ha: float
    stock_end_tc_ha: float
    delta_tc_ha: float
    delta_c_tc: float
    e_base_tco2e: float

    def as_dict(self) -> Dict[str, object]:
        return {
            "aoi_id": self.aoi_id,
            "area_ha": self.area_ha,
            "stock_start_tc_ha": self.stock_start_tc_ha,
            "stock_end_tc_ha": self.stock_end_tc_ha,
            "delta_tc_ha": self.delta_tc_ha,
            "delta_c_tc": self.delta_c_tc,
            "e_base_tco2e": self.e_base_tco2e,
        }


@dataclass
class BaselineResult:
    parts: List[BaselinePart] = field(default_factory=list)
    covered_area_ha: float = 0.0
    requested_area_ha: float = 0.0
    uncovered_area_ha: float = 0.0
    e_base: Quantity = field(default_factory=lambda: Quantity("E_base", unit="т CO2-экв."))
    status: Status = field(default_factory=Status)
    variant: str = "case"
    variant_label: str = "Общая базовая линия кейса"
    counts_for_units: bool = True

    @property
    def fully_covered(self) -> bool:
        return self.uncovered_area_ha <= 1e-6 * max(1.0, self.requested_area_ha)

    def as_dict(self) -> Dict[str, object]:
        return {
            "variant": self.variant,
            "variant_label": self.variant_label,
            "counts_for_units": self.counts_for_units,
            "parts": [p.as_dict() for p in self.parts],
            "covered_area_ha": self.covered_area_ha,
            "requested_area_ha": self.requested_area_ha,
            "uncovered_area_ha": self.uncovered_area_ha,
            "fully_covered": self.fully_covered,
            "E_base": self.e_base.as_dict(),
            "status": self.status.as_dict(),
        }


class BaselineTable:
    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        self.areas: Dict[str, BaselineArea] = {}
        self._load()

    def _load(self) -> None:
        areas_csv = self.settings.methodology_path("areas.csv")
        if not os.path.exists(areas_csv):
            # The organisers' archive keeps areas.csv at the root of data/.
            areas_csv = self.settings.data_path("areas.csv")
        base_csv = self.settings.methodology_path("baseline.csv")
        meta: Dict[str, Dict[str, str]] = {}
        if os.path.exists(areas_csv):
            with open(areas_csv, "r", encoding="utf-8-sig", newline="") as fh:
                for row in csv.DictReader(fh):
                    meta[row["aoi_id"]] = row
        if not os.path.exists(base_csv):
            return
        with open(base_csv, "r", encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                aoi = row["aoi_id"]
                if aoi not in self.areas:
                    m = meta.get(aoi, {})
                    self.areas[aoi] = BaselineArea(
                        aoi_id=aoi,
                        name=m.get("name", aoi),
                        region=m.get("region", ""),
                        baseline_id=row.get("baseline_id", ""),
                        west=float(m.get("bbox_west", 0) or 0),
                        south=float(m.get("bbox_south", 0) or 0),
                        east=float(m.get("bbox_east", 0) or 0),
                        north=float(m.get("bbox_north", 0) or 0),
                        declared_area_ha=float(m.get("area_ha", 0) or 0),
                        role=m.get("selection_role", ""),
                        historical_rate_tc_ha_yr=float(row["historical_rate_tc_ha_yr"]),
                        reference_2015=float(row["reference_mean_2015_tc_ha"]),
                        reference_2019=float(row["reference_mean_2019_tc_ha"]),
                    )
                area = self.areas[aoi]
                y0 = int(row["year_start"])
                y1 = int(row["year_end"])
                area.stock_by_year[y0] = float(row["baseline_stock_start_tc_ha"])
                area.stock_by_year[y1] = float(row["baseline_stock_end_tc_ha"])

    # -- geometry ----------------------------------------------------------
    def parts_for(self, geometry: MultiPolygon) -> List[Tuple[BaselineArea, float]]:
        """Area in hectares of the request inside each known area."""
        out: List[Tuple[BaselineArea, float]] = []
        for area in self.areas.values():
            total = 0.0
            for poly in geometry:
                clipped = clip_polygon_to_rect(
                    poly, area.west, area.south, area.east, area.north
                )
                if clipped:
                    total += polygon_area_m2(clipped)
            if total > 0:
                out.append((area, total / 10000.0))
        return out

    def intersecting_ids(self, geometry: MultiPolygon) -> List[str]:
        return [a.aoi_id for a, _ in self.parts_for(geometry)]

    # -- the calculation ---------------------------------------------------
    def evaluate(
        self,
        geometry: MultiPolygon,
        year_start: int,
        year_end: int,
        requested_area_ha: float,
        co2_per_c: float,
        variant: str = "case",
        control_aoi: Optional[str] = None,
    ) -> BaselineResult:
        res = BaselineResult(requested_area_ha=requested_area_ha, variant=variant)
        res.counts_for_units = variant == "case"
        res.variant_label = {
            "case": "Общая базовая линия кейса",
            "flat": "Нулевая динамика (исследовательский сценарий)",
            "control": "Траектория контрольного участка (исследовательский сценарий)",
        }.get(variant, variant)

        parts = self.parts_for(geometry)
        if not parts:
            res.status = Status(OUT_OF_BASELINE_COVERAGE)
            res.e_base.status = Status(OUT_OF_BASELINE_COVERAGE)
            res.uncovered_area_ha = requested_area_ha
            return res

        control = self.areas.get(control_aoi) if control_aoi else None
        total_e = 0.0
        covered = 0.0
        for area, part_area_ha in parts:
            source = area
            if variant == "control" and control is not None:
                source = control
            s0 = source.stock(year_start)
            s1 = source.stock(year_end)
            if s0 is None or s1 is None:
                res.status = Status(
                    OUT_OF_BASELINE_COVERAGE,
                    detail=(
                        f"Базовая линия для {source.aoi_id} не покрывает "
                        f"{year_start}–{year_end}"
                    ),
                )
                res.e_base.status = res.status
                return res
            if variant == "flat":
                delta = 0.0
            else:
                delta = s1 - s0
            delta_c = delta * part_area_ha
            e_base = -delta_c * co2_per_c
            res.parts.append(
                BaselinePart(
                    aoi_id=area.aoi_id,
                    area_ha=part_area_ha,
                    stock_start_tc_ha=s0,
                    stock_end_tc_ha=s1 if variant != "flat" else s0,
                    delta_tc_ha=delta,
                    delta_c_tc=delta_c,
                    e_base_tco2e=e_base,
                )
            )
            total_e += e_base
            covered += part_area_ha

        res.covered_area_ha = covered
        res.uncovered_area_ha = max(0.0, requested_area_ha - covered)
        if not res.fully_covered:
            res.status = Status(
                OUT_OF_BASELINE_COVERAGE,
                context={
                    "covered_area_ha": covered,
                    "uncovered_area_ha": res.uncovered_area_ha,
                },
            )
            res.e_base.status = res.status
            return res

        res.e_base = Quantity(
            name="E_base",
            value=total_e,
            unit="т CO2-экв. за период",
            sign_convention="положительное = потеря по сценарию без проекта",
            status=Status(OK),
        )
        res.status = Status(OK)
        return res

    def control_candidates(self) -> List[str]:
        return [a.aoi_id for a in self.areas.values() if "контрол" in a.role.lower()]

    def as_dict(self) -> Dict[str, object]:
        return {aid: a.as_dict() for aid, a in self.areas.items()}


_TABLE: Optional[BaselineTable] = None


def get_baseline_table() -> BaselineTable:
    global _TABLE
    if _TABLE is None:
        _TABLE = BaselineTable()
    return _TABLE
