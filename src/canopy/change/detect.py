"""Where the forest changed, when, how much, and how much carbon it moved.

The heaviest criterion of the case asks six things of every detected area: where
it is, in what date window it happened, how large it is, whether other sources
confirm it, how it relates to biomass, and what share of the total carbon result
it produced.  This module answers all six and refuses to answer more.

Detection is thresholded in units of the product's own error rather than in
tonnes, so the threshold follows the data quality instead of a guess:

    changed  <=>  |db_i| > k * sqrt(sd0^2 + sd1^2)  and  |db_i| > floor

That is a *scaled threshold*, not a significance level: CCI declares no error
distribution and neighbouring pixels are correlated.  The wording is kept honest
everywhere it is shown.

Patches smaller than the configured minimum do not become objects on the map,
but their carbon is never discarded -- it is reported as diffuse change, so the
sum of the patch contributions plus the diffuse remainder is exactly the total.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from ..core.config import get_settings
from ..geo.grid import RasterGrid
from ..geo.polygon import MultiPolygon, trace_cells

LOSS = "loss"
GAIN = "gain"


@dataclass
class DateWindow:
    start: Optional[str] = None
    end: Optional[str] = None
    source: str = ""
    note: str = ""

    @property
    def days(self) -> Optional[int]:
        if not self.start or not self.end:
            return None
        return (date.fromisoformat(self.end) - date.fromisoformat(self.start)).days

    def as_dict(self) -> Dict[str, object]:
        return {
            "start": self.start,
            "end": self.end,
            "days": self.days,
            "source": self.source,
            "note": self.note,
        }


@dataclass
class Patch:
    patch_id: str
    kind: str
    cells: List[Tuple[int, int]]
    area_ha: float
    delta_agb_t_ha: float
    delta_c_tc: float
    e_tco2e: float
    contribution_share: float
    polygon: MultiPolygon = field(default_factory=list)
    centroid: Tuple[float, float] = (0.0, 0.0)
    area_gfc_ha: Optional[float] = None
    windows: List[DateWindow] = field(default_factory=list)
    final_window: DateWindow = field(default_factory=DateWindow)
    narrowing_source: Optional[str] = None
    narrowing_days: Optional[int] = None
    evidence: List[Dict[str, object]] = field(default_factory=list)
    rung: str = "D"
    rung_label: str = ""
    missing_conditions: List[str] = field(default_factory=list)
    cause: str = "причина не установлена"
    conflict: bool = False

    def as_dict(self) -> Dict[str, object]:
        return {
            "patch_id": self.patch_id,
            "kind": self.kind,
            "area_ha": self.area_ha,
            "area_gfc_ha": self.area_gfc_ha,
            "pixels": len(self.cells),
            "delta_agb_t_ha": self.delta_agb_t_ha,
            "delta_c_tc": self.delta_c_tc,
            "e_tco2e": self.e_tco2e,
            "contribution_share": self.contribution_share,
            "centroid": list(self.centroid),
            "geometry": {
                "type": "MultiPolygon",
                "coordinates": [[[list(pt) for pt in ring] for ring in poly] for poly in self.polygon],
            },
            "windows": [w.as_dict() for w in self.windows],
            "final_window": self.final_window.as_dict(),
            "narrowing_source": self.narrowing_source,
            "narrowing_days": self.narrowing_days,
            "evidence": self.evidence,
            "rung": self.rung,
            "rung_label": self.rung_label,
            "missing_conditions": self.missing_conditions,
            "cause": self.cause,
            "conflict": self.conflict,
        }


@dataclass
class ChangeResult:
    patches: List[Patch] = field(default_factory=list)
    diffuse_e_tco2e: float = 0.0
    diffuse_share: float = 0.0
    total_e_tco2e: float = 0.0
    changed_area_ha: float = 0.0
    threshold_note: str = ""
    delta_map: Optional[np.ndarray] = None

    def as_dict(self) -> Dict[str, object]:
        return {
            "patches": [p.as_dict() for p in self.patches],
            "patch_count": len(self.patches),
            "diffuse_e_tco2e": self.diffuse_e_tco2e,
            "diffuse_share": self.diffuse_share,
            "total_e_tco2e": self.total_e_tco2e,
            "changed_area_ha": self.changed_area_ha,
            "threshold_note": self.threshold_note,
        }


# ---------------------------------------------------------------------------
def detect_mask(
    agb0: np.ndarray, agb1: np.ndarray, sd0: np.ndarray, sd1: np.ndarray, valid: np.ndarray
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    cfg = get_settings().methods["change_detection"]
    k = float(cfg["z_threshold_k"])
    floor = float(cfg["min_abs_change_t_ha"])
    d = agb1.astype(np.float64) - agb0.astype(np.float64)
    sigma = np.sqrt(sd0.astype(np.float64) ** 2 + sd1.astype(np.float64) ** 2)
    sigma = np.where(sigma > 0, sigma, np.inf)
    significant = (np.abs(d) > k * sigma) & (np.abs(d) > floor) & valid
    return d, significant & (d < 0), significant & (d > 0)


def label_components(mask: np.ndarray, connectivity: int = 8) -> List[List[Tuple[int, int]]]:
    """Connected components of a boolean mask, without scipy."""
    seen = np.zeros(mask.shape, dtype=bool)
    if connectivity == 8:
        nbrs = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]
    else:
        nbrs = [(-1, 0), (1, 0), (0, -1), (0, 1)]
    out: List[List[Tuple[int, int]]] = []
    h, w = mask.shape
    for r in range(h):
        for c in range(w):
            if not mask[r, c] or seen[r, c]:
                continue
            comp: List[Tuple[int, int]] = []
            q = deque([(r, c)])
            seen[r, c] = True
            while q:
                cr, cc = q.popleft()
                comp.append((cr, cc))
                for dr, dc in nbrs:
                    nr, ncl = cr + dr, cc + dc
                    if 0 <= nr < h and 0 <= ncl < w and mask[nr, ncl] and not seen[nr, ncl]:
                        seen[nr, ncl] = True
                        q.append((nr, ncl))
            out.append(comp)
    return out


def build_patches(
    agb0: np.ndarray,
    agb1: np.ndarray,
    sd0: np.ndarray,
    sd1: np.ndarray,
    weights: np.ndarray,
    valid: np.ndarray,
    grid: RasterGrid,
    row0: int,
    col0: int,
    cf: float,
    co2_per_c: float,
    total_e: float,
) -> ChangeResult:
    cfg = get_settings().methods["change_detection"]
    min_ha = float(cfg["min_patch_ha"])
    conn = int(cfg.get("connectivity", 8))

    d, loss_mask, gain_mask = detect_mask(agb0, agb1, sd0, sd1, valid)
    res = ChangeResult(total_e_tco2e=total_e, delta_map=d)
    res.threshold_note = (
        f"Пиксель считается изменившимся при |Δb| > {cfg['z_threshold_k']}·σ и "
        f"|Δb| > {cfg['min_abs_change_t_ha']} т/га. Это масштабированный порог по "
        "собственной ошибке продукта, а не уровень статистической значимости."
    )

    accounted = np.zeros(weights.shape, dtype=bool)
    idx = 0
    for kind, mask in ((LOSS, loss_mask), (GAIN, gain_mask)):
        for comp in label_components(mask, conn):
            cells_area = sum(float(weights[r, c]) for r, c in comp)
            if cells_area < min_ha:
                continue
            idx += 1
            m = np.zeros(weights.shape, dtype=bool)
            for r, c in comp:
                m[r, c] = True
            accounted |= m
            w = np.where(m, weights, 0.0)
            area = float(w.sum())
            delta_c = float((d * w).sum()) * cf
            e = -delta_c * co2_per_c
            abs_cells = [(r + row0, c + col0) for r, c in comp]
            polygon = trace_cells(abs_cells, grid.origin_x, grid.origin_y, grid.dx, grid.dy)
            cx = float(np.mean([grid.origin_x + (c + 0.5) * grid.dx for _r, c in abs_cells]))
            cy = float(np.mean([grid.origin_y + (r + 0.5) * grid.dy for r, _c in abs_cells]))
            res.patches.append(
                Patch(
                    patch_id=f"P{idx:03d}",
                    kind=kind,
                    cells=comp,
                    area_ha=area,
                    delta_agb_t_ha=float((d * w).sum() / area) if area else 0.0,
                    delta_c_tc=delta_c,
                    e_tco2e=e,
                    contribution_share=(e / total_e) if total_e else 0.0,
                    polygon=polygon,
                    centroid=(cx, cy),
                )
            )

    res.patches.sort(key=lambda p: -abs(p.e_tco2e))
    for i, p in enumerate(res.patches, 1):
        p.patch_id = f"P{i:03d}"

    res.changed_area_ha = sum(p.area_ha for p in res.patches)
    explained = sum(p.e_tco2e for p in res.patches)
    res.diffuse_e_tco2e = total_e - explained
    res.diffuse_share = (res.diffuse_e_tco2e / total_e) if total_e else 0.0
    return res


# ---------------------------------------------------------------------------
# dating
# ---------------------------------------------------------------------------
def _year_window(year: int) -> DateWindow:
    return DateWindow(f"{year}-01-01", f"{year}-12-31")


def window_from_cci(
    patch: Patch,
    annual: Dict[int, np.ndarray],
    weights: np.ndarray,
    year_start: int,
    year_end: int,
) -> DateWindow:
    """Narrow to the annual transition that carries most of the patch's change."""
    years = sorted(y for y in annual if year_start <= y <= year_end)
    if len(years) < 2:
        return DateWindow(
            f"{year_start}-01-01", f"{year_end}-12-31", "ESA CCI Biomass",
            "годовые состояния без внутригодового разрешения",
        )
    m = np.zeros(weights.shape, dtype=bool)
    for r, c in patch.cells:
        m[r, c] = True
    w = np.where(m, weights, 0.0)
    tot = w.sum()
    means = []
    for y in years:
        arr = annual[y].astype(np.float64)
        means.append(float((arr * w).sum() / tot) if tot else 0.0)
    steps = [means[i + 1] - means[i] for i in range(len(means) - 1)]
    if not steps:
        return DateWindow(f"{year_start}-01-01", f"{year_end}-12-31", "ESA CCI Biomass")
    pick = int(np.argmin(steps)) if patch.kind == LOSS else int(np.argmax(steps))
    y0, y1 = years[pick], years[pick + 1]
    return DateWindow(
        f"{y0}-01-01",
        f"{y1}-12-31",
        "ESA CCI Biomass",
        f"наибольший годовой шаг между состояниями {y0} и {y1}",
    )


def window_from_sentinel2(
    patch: Patch,
    scene_indices: List[Dict[str, object]],
    min_valid_in_patch: float = 0.2,
) -> Optional[DateWindow]:
    """Last scene before the spectral shift and first scene after it.

    Usability is judged *inside the patch*: a cloud over half the contour may
    leave the patch itself perfectly visible, and the opposite happens too.
    """
    usable = [s for s in scene_indices if s.get("patch_valid_share", 0.0) >= min_valid_in_patch]
    if len(usable) < 2:
        return None
    usable.sort(key=lambda s: s["date"])
    values = [s.get("patch_nbr") for s in usable]
    if any(v is None or not np.isfinite(v) for v in values):
        usable = [s for s, v in zip(usable, values) if v is not None and np.isfinite(v)]
        values = [s["patch_nbr"] for s in usable]
    if len(usable) < 2:
        return None

    drops = [values[i + 1] - values[i] for i in range(len(values) - 1)]
    pick = int(np.argmin(drops)) if patch.kind == LOSS else int(np.argmax(drops))
    if abs(drops[pick]) < 0.05:
        return None
    before = usable[pick]
    after = usable[pick + 1]
    return DateWindow(
        before["date"],
        after["date"],
        "Sentinel-2",
        f"между сценами {before['scene_id']} и {after['scene_id']}",
    )


def window_from_gfc(
    patch: Patch, lossyear_counts: Dict[int, int], year_start: int, year_end: int
) -> Optional[DateWindow]:
    years = {y: n for y, n in lossyear_counts.items() if year_start < y <= year_end}
    if not years:
        return None
    y = max(years, key=lambda k: years[k])
    w = _year_window(y)
    w.source = "Hansen GFC"
    w.note = f"год потери покрова {y} у {years[y]} пикселей очага"
    return w


def window_from_modis(burn: Optional[Dict[str, object]]) -> Optional[DateWindow]:
    if not burn or burn.get("cells", 0) <= 0:
        return None
    start = burn.get("first_date")
    end = burn.get("last_date")
    if not start or not end:
        return None
    unc = int(burn.get("uncertainty_days", 0) or 0)
    s = (date.fromisoformat(start) - timedelta(days=unc)).isoformat()
    e = (date.fromisoformat(end) + timedelta(days=unc)).isoformat()
    return DateWindow(s, e, "MODIS MCD64A1", f"с учётом заявленной погрешности ±{unc} дн.")


def intersect_windows(windows: Sequence[DateWindow]) -> Tuple[DateWindow, bool]:
    """Intersection of all non-empty windows; flags an empty intersection."""
    valid = [w for w in windows if w.start and w.end]
    if not valid:
        return DateWindow(), False
    start = max(date.fromisoformat(w.start) for w in valid)
    end = min(date.fromisoformat(w.end) for w in valid)
    if start > end:
        widest = max(valid, key=lambda w: w.days or 0)
        return (
            DateWindow(
                widest.start,
                widest.end,
                "источники противоречат",
                "пересечение окон пусто; показаны окна каждого источника отдельно",
            ),
            True,
        )
    contributors = [w.source for w in valid]
    return (
        DateWindow(start.isoformat(), end.isoformat(), " + ".join(contributors)),
        False,
    )


def assign_windows(patch: Patch, windows: Sequence[DateWindow]) -> None:
    patch.windows = [w for w in windows if w.start and w.end]
    final, conflict = intersect_windows(patch.windows)
    patch.final_window = final
    patch.conflict = conflict
    if patch.windows and final.days is not None:
        widest = max((w.days or 0) for w in patch.windows)
        best = min(patch.windows, key=lambda w: w.days if w.days is not None else 10 ** 6)
        patch.narrowing_source = best.source
        patch.narrowing_days = widest - (final.days or widest)
