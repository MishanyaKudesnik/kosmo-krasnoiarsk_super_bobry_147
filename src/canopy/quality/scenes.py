"""Sentinel-2 scene quality, measured on the request contour.

The dataset makes the point better than any argument: over RU_TVER_01 the scene
of 2019-07-02 reports 41.2 % cloud yet is fully usable on the contour, while the
only post-fire scene over RU_MORDOVIA_03, 2021-09-12, reports 22.1 % cloud and
is 98 % unusable exactly where the answer is needed.  Scene-level metadata
therefore never decide anything here; the SCL classes are counted on the
polygon, pixel by pixel, and the full histogram is kept so the interface can
tell a verifier *why* a scene was set aside instead of silently dropping it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from ..core.config import get_settings
from ..dal.catalog import Asset, Catalog
from ..geo.grid import PixelWeights, RasterGrid, footprint_in_source
from ..geo.polygon import MultiPolygon, point_in_multipolygon
from ..rio.tiff import GeoTiff

SCL_LABELS = {
    0: "нет данных",
    1: "дефектные",
    2: "тёмные области",
    3: "тень облака",
    4: "растительность",
    5: "без растительности",
    6: "вода",
    7: "не классифицировано",
    8: "облако (средняя вероятность)",
    9: "облако (высокая вероятность)",
    10: "перистые облака",
    11: "снег и лёд",
}


@dataclass
class SceneQuality:
    scene_id: str
    datetime_utc: str
    year: int
    doy: int
    usable_fraction: float
    scl_histogram: Dict[int, float]
    pixels: int
    asset_scl: Optional[Asset] = None
    asset_refl: Optional[Asset] = None
    processing_baseline: str = ""
    scene_cloud_percent: Optional[float] = None
    selected: bool = False
    rejected_reason: Optional[str] = None

    def as_dict(self) -> Dict[str, object]:
        return {
            "scene_id": self.scene_id,
            "datetime_utc": self.datetime_utc,
            "date": self.datetime_utc[:10],
            "year": self.year,
            "doy": self.doy,
            "usable_fraction": self.usable_fraction,
            "scl_histogram": [
                {
                    "class": int(k),
                    "label": SCL_LABELS.get(int(k), str(k)),
                    "share": float(v),
                }
                for k, v in sorted(self.scl_histogram.items(), key=lambda kv: -kv[1])
                if v > 0
            ],
            "pixels": self.pixels,
            "processing_baseline": self.processing_baseline,
            "scene_cloud_percent": self.scene_cloud_percent,
            "selected": self.selected,
            "rejected_reason": self.rejected_reason,
            "has_reflectance": self.asset_refl is not None,
            "file_scl": self.asset_scl.relative_path if self.asset_scl else None,
            "file_reflectance": self.asset_refl.relative_path if self.asset_refl else None,
        }


def _doy(iso: str) -> int:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timetuple().tm_yday
    except Exception:
        try:
            return date.fromisoformat(iso[:10]).timetuple().tm_yday
        except Exception:
            return 0


def contour_mask_for(grid: RasterGrid, geometry: MultiPolygon) -> Optional[Tuple[Tuple[int, int, int, int], np.ndarray]]:
    """Boolean mask of a projected raster's pixels whose centre is in the contour.

    Sentinel-2 sits in UTM while the contour is geographic, so the test is done
    per pixel centre.  This mask only ever drives *quality statistics* and index
    aggregation, never an area: areas always come from the geodesic weighting on
    the biomass grid.
    """
    from ..geo.crs import to_wgs84_array
    from ..geo.polygon import multipolygon_bbox, points_in_multipolygon

    west, south, east, north = multipolygon_bbox(geometry)
    corners = []
    from ..geo.crs import from_wgs84

    for lon, lat in ((west, south), (west, north), (east, north), (east, south)):
        corners.append(from_wgs84(lon, lat, grid.crs))
    xs = [c[0] for c in corners]
    ys = [c[1] for c in corners]
    win = grid.window_for_bbox(min(xs), min(ys), max(xs), max(ys))
    if win is None:
        return None
    r0, c0, nr, nc = win
    rows = np.arange(r0, r0 + nr)[:, None]
    cols = np.arange(c0, c0 + nc)[None, :]
    xs = grid.origin_x + (cols + 0.5) * grid.dx
    ys = grid.origin_y + (rows + 0.5) * grid.dy
    xs = np.broadcast_to(xs, (nr, nc))
    ys = np.broadcast_to(ys, (nr, nc))
    lon, lat = to_wgs84_array(xs, ys, grid.crs)
    mask = points_in_multipolygon(lon, lat, geometry)
    if not mask.any():
        return None
    return (r0, c0, nr, nc), mask


def assess_scene(
    scl_asset: Asset,
    geometry: MultiPolygon,
    refl_asset: Optional[Asset],
    scene_meta: Dict[str, str],
    valid_classes: Sequence[int],
) -> Optional[SceneQuality]:
    got = contour_mask_for(scl_asset.grid, geometry)
    if got is None:
        return None
    (r0, c0, nr, nc), mask = got
    with GeoTiff(scl_asset.path) as t:
        scl = t.read(band=1, window=(r0, c0, nr, nc))
    vals = scl[mask]
    if vals.size == 0:
        return None
    hist: Dict[int, float] = {}
    for cls in np.unique(vals):
        hist[int(cls)] = float(np.count_nonzero(vals == cls) / vals.size)
    usable = float(sum(hist.get(int(c), 0.0) for c in valid_classes))

    iso = scl_asset.observation_time or scene_meta.get("datetime_utc") or ""
    year = scl_asset.year or (int(iso[:4]) if iso[:4].isdigit() else 0)
    cloud = scene_meta.get("source_scene_cloud_percent")
    return SceneQuality(
        scene_id=scl_asset.scene_id or scl_asset.relative_path,
        datetime_utc=iso,
        year=year,
        doy=_doy(iso),
        usable_fraction=usable,
        scl_histogram=hist,
        pixels=int(vals.size),
        asset_scl=scl_asset,
        asset_refl=refl_asset,
        processing_baseline=scene_meta.get("processing_baseline", ""),
        scene_cloud_percent=float(cloud) if cloud not in (None, "") else None,
    )


def assess_all(
    catalog: Catalog,
    geometry: MultiPolygon,
    bbox: Tuple[float, float, float, float],
    years: Sequence[int],
) -> List[SceneQuality]:
    settings = get_settings()
    cfg = settings.methods["scene_selection"]
    valid_classes = cfg["valid_scl_classes"]
    refl_by_scene = {
        a.scene_id: a for a in catalog.query("S2_REFLECTANCE", bbox=bbox) if a.scene_id
    }
    out: List[SceneQuality] = []
    for asset in catalog.query("S2_SCL", bbox=bbox):
        if asset.year not in years:
            continue
        meta = catalog.scene_meta(asset.scene_id or "")
        q = assess_scene(
            asset, geometry, refl_by_scene.get(asset.scene_id or ""), meta, valid_classes
        )
        if q is not None:
            out.append(q)
    out.sort(key=lambda q: q.datetime_utc)
    return out


def select_per_year(scenes: List[SceneQuality]) -> Dict[int, Optional[SceneQuality]]:
    """Best usable scene per year inside the configured seasonal window."""
    cfg = get_settings().methods["scene_selection"]
    doy_min = int(cfg["season_doy_min"])
    doy_max = int(cfg["season_doy_max"])
    ref_doy = int(cfg["reference_doy"])
    min_usable = float(cfg["min_usable_fraction"])

    by_year: Dict[int, List[SceneQuality]] = {}
    for s in scenes:
        by_year.setdefault(s.year, []).append(s)

    chosen: Dict[int, Optional[SceneQuality]] = {}
    for year, group in by_year.items():
        candidates = []
        for s in group:
            if not (doy_min <= s.doy <= doy_max):
                s.rejected_reason = (
                    f"вне сезонного окна (DOY {s.doy}, окно {doy_min}–{doy_max})"
                )
                continue
            if s.usable_fraction < min_usable:
                s.rejected_reason = (
                    f"годных пикселей {s.usable_fraction * 100:.1f} % "
                    f"при пороге {min_usable * 100:.0f} %"
                )
                continue
            if s.asset_refl is None:
                s.rejected_reason = "нет файла отражения"
                continue
            candidates.append(s)
        if not candidates:
            chosen[year] = None
            continue
        best = max(candidates, key=lambda s: (s.usable_fraction, -abs(s.doy - ref_doy)))
        best.selected = True
        best.rejected_reason = None
        chosen[year] = best
    return chosen


def read_index_stack(
    scene: SceneQuality, geometry: MultiPolygon
) -> Optional[Dict[str, np.ndarray]]:
    """NBR, NDVI and NDMI on the scene grid, with cloudy pixels masked out."""
    if scene.asset_refl is None:
        return None
    asset = scene.asset_refl
    got = contour_mask_for(asset.grid, geometry)
    if got is None:
        return None
    (r0, c0, nr, nc), inside = got
    with GeoTiff(asset.path) as t:
        arr = t.read(window=(r0, c0, nr, nc)).astype(np.float64)
    with GeoTiff(scene.asset_scl.path) as t:
        scl = t.read(band=1, window=(r0, c0, nr, nc))

    cfg = get_settings().methods["scene_selection"]
    ok = np.isin(scl, cfg["valid_scl_classes"]) & inside
    red, nir, swir1, swir2 = arr[2], arr[3], arr[4], arr[5]

    def norm(a: np.ndarray, b: np.ndarray) -> np.ndarray:
        den = a + b
        out = np.full(a.shape, np.nan)
        good = np.isfinite(den) & (np.abs(den) > 1e-9) & ok
        out[good] = (a[good] - b[good]) / den[good]
        return out

    return {
        "window": (r0, c0, nr, nc),
        "valid": ok,
        "NBR": norm(nir, swir2),
        "NDVI": norm(nir, red),
        "NDMI": norm(nir, swir1),
        "grid": asset.grid,
    }
