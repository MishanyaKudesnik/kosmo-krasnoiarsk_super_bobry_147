"""Potential carbon units under the scenario rules of the case.

Steps 12-18:

    R      = E_base - E_proj - LK
    H      = max(U - E_proj, E_proj - L)
    UNC    = max(0, H/R - allowance)        allowance = 0.10
    R_adj  = R * (1 - UNC)
    B      = buffer_share * R_adj           buffer_share = 0.15
    Q      = floor(R_adj - B)
    V      = Q * price

Every branch the case names explicitly is a branch here, and each one carries a
status code rather than a silent zero:

* inputs incomplete  -> units unavailable
* coverage below 1   -> units unavailable, by the rule of the case
* R <= 0             -> Q = 0 and H/R is *not* computed
* R > 0 and H/R >= 1 -> Q = 0
* H/R <= allowance   -> no deduction

The worked example of the case statement (100 ha, one year, 100 -> 104 t/ha) is
reproduced to the last decimal by `tests/test_case_example.py`, which is the
first test in the repository.

Nothing here is a certified credit.  The result is a scenario calculation under
the conditions of the case, and every payload says so.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ..core.status import (
    NO_BIOMASS_DATA,
    OK,
    RESULT_NOT_ABOVE_BASELINE,
    Quantity,
    Status,
    UNCERTAINTY_EXCEEDS_RESULT,
    UNITS_UNAVAILABLE_NO_BASELINE,
    UNITS_UNAVAILABLE_PARTIAL_COVERAGE,
)

DISCLAIMER = (
    "Расчёт по условиям кейса. Это не сертифицированные единицы и не "
    "подтверждение дополнительности проекта."
)


@dataclass
class UnitsResult:
    r: Quantity = field(default_factory=lambda: Quantity("R", unit="т CO2-экв."))
    h: Quantity = field(default_factory=lambda: Quantity("H", unit="т CO2-экв."))
    h_over_r: Quantity = field(default_factory=lambda: Quantity("H/R", unit="доля"))
    unc: Quantity = field(default_factory=lambda: Quantity("UNC", unit="доля"))
    r_adj: Quantity = field(default_factory=lambda: Quantity("R_adj", unit="т CO2-экв."))
    buffer: Quantity = field(default_factory=lambda: Quantity("B", unit="т CO2-экв."))
    q: Quantity = field(default_factory=lambda: Quantity("Q", unit="потенциальных единиц"))
    remainder: Optional[float] = None
    value_scenarios: List[Dict[str, object]] = field(default_factory=list)
    status: Status = field(default_factory=Status)
    disclaimer: str = DISCLAIMER
    inputs: Dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, object]:
        return {
            "R": self.r.as_dict(),
            "H": self.h.as_dict(),
            "H_over_R": self.h_over_r.as_dict(),
            "UNC": self.unc.as_dict(),
            "R_adj": self.r_adj.as_dict(),
            "B": self.buffer.as_dict(),
            "Q": self.q.as_dict(),
            "remainder_not_in_q": self.remainder,
            "value_scenarios": self.value_scenarios,
            "status": self.status.as_dict(),
            "disclaimer": self.disclaimer,
            "inputs": self.inputs,
        }


def compute_units(
    e_proj: Optional[float],
    e_base: Optional[float],
    lower: Optional[float],
    upper: Optional[float],
    leakage: float,
    allowance: float,
    stop_ratio: float,
    buffer_share: float,
    coverage_fraction: float,
    prices: Optional[List[Dict[str, object]]] = None,
    baseline_available: bool = True,
    coverage_epsilon: float = 1e-9,
) -> UnitsResult:
    res = UnitsResult()
    res.inputs = {
        "E_proj": e_proj,
        "E_base": e_base,
        "L": lower,
        "U": upper,
        "LK": leakage,
        "UNC_allowance": allowance,
        "UNC_stop_ratio": stop_ratio,
        "BUF": buffer_share,
        "coverage_fraction": coverage_fraction,
    }

    def refuse(reasons: List[Tuple[str, Optional[str]]]) -> UnitsResult:
        """Refuse with the first reason, but carry the rest of them along.

        Several preconditions usually fail together -- a contour that leaves the
        data also leaves the baseline table.  Reporting only the first one makes
        the service look as though it misread the request ("no baseline row" for
        a contour that has one, covering half of it).  The primary reason is the
        one furthest upstream; the others travel in the status context so the
        screen can list everything the user would have to fix.
        """
        code, detail = reasons[0]
        st = Status(code, detail)
        if len(reasons) > 1:
            st.context["also"] = [
                {"code": c, "detail": d} for c, d in reasons[1:]
            ]
        res.status = st
        for q in (res.r, res.h, res.h_over_r, res.unc, res.r_adj, res.buffer, res.q):
            q.value = None
            q.status = st
        return res

    # --- preconditions from the case -------------------------------------
    # Ordered from the ground up: what the data covers, then what was computed
    # from it, then what it is compared against.
    reasons: List[Tuple[str, Optional[str]]] = []
    if coverage_fraction <= coverage_epsilon:
        # No data at all is not "partial coverage of 0.0 %": that phrasing reads
        # like a rounding accident rather than the plain fact.
        reasons.append((NO_BIOMASS_DATA, None))
    elif coverage_fraction < 1.0 - coverage_epsilon:
        reasons.append((
            UNITS_UNAVAILABLE_PARTIAL_COVERAGE,
            f"Покрытие контура данными {coverage_fraction * 100:.1f} %",
        ))
    if e_proj is None:
        reasons.append((UNITS_UNAVAILABLE_NO_BASELINE, "Нет результата проекта"))
    if not baseline_available or e_base is None:
        reasons.append((UNITS_UNAVAILABLE_NO_BASELINE, None))
    if e_proj is not None and (lower is None or upper is None):
        reasons.append((
            UNITS_UNAVAILABLE_NO_BASELINE, "Диапазон неопределённости не рассчитан"
        ))
    if not reasons and not all(
        math.isfinite(v) for v in (e_proj, e_base, lower, upper)
    ):
        reasons.append((UNITS_UNAVAILABLE_NO_BASELINE, "Недопустимые входные значения"))
    if reasons:
        return refuse(reasons)

    # --- R ----------------------------------------------------------------
    r_value = e_base - e_proj - leakage
    res.r = Quantity(
        "R",
        r_value,
        "т CO2-экв. за период",
        status=Status(OK),
        sign_convention="положительное = результат лучше базовой линии",
    )

    h_value = max(upper - e_proj, e_proj - lower)
    if h_value < 0:
        h_value = 0.0
    res.h = Quantity("H", h_value, "т CO2-экв.", status=Status(OK))
    if h_value == 0.0:
        res.h.notes.append(
            "Нулевая ширина диапазона почти всегда означает ошибку во входных данных"
        )

    # --- R <= 0: Q = 0 and H/R is not computed ---------------------------
    if r_value <= 0:
        st = Status(RESULT_NOT_ABOVE_BASELINE)
        res.h_over_r = Quantity("H/R", None, "доля", status=st)
        res.unc = Quantity("UNC", None, "доля", status=st)
        res.r_adj = Quantity("R_adj", None, "т CO2-экв.", status=st)
        res.buffer = Quantity("B", None, "т CO2-экв.", status=st)
        res.q = Quantity("Q", 0.0, "потенциальных единиц", status=st)
        res.status = st
        res.value_scenarios = _values(0, prices)
        return res

    ratio = h_value / r_value
    res.h_over_r = Quantity("H/R", ratio, "доля", status=Status(OK))

    if ratio >= stop_ratio:
        st = Status(UNCERTAINTY_EXCEEDS_RESULT, context={"H_over_R": ratio})
        res.unc = Quantity("UNC", None, "доля", status=st)
        res.r_adj = Quantity("R_adj", None, "т CO2-экв.", status=st)
        res.buffer = Quantity("B", None, "т CO2-экв.", status=st)
        res.q = Quantity("Q", 0.0, "потенциальных единиц", status=st)
        res.status = st
        res.value_scenarios = _values(0, prices)
        return res

    unc = max(0.0, ratio - allowance)
    r_adj = r_value * (1.0 - unc)
    buf = buffer_share * r_adj
    net = r_adj - buf
    q = int(math.floor(net))
    q = max(0, q)

    res.unc = Quantity("UNC", unc, "доля", status=Status(OK))
    res.r_adj = Quantity("R_adj", r_adj, "т CO2-экв.", status=Status(OK))
    res.buffer = Quantity("B", buf, "т CO2-экв.", status=Status(OK))
    res.q = Quantity("Q", float(q), "потенциальных единиц", status=Status(OK))
    res.remainder = net - q
    res.value_scenarios = _values(q, prices)
    res.status = Status(OK)
    return res


def _values(q: int, prices: Optional[List[Dict[str, object]]]) -> List[Dict[str, object]]:
    out: List[Dict[str, object]] = []
    for p in prices or []:
        out.append(
            {
                "id": p.get("id"),
                "label": p.get("label"),
                "price": p.get("value"),
                "unit": p.get("unit"),
                "value": q * float(p.get("value", 0.0)),
                "note": "Заданные сценарные цены кейса, не прогноз рынка",
            }
        )
    return out
