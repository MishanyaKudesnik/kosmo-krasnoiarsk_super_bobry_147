#!/usr/bin/env python3
"""Generate SYNTHETIC raster fixtures that mirror the case dataset.

Why this exists
---------------
The 168 GeoTIFFs of the case are distributed as a separate archive.  This
container has no network access to fetch it, so without fixtures nothing in the
pipeline could be executed or tested.  This script builds rasters with the exact
grids, CRS, band layout, dtypes and nodata conventions of the real product
family, so the whole analysis path runs for real.

What is real and what is not
----------------------------
Real, taken from the case tables shipped with the project:

* AOI geometry and area                         data/methodology/areas.csv
* the 2015 and 2019 mean carbon stock per AOI   data/methodology/baseline.csv
* per-scene SCL class counts on each crop       data/scene_metadata.json
* burn window, burned MODIS cell counts         data/events.csv
* grids, shapes, dtypes, CRS, nodata            data/file_catalog.csv

Synthetic: the pixel values themselves and their spatial arrangement.

Every file written here lands in `data_fixtures/`, never in `data/`, and the
catalogue tags each asset `origin="fixture"`.  The API and the UI carry that tag
through to every number, so a synthetic result can never be mistaken for a
measured one.  Drop the real archive into `data/` and the catalogue prefers it
automatically -- the fixtures are then ignored.

Usage:  python3 tools/make_fixtures.py [--out data_fixtures] [--seed 20260918]
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
from datetime import date
from typing import Dict, List, Sequence, Tuple

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from canopy.geo.crs import (  # noqa: E402
    SINUSOIDAL,
    utm_zone_for_lon,
    wgs84_to_sinusoidal,
    wgs84_to_utm,
)
from canopy.geo.grid import RasterGrid, pixel_weights  # noqa: E402
from canopy.geo.polygon import normalise_geometry  # noqa: E402
from canopy.rio.tiff import write_geotiff  # noqa: E402

CCI_STEP = 1.0 / 1125.0          # 0.000888888... degrees, the native CCI grid
GFC_STEP = 0.00025
MODIS_PIX = 463.3127165279167
MODIS_X0 = -20015109.354
MODIS_Y0 = 10007554.677
S2_PIX = 20.0
CF = 0.47

SCL_NAMES = {
    0: "nodata", 1: "defective", 2: "dark", 3: "cloud_shadow", 4: "vegetation",
    5: "not_vegetated", 6: "water", 7: "unclassified", 8: "cloud_medium",
    9: "cloud_high", 10: "thin_cirrus", 11: "snow",
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def smooth_field(rng: np.random.Generator, shape: Tuple[int, int], scale: float) -> np.ndarray:
    """A smooth random field via spectral filtering (no scipy needed)."""
    h, w = shape
    noise = rng.standard_normal((h, w))
    fy = np.fft.fftfreq(h)[:, None]
    fx = np.fft.fftfreq(w)[None, :]
    r = np.sqrt(fy ** 2 + fx ** 2)
    kernel = np.exp(-0.5 * (r * scale) ** 2)
    out = np.real(np.fft.ifft2(np.fft.fft2(noise) * kernel))
    s = out.std()
    return (out - out.mean()) / (s if s > 1e-12 else 1.0)


def blobs(
    rng: np.random.Generator, shape: Tuple[int, int], n: int, radius: float
) -> np.ndarray:
    """Union of n soft circular blobs, values in [0, 1]."""
    h, w = shape
    yy, xx = np.mgrid[0:h, 0:w]
    acc = np.zeros(shape)
    for _ in range(max(0, n)):
        cy = rng.uniform(0, h)
        cx = rng.uniform(0, w)
        rr = radius * rng.uniform(0.55, 1.6)
        d = np.sqrt((yy - cy) ** 2 + (xx - cx) ** 2) / max(rr, 1e-6)
        acc = np.maximum(acc, np.clip(1.4 - d, 0.0, 1.0))
    return np.clip(acc, 0.0, 1.0)


def align_floor(value: float, origin: float, step: float) -> Tuple[int, float]:
    idx = math.floor((value - origin) / step)
    return idx, origin + idx * step


def cci_grid_for(west: float, south: float, east: float, north: float) -> RasterGrid:
    """The native CCI window containing a bbox, aligned to the global grid."""
    _, x0 = align_floor(west, -180.0, CCI_STEP)
    ny, y0 = align_floor(90.0 - north, 0.0, CCI_STEP)
    y_top = 90.0 - ny * CCI_STEP
    ncols = int(math.ceil((east - x0) / CCI_STEP))
    nrows = int(math.ceil((y_top - south) / CCI_STEP))
    return RasterGrid(x0, y_top, CCI_STEP, -CCI_STEP, ncols, nrows, 4326)


def gfc_grid_for(west: float, south: float, east: float, north: float) -> RasterGrid:
    ncols = int(round((east - west) / GFC_STEP))
    nrows = int(round((north - south) / GFC_STEP))
    return RasterGrid(west, north, GFC_STEP, -GFC_STEP, ncols, nrows, 4326)


def modis_grid_for(west: float, south: float, east: float, north: float) -> RasterGrid:
    corners = [
        wgs84_to_sinusoidal(lon, lat)
        for lon, lat in ((west, south), (west, north), (east, north), (east, south))
    ]
    xs = [c[0] for c in corners]
    ys = [c[1] for c in corners]
    c0 = math.floor((min(xs) - MODIS_X0) / MODIS_PIX)
    c1 = math.ceil((max(xs) - MODIS_X0) / MODIS_PIX)
    r0 = math.floor((MODIS_Y0 - max(ys)) / MODIS_PIX)
    r1 = math.ceil((MODIS_Y0 - min(ys)) / MODIS_PIX)
    return RasterGrid(
        MODIS_X0 + c0 * MODIS_PIX,
        MODIS_Y0 - r0 * MODIS_PIX,
        MODIS_PIX,
        -MODIS_PIX,
        c1 - c0,
        r1 - r0,
        SINUSOIDAL,
    )


def s2_grid_for(west: float, south: float, east: float, north: float) -> Tuple[RasterGrid, int]:
    zone = utm_zone_for_lon((west + east) / 2.0)
    corners = [
        wgs84_to_utm(lon, lat, zone)
        for lon, lat in ((west, south), (west, north), (east, north), (east, south))
    ]
    xs = [c[0] for c in corners]
    ys = [c[1] for c in corners]
    x0 = math.floor(min(xs) / S2_PIX) * S2_PIX
    y1 = math.ceil(max(ys) / S2_PIX) * S2_PIX
    ncols = int(math.ceil((max(xs) - x0) / S2_PIX))
    nrows = int(math.ceil((y1 - min(ys)) / S2_PIX))
    return RasterGrid(x0, y1, S2_PIX, -S2_PIX, ncols, nrows, 32600 + zone), zone


def weighted_mean(values: np.ndarray, weights: np.ndarray) -> float:
    tot = weights.sum()
    return float((values * weights).sum() / tot) if tot > 0 else 0.0


def tune_to_mean(
    field: np.ndarray, weights: np.ndarray, target: float, rng: np.random.Generator
) -> np.ndarray:
    """Scale then nudge an integer field so its weighted mean hits `target`.

    Integer rasters cannot reproduce an arbitrary real mean exactly; this gets
    within one quantisation step spread over the contour, which is ~2e-6 in
    relative terms for these areas.
    """
    inside = weights > 0
    cur = weighted_mean(field, weights)
    if cur > 0:
        field = field * (target / cur)
    field = np.rint(np.clip(field, 0, 65000)).astype(np.float64)
    for _ in range(4000):
        cur = weighted_mean(field, weights)
        diff = target - cur
        if abs(diff) < 1e-4:
            break
        total_w = weights.sum()
        step = 1.0 if diff > 0 else -1.0
        need = int(min(len(np.flatnonzero(inside)), max(1, abs(diff) * total_w / weights.max())))
        idx = rng.choice(np.flatnonzero(inside.ravel()), size=need, replace=False)
        flat = field.ravel()
        cand = flat[idx] + step
        ok = (cand >= 0) & (cand <= 65000)
        flat[idx[ok]] = cand[ok]
    return field


# ---------------------------------------------------------------------------
# per-AOI generation
# ---------------------------------------------------------------------------
class AoiFixture:
    def __init__(self, row: Dict[str, str], baseline: Dict[str, Dict], seed: int):
        self.aoi_id = row["aoi_id"]
        self.row = row
        self.west = float(row["bbox_west"])
        self.south = float(row["bbox_south"])
        self.east = float(row["bbox_east"])
        self.north = float(row["bbox_north"])
        self.role = row.get("selection_role", "")
        self.base = baseline[self.aoi_id]
        self.rng = np.random.default_rng(seed + abs(hash(self.aoi_id)) % 100000)

        self.geometry = normalise_geometry(
            {
                "type": "Polygon",
                "coordinates": [
                    [
                        [self.east, self.south],
                        [self.east, self.north],
                        [self.west, self.north],
                        [self.west, self.south],
                        [self.east, self.south],
                    ]
                ],
            }
        )
        self.cci = cci_grid_for(self.west, self.south, self.east, self.north)
        self.pw = pixel_weights(self.cci, self.geometry)
        self.shape = (self.cci.height, self.cci.width)
        self.w_full = np.zeros(self.shape)
        self.w_full[
            self.pw.row0 : self.pw.row0 + self.pw.nrows,
            self.pw.col0 : self.pw.col0 + self.pw.ncols,
        ] = self.pw.weights_ha

    # -- the disturbance story --------------------------------------------
    def disturbance_layers(self) -> Dict[str, np.ndarray]:
        """Spatial masks used consistently by every product."""
        rng = self.rng
        shape = self.shape
        out: Dict[str, np.ndarray] = {}
        if self.aoi_id.endswith("MORDOVIA_03"):
            out["fire_core"] = blobs(rng, shape, 3, 7.0)          # severe burn
            out["fire_edge"] = np.maximum(out["fire_core"], blobs(rng, shape, 5, 11.0))
            out["early"] = blobs(rng, shape, 4, 5.0)              # 2015-2019 decline
        elif self.aoi_id.endswith("MORDOVIA_04"):
            out["early"] = blobs(rng, shape, 6, 8.0)              # loss before 2019
            out["fire_core"] = blobs(rng, shape, 5, 9.0)
            out["fire_edge"] = np.ones(shape) * 0.85
        elif self.aoi_id.endswith("VOLOGDA_02"):
            out["cut2020"] = blobs(rng, shape, 2, 4.0)
            out["cut2021"] = blobs(rng, shape, 2, 4.5)
            out["cut2022"] = blobs(rng, shape, 3, 4.0)
        else:                                                      # control
            out["none"] = np.zeros(shape)
        return out

    def agb_series(self) -> Dict[int, np.ndarray]:
        """AGB in t d.m./ha for 2015..2024, as uint16-compatible floats."""
        rng = self.rng
        shape = self.shape
        dist = self.disturbance_layers()
        texture = 1.0 + 0.28 * smooth_field(rng, shape, 7.0)
        texture = np.clip(texture, 0.25, 2.1)

        target15 = float(self.base["reference_mean_2015_tc_ha"]) / CF
        target19 = float(self.base["reference_mean_2019_tc_ha"]) / CF

        field15 = tune_to_mean(texture * target15, self.w_full, target15, rng)
        series: Dict[int, np.ndarray] = {2015: field15}

        # 2016-2019: linear path to the published 2019 mean, with the early
        # disturbance carved out where the case says there was one
        early = dist.get("early", np.zeros(shape))
        for year in (2016, 2017, 2018, 2019):
            t = (year - 2015) / 4.0
            f = field15 * (1.0 - 0.55 * early * t)
            f = f * (1.0 + 0.004 * rng.standard_normal(shape))
            if year == 2019:
                f = tune_to_mean(f, self.w_full, target19, rng)
            else:
                f = np.rint(np.clip(f, 0, 65000))
            series[year] = f

        cur = series[2019].copy()
        for year in range(2020, 2025):
            f = cur.copy()
            if self.aoi_id.endswith(("MORDOVIA_03", "MORDOVIA_04")):
                if year == 2021:
                    # August 2021 fire: severity taken from the Sentinel-2
                    # not-vegetated jump reported for these crops
                    sev = 0.85 * dist["fire_core"] + 0.25 * (
                        dist["fire_edge"] - dist["fire_core"]
                    ).clip(0, 1)
                    f = f * (1.0 - np.clip(sev, 0.0, 0.95))
                elif year > 2021:
                    f = f + 1.9 * np.clip(dist["fire_edge"], 0, 1)   # regrowth
                    f = f + 0.35
            elif self.aoi_id.endswith("VOLOGDA_02"):
                key = f"cut{year}"
                if key in dist:
                    f = f * (1.0 - 0.8 * dist[key])
                f = f + 1.1
            else:
                f = f * (1.0 + 0.0042) + 0.25                        # control growth
            f = f * (1.0 + 0.003 * rng.standard_normal(shape))
            f = np.rint(np.clip(f, 0.0, 65000.0))
            series[year] = f
            cur = f
        return series

    def agb_sd(self, agb: np.ndarray) -> np.ndarray:
        sd = 0.22 * agb + 9.0 + 3.0 * np.abs(smooth_field(self.rng, self.shape, 9.0))
        return np.rint(np.clip(sd, 4.0, 400.0))

    def report_means(self, series: Dict[int, np.ndarray]) -> List[Tuple[int, float]]:
        return [(y, weighted_mean(series[y], self.w_full) * CF) for y in sorted(series)]


# ---------------------------------------------------------------------------
# writers per product
# ---------------------------------------------------------------------------
def write_cci(out_root: str, fx: AoiFixture, series: Dict[int, np.ndarray]) -> List[str]:
    paths = []
    d = os.path.join(out_root, fx.aoi_id)
    os.makedirs(d, exist_ok=True)
    for year, agb in series.items():
        sd = fx.agb_sd(agb)
        stack = np.stack([agb.astype(np.uint16), sd.astype(np.uint16)])
        p = os.path.join(d, f"CCI_Biomass_{year}.tif")
        write_geotiff(p, stack, fx.cci, epsg=4326, compress=True)
        paths.append(p)
    diff = (series[2020] - series[2019]).astype(np.int16)
    sd19 = fx.agb_sd(series[2019])
    sd20 = fx.agb_sd(series[2020])
    dsd = np.rint(np.sqrt(sd19.astype(np.float64) ** 2 + sd20.astype(np.float64) ** 2) * 0.72)
    flag = np.where(np.abs(diff) > 12, 2, 1).astype(np.int16)
    p = os.path.join(d, "CCI_Change_2019_2020.tif")
    write_geotiff(
        p, np.stack([diff, dsd.astype(np.int16), flag]), fx.cci, epsg=4326, compress=True
    )
    paths.append(p)
    return paths


def write_gfc(out_root: str, fx: AoiFixture) -> str:
    grid = gfc_grid_for(fx.west, fx.south, fx.east, fx.north)
    shape = (grid.height, grid.width)
    rng = fx.rng
    cover = np.clip(72 + 18 * smooth_field(rng, shape, 6.0), 0, 100)
    loss = np.zeros(shape, dtype=np.uint8)
    if fx.aoi_id.endswith(("MORDOVIA_03", "MORDOVIA_04")):
        m = blobs(rng, shape, 6, 24.0)
        loss[m > 0.55] = 21
        if fx.aoi_id.endswith("MORDOVIA_04"):
            e = blobs(rng, shape, 5, 18.0)
            loss[(e > 0.6) & (loss == 0)] = rng.choice([17, 18])
    elif fx.aoi_id.endswith("VOLOGDA_02"):
        for code, n in ((20, 2), (21, 2), (22, 3)):
            m = blobs(rng, shape, n, 14.0)
            loss[(m > 0.6) & (loss == 0)] = code
    else:
        m = blobs(rng, shape, 1, 5.0)
        loss[m > 0.85] = 19
    mask = np.full(shape, 1, dtype=np.uint8)
    d = os.path.join(out_root, fx.aoi_id)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "GFC_2025_v1_13.tif")
    write_geotiff(
        p,
        np.stack([cover.astype(np.uint8), loss, mask]),
        grid,
        epsg=4326,
        compress=True,
    )
    return p


def write_modis(out_root: str, fx: AoiFixture, events: List[Dict[str, str]]) -> List[str]:
    evs = [e for e in events if e.get("aoi_id") == fx.aoi_id]
    if not evs:
        return []
    ev = evs[0]
    grid = modis_grid_for(fx.west, fx.south, fx.east, fx.north)
    shape = (grid.height, grid.width)

    # which cell centres fall inside the AOI
    from canopy.geo.crs import sinusoidal_to_wgs84
    from canopy.geo.polygon import point_in_multipolygon

    inside = np.zeros(shape, dtype=bool)
    for r in range(shape[0]):
        for c in range(shape[1]):
            x, y = grid.cell_center(r, c)
            lon, lat = sinusoidal_to_wgs84(x, y)
            inside[r, c] = point_in_multipolygon(lon, lat, fx.geometry)

    n_inside = int(inside.sum())
    want = int(ev["burned_pixel_centers_in_aoi"])
    d0 = date.fromisoformat(ev["date_min_product"])
    d1 = date.fromisoformat(ev["date_max_product"])
    doy0 = d0.timetuple().tm_yday
    doy1 = d1.timetuple().tm_yday

    burn = np.full(shape, -1, dtype=np.int16)
    idx = np.flatnonzero(inside.ravel())
    if want >= n_inside:
        chosen = idx
    else:
        # burn the cells nearest the centre of gravity of the fire blobs
        order = fx.rng.permutation(idx)
        chosen = order[:want]
    days = fx.rng.integers(doy0, doy1 + 1, size=len(chosen)).astype(np.int16)
    flat = burn.ravel()
    flat[chosen] = days
    burn = flat.reshape(shape)

    first = np.where(burn > 0, burn, -1).astype(np.int16)
    last = np.where(burn > 0, np.minimum(burn + 2, doy1 + 2), -1).astype(np.int16)
    unc = np.where(
        burn > 0,
        fx.rng.integers(
            int(ev["date_uncertainty_days_min"]), int(ev["date_uncertainty_days_max"]) + 1,
            size=shape,
        ),
        0,
    ).astype(np.uint8)
    qa = np.where(burn > 0, 3, 1).astype(np.uint8)

    granule = os.path.basename(ev["evidence_file"]).rsplit("_Burn_Date", 1)[0]
    d = os.path.join(out_root, fx.aoi_id, "MODIS")
    os.makedirs(d, exist_ok=True)
    out = []
    for layer, arr, nodata in (
        ("Burn_Date", burn, -1.0),
        ("First_Day", first, -1.0),
        ("Last_Day", last, -1.0),
        ("Burn_Date_Uncertainty", unc, None),
        ("QA", qa, None),
    ):
        p = os.path.join(d, f"{granule}_{layer}.tif")
        write_geotiff(p, arr, grid, epsg=None, nodata=nodata, compress=True)
        out.append(p)
    return out


def write_sentinel2(
    out_root: str, fx: AoiFixture, scenes: List[Dict[str, str]], scl_counts: Dict[str, Dict[str, int]]
) -> List[str]:
    grid, _zone = s2_grid_for(fx.west, fx.south, fx.east, fx.north)
    shape = (grid.height, grid.width)
    npx = shape[0] * shape[1]
    rng = fx.rng
    d = os.path.join(out_root, fx.aoi_id, "Sentinel2")
    os.makedirs(d, exist_ok=True)
    written: List[str] = []

    dist = fx.disturbance_layers()

    def upscale(a: np.ndarray) -> np.ndarray:
        """Nearest-neighbour blow-up of a CCI-sized mask onto the S2 grid."""
        ys = (np.arange(shape[0]) * a.shape[0] / shape[0]).astype(int).clip(0, a.shape[0] - 1)
        xs = (np.arange(shape[1]) * a.shape[1] / shape[1]).astype(int).clip(0, a.shape[1] - 1)
        return a[ys][:, xs]

    for sc in scenes:
        item = sc["item_id"]
        dt = sc["datetime_utc"]
        year = int(sc["year"])
        counts = scl_counts.get(item, {})
        scl = _scl_from_counts(counts, shape, rng)

        veg = (scl == 4)
        burned_state = 0.0
        if fx.aoi_id.endswith(("MORDOVIA_03", "MORDOVIA_04")):
            if year == 2021 and dt >= "2021-08":
                burned_state = 1.0
            elif year == 2022:
                burned_state = 0.75
            elif year == 2023:
                burned_state = 0.45
            elif year >= 2024:
                burned_state = 0.3
        sev = upscale(np.clip(dist.get("fire_edge", np.zeros(fx.shape)), 0, 1)) * burned_state

        base_nir = 0.30 - 0.14 * sev + 0.02 * smooth_field(rng, shape, 14.0)
        base_swir = 0.12 + 0.16 * sev + 0.01 * smooth_field(rng, shape, 14.0)
        base_red = 0.045 + 0.05 * sev
        refl = np.stack(
            [
                np.full(shape, 0.035) + 0.01 * sev,          # B02
                np.full(shape, 0.06) + 0.01 * sev,           # B03
                base_red,                                     # B04
                base_nir,                                     # B8A
                base_swir,                                    # B11
                base_swir * 0.78 + 0.02 * sev,               # B12
            ]
        ).astype(np.float32)

        cloud = np.isin(scl, [8, 9, 10, 3])
        refl[:, cloud] = np.nan
        notveg = (scl == 5)
        refl[3][notveg] *= 0.62
        refl[4][notveg] *= 1.35
        refl = np.clip(refl, 0.0, 1.2).astype(np.float32)
        refl[:, cloud] = np.nan

        p1 = os.path.join(d, f"{item}_reflectance.tif")
        write_geotiff(p1, refl, grid, epsg=grid.crs, nodata=float("nan"), compress=True)
        p2 = os.path.join(d, f"{item}_SCL.tif")
        write_geotiff(p2, scl.astype(np.uint8), grid, epsg=grid.crs, nodata=0.0, compress=True)
        written += [p1, p2]
    return written


def _scl_from_counts(
    counts: Dict[str, int], shape: Tuple[int, int], rng: np.random.Generator
) -> np.ndarray:
    """Build an SCL raster reproducing the real per-class share of a crop."""
    npx = shape[0] * shape[1]
    total = sum(counts.values()) or npx
    order = [9, 8, 10, 3, 2, 11, 6, 7, 5, 4, 1, 0]
    shares = {int(k): v / total for k, v in counts.items()}
    scl = np.full(shape, 4, dtype=np.uint8)
    if not shares:
        return scl

    # cloud-like classes go into coherent blobs, ground classes into patches
    field = smooth_field(rng, shape, 5.0)
    ranked = np.argsort(field, axis=None)
    assigned = np.zeros(npx, dtype=bool)
    cursor = 0
    for cls in order:
        share = shares.get(cls, 0.0)
        if share <= 0:
            continue
        n = int(round(share * npx))
        if n <= 0:
            continue
        take = ranked[cursor : cursor + n]
        cursor += n
        flat = scl.ravel()
        flat[take] = cls
        assigned[take] = True
    return scl


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="data_fixtures")
    ap.add_argument("--seed", type=int, default=20260918)
    ap.add_argument("--skip-s2", action="store_true")
    args = ap.parse_args()

    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    out_root = os.path.join(root, args.out)
    os.makedirs(out_root, exist_ok=True)

    areas = list(csv.DictReader(open(os.path.join(root, "data/methodology/areas.csv"), encoding="utf-8-sig")))
    base_rows = list(csv.DictReader(open(os.path.join(root, "data/methodology/baseline.csv"), encoding="utf-8-sig")))
    baseline: Dict[str, Dict] = {}
    for r in base_rows:
        baseline.setdefault(r["aoi_id"], r)
    events = list(csv.DictReader(open(os.path.join(root, "data/events.csv"), encoding="utf-8-sig")))
    scenes = list(csv.DictReader(open(os.path.join(root, "data/scenes.csv"), encoding="utf-8-sig")))

    meta_path = os.path.join(root, "data/scene_metadata.json")
    scl_counts: Dict[str, Dict[str, int]] = {}
    if os.path.exists(meta_path):
        meta = json.load(open(meta_path, encoding="utf-8"))
        for s in meta.get("scenes", []):
            scl_counts[s["source_stac_item"]["id"]] = s.get("scl_counts_crop", {})

    manifest: List[Dict] = []
    for row in areas:
        fx = AoiFixture(row, baseline, args.seed)
        print(f"\n=== {fx.aoi_id} ===")
        print(
            f"  CCI grid   {fx.cci.height}x{fx.cci.width} @ {fx.cci.dx:.10f} deg  "
            f"weights: {fx.pw.total_ha:.4f} ha vs declared {float(row['area_ha']):.4f} ha"
        )
        series = fx.agb_series()
        write_cci(out_root, fx, series)
        gp = write_gfc(out_root, fx)
        mp = write_modis(out_root, fx, events)
        aoi_scenes = [s for s in scenes if s["aoi_id"] == fx.aoi_id]
        sp = [] if args.skip_s2 else write_sentinel2(out_root, fx, aoi_scenes, scl_counts)
        print(f"  GFC 1 file, MODIS {len(mp)} files, Sentinel-2 {len(sp)} files")
        means = fx.report_means(series)
        print("  mean stock t C/ha:", ", ".join(f"{y}:{v:.3f}" for y, v in means))
        print(
            f"  published 2015={float(fx.base['reference_mean_2015_tc_ha']):.6f}  "
            f"2019={float(fx.base['reference_mean_2019_tc_ha']):.6f}"
        )
        manifest.append(
            {
                "aoi_id": fx.aoi_id,
                "cci_grid": [fx.cci.height, fx.cci.width],
                "weights_total_ha": fx.pw.total_ha,
                "declared_area_ha": float(row["area_ha"]),
                "means_tc_ha": {str(y): v for y, v in means},
            }
        )

    note = {
        "kind": "SYNTHETIC FIXTURE",
        "generated_by": "tools/make_fixtures.py",
        "seed": args.seed,
        "warning": (
            "Pixel values in this directory are synthetic. Grids, CRS, dtypes, band "
            "layout, AOI geometry, the published 2015/2019 mean stocks, SCL class "
            "shares and the MODIS burn window are taken from the case dataset. "
            "Put the real archive in data/ and it takes precedence automatically."
        ),
        # The interface, the report and the jury all speak Russian; a warning
        # only a developer can read is not a warning.
        "warning_ru": (
            "Значения пикселей в этом каталоге синтетические. Из материалов кейса "
            "взяты сетки, CRS, типы данных, состав каналов, геометрия участков, "
            "опубликованные средние запасы 2015 и 2019 годов, доли классов SCL и "
            "окно пожара по MODIS. Положите настоящий архив в data/ — каталог "
            "начнёт использовать его автоматически."
        ),
        "aois": manifest,
    }
    with open(os.path.join(out_root, "FIXTURE_NOTICE.json"), "w", encoding="utf-8") as fh:
        json.dump(note, fh, ensure_ascii=False, indent=2)
    print("\nWrote FIXTURE_NOTICE.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
