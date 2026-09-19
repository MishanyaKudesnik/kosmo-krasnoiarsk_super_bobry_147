"""Carbon stock and the stock-difference method.

Steps 1-9 of the case pipeline live here and nothing else:

    b(i,t)  AGB, t d.m./ha        from the biomass product
    c(i,t)  = CF * b(i,t)         t C/ha
    a(i)    intersection area     ha, from geo.grid.pixel_weights
    A       = sum a(i)            ha
    C(t)    = sum a(i) * c(i,t)   t C
    dC      = C(t1) - C(t0)       t C
    E       = -dC * 44/12         t CO2e, positive means a loss from the pool
    e       = E / (A * dt)        t CO2e/ha/yr

Two rules from the case are enforced structurally rather than by convention:

* the same pixel set is used at both dates, including pixels that lost their
  cover, so `valid_mask` is an intersection and never a per-date mask;
* the accounted pool is carried on every quantity, so no number can be shown
  without it.

The module knows nothing about baselines, units or uncertainty.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

from ..core.status import (
    NO_BIOMASS_DATA,
    OK,
    PARTIAL_COVERAGE,
    Quantity,
    Status,
)

POOL = "живая надземная древесная биомасса"
SIGN_CONVENTION = "положительное значение = потеря углерода из учитываемого пула"


@dataclass
class StockState:
    """The stock at one date."""

    year: int
    total_tc: float
    mean_tc_ha: float
    area_ha: float
    valid_pixels: int
    agb_mean_t_ha: float

    def as_dict(self) -> Dict[str, object]:
        return {
            "year": self.year,
            "total_tc": self.total_tc,
            "mean_tc_ha": self.mean_tc_ha,
            "area_ha": self.area_ha,
            "valid_pixels": self.valid_pixels,
            "agb_mean_t_ha": self.agb_mean_t_ha,
        }


@dataclass
class CarbonResult:
    pool: str = POOL
    cf: float = 0.47
    co2_per_c: float = 44.0 / 12.0
    year_start: int = 0
    year_end: int = 0
    delta_years: int = 0
    area_requested_ha: float = 0.0
    area_valid_ha: float = 0.0
    area_gap_ha: float = 0.0
    coverage_fraction: float = 0.0
    state_start: Optional[StockState] = None
    state_end: Optional[StockState] = None
    delta_c_tc: Optional[float] = None
    e_total: Quantity = field(default_factory=lambda: Quantity("E", unit="т CO2-экв."))
    e_per_ha_year: Quantity = field(
        default_factory=lambda: Quantity("e", unit="т CO2-экв./га/год")
    )
    annual_series: List[Dict[str, object]] = field(default_factory=list)
    status: Status = field(default_factory=Status)

    def as_dict(self) -> Dict[str, object]:
        return {
            "pool": self.pool,
            "cf": self.cf,
            "co2_per_c": self.co2_per_c,
            "sign_convention": SIGN_CONVENTION,
            "year_start": self.year_start,
            "year_end": self.year_end,
            "delta_years": self.delta_years,
            "area_requested_ha": self.area_requested_ha,
            "area_valid_ha": self.area_valid_ha,
            "area_gap_ha": self.area_gap_ha,
            "coverage_fraction": self.coverage_fraction,
            "state_start": self.state_start.as_dict() if self.state_start else None,
            "state_end": self.state_end.as_dict() if self.state_end else None,
            "delta_c_tc": self.delta_c_tc,
            "E": self.e_total.as_dict(),
            "e_per_ha_year": self.e_per_ha_year.as_dict(),
            "annual_series": self.annual_series,
            "status": self.status.as_dict(),
        }


def stock_state(
    year: int, agb: np.ndarray, weights: np.ndarray, valid: np.ndarray, cf: float
) -> StockState:
    """Total and mean stock over the valid part of the contour."""
    w = np.where(valid, weights, 0.0)
    area = float(w.sum())
    if area <= 0:
        return StockState(year, 0.0, 0.0, 0.0, 0, 0.0)
    agb_f = agb.astype(np.float64)
    total_agb = float((agb_f * w).sum())
    total_tc = total_agb * cf
    return StockState(
        year=year,
        total_tc=total_tc,
        mean_tc_ha=total_tc / area,
        area_ha=area,
        valid_pixels=int(np.count_nonzero(w > 0)),
        agb_mean_t_ha=total_agb / area,
    )


def compute_carbon(
    year_start: int,
    year_end: int,
    agb_start: np.ndarray,
    agb_end: np.ndarray,
    weights: np.ndarray,
    valid_start: np.ndarray,
    valid_end: np.ndarray,
    cf: float,
    co2_per_c: float,
    area_requested_ha: float,
    annual: Optional[Dict[int, Tuple[np.ndarray, np.ndarray]]] = None,
) -> CarbonResult:
    """Run steps 1-9 for one contour and one period."""
    res = CarbonResult(
        pool=POOL,
        cf=cf,
        co2_per_c=co2_per_c,
        year_start=year_start,
        year_end=year_end,
        delta_years=year_end - year_start,
        area_requested_ha=area_requested_ha,
    )

    # identical boundaries at both dates, by construction
    valid = valid_start & valid_end & (weights > 0)
    area_valid = float(np.where(valid, weights, 0.0).sum())
    res.area_valid_ha = area_valid
    res.area_gap_ha = max(0.0, area_requested_ha - area_valid)
    res.coverage_fraction = area_valid / area_requested_ha if area_requested_ha > 0 else 0.0

    if area_valid <= 0:
        res.status = Status(NO_BIOMASS_DATA)
        res.e_total.status = Status(NO_BIOMASS_DATA)
        res.e_per_ha_year.status = Status(NO_BIOMASS_DATA)
        return res

    res.state_start = stock_state(year_start, agb_start, weights, valid, cf)
    res.state_end = stock_state(year_end, agb_end, weights, valid, cf)
    res.delta_c_tc = res.state_end.total_tc - res.state_start.total_tc

    e_total = -res.delta_c_tc * co2_per_c
    res.e_total = Quantity(
        name="E",
        value=e_total,
        unit="т CO2-экв. за период",
        pool=POOL,
        sign_convention=SIGN_CONVENTION,
        status=Status(OK),
    )
    dt = res.delta_years
    if dt > 0:
        res.e_per_ha_year = Quantity(
            name="e",
            value=e_total / (area_valid * dt),
            unit="т CO2-экв./га/год",
            pool=POOL,
            sign_convention=SIGN_CONVENTION,
            status=Status(OK),
        )

    if annual:
        for year in sorted(annual):
            arr, ok = annual[year]
            st = stock_state(year, arr, weights, valid & ok, cf)
            res.annual_series.append(
                {
                    "year": year,
                    "mean_tc_ha": st.mean_tc_ha,
                    "total_tc": st.total_tc,
                    "valid_pixels": st.valid_pixels,
                }
            )

    if res.coverage_fraction < 1.0 - 1e-9:
        res.status = Status(
            PARTIAL_COVERAGE,
            context={
                "area_valid_ha": area_valid,
                "area_gap_ha": res.area_gap_ha,
                "coverage_fraction": res.coverage_fraction,
            },
        )
    else:
        res.status = Status(OK)
    return res


def patch_contribution(
    mask: np.ndarray,
    agb_start: np.ndarray,
    agb_end: np.ndarray,
    weights: np.ndarray,
    cf: float,
    co2_per_c: float,
) -> Dict[str, float]:
    """The share of the total result produced by one set of pixels."""
    w = np.where(mask, weights, 0.0)
    area = float(w.sum())
    d_agb = agb_end.astype(np.float64) - agb_start.astype(np.float64)
    delta_c = float((d_agb * w).sum()) * cf
    return {
        "area_ha": area,
        "delta_c_tc": delta_c,
        "e_tco2e": -delta_c * co2_per_c,
        "mean_delta_agb_t_ha": float((d_agb * w).sum() / area) if area > 0 else 0.0,
    }
