"""Uncertainty: five separate layers, one of which produces L and U.

The case asks how input errors reach the stock difference and the territory
total, and requires that spatial and temporal dependence be either accounted for
or investigated.  It also forbids presenting a scenario range as an empirically
calibrated interval.  Both are taken literally.

Layer 2 is the only one that moves L and U.  It propagates the product's own
per-pixel standard deviation `AGB_SD` through the weighted sum:

    Var(C_t) = CF^2 * sum_i sum_j a_i a_j rho(d_ij) sigma_i sigma_j
    Var(dC)  = Var(C_t1) + Var(C_t0) - 2 rho_t sqrt(Var(C_t1) Var(C_t0))
    sigma(E) = (44/12) sqrt(Var(dC))
    L, U     = E -+ k sigma(E)

`rho` and `rho_t` are *chosen*, not measured, so the payload carries
`interval_kind = "scenario"` and the list of assumptions.  Three bracketing
scenarios are always computed, because the spread between independent and fully
correlated errors changes H by an order of magnitude and therefore decides
whether any units are issued at all.

The other four layers are reported as their own numbers and never folded into
the interval: data quality, spatial coverage, temporal coverage and the strength
of causal evidence.  The optional composite VRI is an ordinal triage index with
published weights, and no branch of the code reads it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from ..core.config import get_settings
from ..core.status import OK, Quantity, Status
from ..geo.ellipsoid import meters_per_degree

INTERVAL_KIND = "scenario"


@dataclass
class VarianceScenario:
    id: str
    label: str
    spatial_model: str
    correlation_length_m: Optional[float]
    temporal_correlation: float
    sigma_e: float
    lower: float
    upper: float
    h: float

    def as_dict(self) -> Dict[str, object]:
        return {
            "id": self.id,
            "label": self.label,
            "spatial_model": self.spatial_model,
            "correlation_length_m": self.correlation_length_m,
            "temporal_correlation": self.temporal_correlation,
            "sigma_e": self.sigma_e,
            "L": self.lower,
            "U": self.upper,
            "H": self.h,
        }


@dataclass
class UncertaintyLayer:
    id: str
    title: str
    score: float                  # 0..1, higher is better
    headline: str
    details: List[Dict[str, object]] = field(default_factory=list)
    affects_interval: bool = False

    def as_dict(self) -> Dict[str, object]:
        return {
            "id": self.id,
            "title": self.title,
            "score": self.score,
            "headline": self.headline,
            "details": self.details,
            "affects_interval": self.affects_interval,
        }


@dataclass
class UncertaintyResult:
    interval_kind: str = INTERVAL_KIND
    k_multiplier: float = 1.0
    active_scenario: str = ""
    sigma_e: Optional[float] = None
    lower: Quantity = field(default_factory=lambda: Quantity("L", unit="т CO2-экв."))
    upper: Quantity = field(default_factory=lambda: Quantity("U", unit="т CO2-экв."))
    h: Quantity = field(default_factory=lambda: Quantity("H", unit="т CO2-экв."))
    scenarios: List[VarianceScenario] = field(default_factory=list)
    layers: List[UncertaintyLayer] = field(default_factory=list)
    assumptions: List[str] = field(default_factory=list)
    vri: Optional[float] = None
    vri_note: str = ""
    cross_check: Optional[Dict[str, object]] = None

    def as_dict(self) -> Dict[str, object]:
        return {
            "interval_kind": self.interval_kind,
            "interval_kind_note": (
                "Сценарный диапазон при заданных допущениях о корреляции ошибок. "
                "Это не эмпирически откалиброванный доверительный интервал: "
                "распределение ошибки продуктом не заявлено, независимых наземных "
                "измерений в наборе нет."
            ),
            "k_multiplier": self.k_multiplier,
            "active_scenario": self.active_scenario,
            "sigma_e": self.sigma_e,
            "L": self.lower.as_dict(),
            "U": self.upper.as_dict(),
            "H": self.h.as_dict(),
            "scenarios": [s.as_dict() for s in self.scenarios],
            "layers": [l.as_dict() for l in self.layers],
            "assumptions": self.assumptions,
            "vri": self.vri,
            "vri_note": self.vri_note,
            "cross_check": self.cross_check,
        }


# ---------------------------------------------------------------------------
# variance of a weighted sum
# ---------------------------------------------------------------------------
def _pixel_coordinates_m(
    rows: np.ndarray, cols: np.ndarray, grid, lat0: float
) -> Tuple[np.ndarray, np.ndarray]:
    mlon, mlat = meters_per_degree(lat0)
    x = (grid.origin_x + (cols + 0.5) * grid.dx) * mlon
    y = (grid.origin_y + (rows + 0.5) * grid.dy) * mlat
    return x, y


def weighted_sum_variance(
    values_sigma: np.ndarray,
    weights: np.ndarray,
    grid,
    row0: int,
    col0: int,
    model: str,
    correlation_length_m: Optional[float],
    lat0: float,
    block: int = 512,
) -> float:
    """Var(sum_i a_i x_i) for a chosen correlation model, in (t d.m.)^2."""
    sel = weights > 0
    if not sel.any():
        return 0.0
    v = (weights[sel] * values_sigma[sel]).astype(np.float64)
    if model == "independent":
        return float(np.sum(v * v))
    if model == "fully_correlated":
        s = float(np.sum(v))
        return s * s
    if model != "exponential":
        raise ValueError(f"unknown spatial model {model!r}")

    rr, cc = np.nonzero(sel)
    x, y = _pixel_coordinates_m(rr + row0, cc + col0, grid, lat0)
    n = v.size
    L = float(correlation_length_m or 1000.0)
    total = 0.0
    for start in range(0, n, block):
        stop = min(n, start + block)
        dx = x[start:stop, None] - x[None, :]
        dy = y[start:stop, None] - y[None, :]
        d = np.sqrt(dx * dx + dy * dy)
        rho = np.exp(-d / L)
        total += float(v[start:stop] @ (rho @ v))
    return total


def difference_variance(var_start: float, var_end: float, rho_t: float) -> float:
    cov = rho_t * math.sqrt(max(0.0, var_start) * max(0.0, var_end))
    return max(0.0, var_start + var_end - 2.0 * cov)


# ---------------------------------------------------------------------------
# the public entry point
# ---------------------------------------------------------------------------
def compute_uncertainty(
    e_value: Optional[float],
    sd_start: np.ndarray,
    sd_end: np.ndarray,
    weights: np.ndarray,
    grid,
    row0: int,
    col0: int,
    lat0: float,
    cf: float,
    co2_per_c: float,
    layers: Sequence[UncertaintyLayer],
    change_product: Optional[Dict[str, object]] = None,
    extra_scenarios: Optional[Sequence[Dict[str, object]]] = None,
) -> UncertaintyResult:
    cfg = get_settings().methods["uncertainty"]
    res = UncertaintyResult(k_multiplier=float(cfg.get("k_multiplier", 1.0)))
    res.layers = list(layers)
    res.assumptions = [
        "Границы получены переносом канала AGB_SD продукта ESA CCI Biomass.",
        f"Модель пространственной корреляции: {cfg.get('spatial_model')}, "
        f"L = {cfg.get('correlation_length_m')} м (параметр конфигурации, не измерение).",
        f"Временная корреляция ошибок ρ_t = {cfg.get('temporal_correlation')} "
        "(консервативное допущение: ошибки двух лет не гасят друг друга).",
        f"Множитель k = {res.k_multiplier}; это не квантиль распределения.",
        "Базовая линия и утечка зафиксированы условиями кейса, их сценарная "
        "неопределённость в L и U не входит.",
    ]

    if e_value is None or not np.any(weights > 0):
        return res

    scenarios: List[VarianceScenario] = []
    for sc in list(cfg.get("scenarios", [])) + list(extra_scenarios or []):
        model = sc.get("spatial_model", "independent")
        length = sc.get("correlation_length_m")
        rho_t = float(sc.get("temporal_correlation", 0.0))
        v0 = weighted_sum_variance(sd_start, weights, grid, row0, col0, model, length, lat0)
        v1 = weighted_sum_variance(sd_end, weights, grid, row0, col0, model, length, lat0)
        var_d = difference_variance(v0, v1, rho_t)
        sigma_e = cf * co2_per_c * math.sqrt(var_d)
        h = res.k_multiplier * sigma_e
        scenarios.append(
            VarianceScenario(
                id=sc.get("id", model),
                label=sc.get("label", model),
                spatial_model=model,
                correlation_length_m=length,
                temporal_correlation=rho_t,
                sigma_e=sigma_e,
                lower=e_value - h,
                upper=e_value + h,
                h=h,
            )
        )
    res.scenarios = scenarios

    active_model = cfg.get("spatial_model", "exponential")
    active_length = cfg.get("correlation_length_m")
    active = next(
        (
            s
            for s in scenarios
            if s.spatial_model == active_model
            and (s.correlation_length_m or 0) == (active_length or 0)
        ),
        scenarios[0] if scenarios else None,
    )
    if active is None:
        return res

    res.active_scenario = active.id
    res.sigma_e = active.sigma_e
    res.lower = Quantity("L", active.lower, "т CO2-экв.", status=Status(OK))
    res.upper = Quantity("U", active.upper, "т CO2-экв.", status=Status(OK))
    res.h = Quantity("H", active.h, "т CO2-экв.", status=Status(OK))

    if change_product:
        res.cross_check = change_product

    weights_cfg = get_settings().methods.get("vri_weights", {})
    total_w = 0.0
    acc = 0.0
    for layer in res.layers:
        w = float(weights_cfg.get(layer.id, 0.0))
        if w > 0:
            acc += w * max(0.0, min(1.0, layer.score))
            total_w += w
    if total_w > 0:
        res.vri = acc / total_w
        res.vri_note = (
            "Порядковая свёртка пяти слоёв с экспертными весами из config/methods.json. "
            "Это не вероятность: шкала не откалибрована ни по какой выборке верных и "
            "неверных вердиктов, слои не независимы, и ни одно решение в коде по ней "
            "не принимается."
        )
    return res


def reconcile_with_change_product(
    our_sigma_delta_c: float,
    esa_sigma_delta_c: float,
    var_start: float,
    var_end: float,
    cf: float,
) -> Dict[str, object]:
    """Solve for the temporal correlation implied by ESA's own difference SD.

    The dataset contains exactly one file where the product authors state the
    uncertainty of a *difference* rather than of two states: CCI_Change for
    2019->2020.  Matching our propagation to it gives a rho_t that is consistent
    with the source instead of assumed.  This is a reconciliation of an
    assumption with what the source itself declares -- not an empirical
    calibration, because one satellite product does not validate another.
    """
    target_var = (esa_sigma_delta_c / cf) ** 2 if cf else 0.0
    denom = 2.0 * math.sqrt(max(1e-30, var_start * var_end))
    rho = (var_start + var_end - target_var) / denom if denom > 0 else float("nan")
    return {
        "our_sigma_delta_c_tc": our_sigma_delta_c,
        "esa_sigma_delta_c_tc": esa_sigma_delta_c,
        "implied_rho_t": rho if math.isfinite(rho) else None,
        "implied_rho_t_clipped": max(-1.0, min(1.0, rho)) if math.isfinite(rho) else None,
        "note": (
            "ρ_t, согласованный с каналом AGB_difference_SD продукта ESA за 2019→2020. "
            "Сопоставление спутниковых продуктов не является наземной валидацией."
        ),
    }


# ---------------------------------------------------------------------------
# the four layers that do not touch L and U
# ---------------------------------------------------------------------------
def layer_data_quality(
    years: Sequence[int], selected: Dict[int, object], scenes: Sequence[object]
) -> UncertaintyLayer:
    covered = [y for y in years if selected.get(y) is not None]
    share = len(covered) / len(years) if years else 0.0
    usable = [getattr(s, "usable_fraction", 0.0) for s in scenes]
    mean_usable = float(np.mean(usable)) if usable else 0.0
    baselines = sorted({getattr(s, "processing_baseline", "") for s in scenes} - {""})
    details = [
        {"label": "Лет с пригодной сценой", "value": f"{len(covered)} из {len(years)}"},
        {"label": "Средняя доля годных пикселей по сценам", "value": f"{mean_usable * 100:.1f} %"},
    ]
    if len(baselines) > 1:
        details.append(
            {
                "label": "Версии обработки Sentinel-2",
                "value": ", ".join(baselines),
                "warning": "Радиометрическая неоднородность: смещение читается из каждой сцены отдельно",
            }
        )
    return UncertaintyLayer(
        id="data_quality",
        title="Качество данных",
        score=0.6 * share + 0.4 * mean_usable,
        headline=f"{len(covered)} из {len(years)} лет имеют пригодное наблюдение",
        details=details,
    )


def layer_model_error(sigma_e: Optional[float], e_value: Optional[float]) -> UncertaintyLayer:
    if sigma_e is None or not e_value:
        rel = None
        score = 0.3
        headline = "Перенос ошибки продукта не выполнен"
    else:
        rel = abs(sigma_e / e_value) if e_value else None
        score = max(0.0, 1.0 - min(1.0, (rel or 1.0)))
        headline = f"σ(E) = {sigma_e:,.0f} т CO2-экв. ({(rel or 0) * 100:.0f} % от результата)"
    return UncertaintyLayer(
        id="model_error",
        title="Ошибка продукта и модели",
        score=score,
        headline=headline.replace(",", " "),
        details=[
            {
                "label": "Источник",
                "value": "канал AGB_SD продукта ESA CCI Biomass v7.0",
            },
            {
                "label": "Относительная ширина",
                "value": f"{(rel or 0) * 100:.1f} %" if rel is not None else "—",
            },
        ],
        affects_interval=True,
    )


def layer_spatial_coverage(
    coverage_fraction: float, partial_share: float, area_gap_ha: float
) -> UncertaintyLayer:
    return UncertaintyLayer(
        id="spatial_coverage",
        title="Пространственное покрытие",
        score=max(0.0, min(1.0, coverage_fraction)),
        headline=f"Рассчитано {coverage_fraction * 100:.2f} % площади запроса",
        details=[
            {"label": "Пропуски", "value": f"{area_gap_ha:.2f} га"},
            {
                "label": "Доля площади в краевых пикселях",
                "value": f"{partial_share * 100:.1f} %",
            },
        ],
    )


def layer_temporal_coverage(
    window_days: Optional[int], years_without_obs: Sequence[int], delta_years: int
) -> UncertaintyLayer:
    if window_days is None:
        score = 0.35
        headline = "Изменений для датировки не найдено"
    else:
        score = max(0.0, 1.0 - min(1.0, window_days / 365.0))
        headline = f"Самое узкое окно датировки: {window_days} дн."
    return UncertaintyLayer(
        id="temporal_coverage",
        title="Временное покрытие",
        score=score,
        headline=headline,
        details=[
            {"label": "Длительность периода", "value": f"{delta_years} лет"},
            {
                "label": "Годы без пригодных наблюдений",
                "value": ", ".join(str(y) for y in years_without_obs) or "нет",
            },
            {
                "label": "Оговорка",
                "value": "CCI даёт годовое состояние, а не дату; Δt — целое число лет",
            },
        ],
    )


def layer_causal_evidence(
    share_confirmed: float, rung_counts: Dict[str, int]
) -> UncertaintyLayer:
    return UncertaintyLayer(
        id="causal_evidence",
        title="Сила причинных свидетельств",
        score=max(0.0, min(1.0, share_confirmed)),
        headline=(
            f"{share_confirmed * 100:.0f} % изменения углерода приходится на очаги "
            "с подтверждённой причиной"
        ),
        details=[
            {"label": f"Ступень {k}", "value": str(v)} for k, v in sorted(rung_counts.items())
        ]
        or [{"label": "Очагов не выделено", "value": "0"}],
    )
