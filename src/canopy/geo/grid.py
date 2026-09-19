"""Raster grids and exact pixel/polygon area weighting.

This is the module the case statement is really about: "area is accounted for by
the intersection of pixels with the contour; on a grid in degrees the pixel area
cannot be taken as one hectare".

`pixel_weights` returns, for every pixel of a grid that touches a request
contour, the geodesic area in hectares of the part of that pixel which actually
lies inside the contour.  Interior pixels reuse a per-row cached area; only the
pixels cut by the boundary are clipped, so the cost is proportional to the
perimeter rather than the area.

At the latitudes of this dataset one ESA CCI pixel covers 0.497-0.563 ha, so the
naive "one pixel = one hectare" assumption would inflate every stock and every
result by a factor of 1.78 to 2.01.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from . import crs as crs_mod
from .ellipsoid import (
    EQUAL_AREA_SCALE,
    cell_area_m2,
    equal_area_multipolygon,
    equal_area_xy,
    meters_per_degree,
    polygon_area_m2,
)
from .polygon import (
    MultiPolygon,
    clip_polygon_to_rect,
    multipolygon_bbox,
    point_in_multipolygon,
    rect_fully_inside_ring,
    signed_area,
)


def _planar_polygon_area(
    rings: Sequence[Sequence[Tuple[float, float]]],
    ox: float = 0.0,
    oy: float = 0.0,
) -> float:
    """Shoelace area of [exterior, hole, ...] in a plane, holes subtracted.

    `ox, oy` shift the ring to a local origin first.  A pixel of the analysis
    grid is about 1e-5 by 1e-5 in the equal-area plane while the coordinates
    themselves are of order one, so the shoelace products cancel to eleven
    digits and the leftover round-off is a few parts per million of the pixel.
    Measured from the corner of the pixel the same sum is exact.
    """
    if not rings:
        return 0.0
    area = abs(_shoelace(rings[0], ox, oy))
    for hole in rings[1:]:
        area -= abs(_shoelace(hole, ox, oy))
    return max(0.0, area)


def _shoelace(ring: Sequence[Tuple[float, float]], ox: float, oy: float) -> float:
    pts = ring[:-1] if len(ring) > 1 and ring[0] == ring[-1] else ring
    n = len(pts)
    if n < 3:
        return 0.0
    total = 0.0
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        total += (x1 - ox) * (y2 - oy) - (x2 - ox) * (y1 - oy)
    return total / 2.0


@dataclass(frozen=True)
class RasterGrid:
    """A north-up affine grid.  `dy` is negative for the usual north-up case."""

    origin_x: float
    origin_y: float
    dx: float
    dy: float
    width: int
    height: int
    crs: object = 4326

    # -- geometry of a single cell -----------------------------------------
    def cell_bounds(self, row: int, col: int) -> Tuple[float, float, float, float]:
        x0 = self.origin_x + col * self.dx
        x1 = self.origin_x + (col + 1) * self.dx
        y0 = self.origin_y + row * self.dy
        y1 = self.origin_y + (row + 1) * self.dy
        return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)

    def cell_center(self, row: int, col: int) -> Tuple[float, float]:
        return (
            self.origin_x + (col + 0.5) * self.dx,
            self.origin_y + (row + 0.5) * self.dy,
        )

    def bounds(self) -> Tuple[float, float, float, float]:
        x0, y0 = self.origin_x, self.origin_y
        x1 = self.origin_x + self.width * self.dx
        y1 = self.origin_y + self.height * self.dy
        return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)

    def index_of(self, x: float, y: float) -> Tuple[int, int]:
        col = int((x - self.origin_x) / self.dx)
        row = int((y - self.origin_y) / self.dy)
        return row, col

    def window_for_bbox(
        self, west: float, south: float, east: float, north: float
    ) -> Optional[Tuple[int, int, int, int]]:
        """(row0, col0, nrows, ncols) covering a bbox given in grid coordinates."""
        c0 = int(np.floor((west - self.origin_x) / self.dx))
        c1 = int(np.ceil((east - self.origin_x) / self.dx))
        if self.dy < 0:
            r0 = int(np.floor((north - self.origin_y) / self.dy))
            r1 = int(np.ceil((south - self.origin_y) / self.dy))
        else:
            r0 = int(np.floor((south - self.origin_y) / self.dy))
            r1 = int(np.ceil((north - self.origin_y) / self.dy))
        r0 = max(0, r0)
        c0 = max(0, c0)
        r1 = min(self.height, r1)
        c1 = min(self.width, c1)
        if r1 <= r0 or c1 <= c0:
            return None
        return r0, c0, r1 - r0, c1 - c0

    def pixel_size_m(self, lat: float) -> Tuple[float, float]:
        if self.crs in (4326, "4326", "EPSG:4326", None):
            mlon, mlat = meters_per_degree(lat)
            return abs(self.dx) * mlon, abs(self.dy) * mlat
        return abs(self.dx), abs(self.dy)

    def nominal_pixel_area_ha(self, lat: float) -> float:
        w, h = self.pixel_size_m(lat)
        return w * h / 10000.0


@dataclass
class PixelWeights:
    """Result of intersecting a contour with a grid."""

    grid: RasterGrid
    row0: int
    col0: int
    nrows: int
    ncols: int
    weights_ha: np.ndarray          # (nrows, ncols) float64, 0 where outside
    total_ha: float
    full_pixels: int
    partial_pixels: int
    partial_area_ha: float

    @property
    def mask(self) -> np.ndarray:
        return self.weights_ha > 0.0

    def as_dict(self) -> dict:
        return {
            "total_ha": self.total_ha,
            "pixels_touched": int(self.mask.sum()),
            "full_pixels": self.full_pixels,
            "partial_pixels": self.partial_pixels,
            "partial_area_ha": self.partial_area_ha,
            "partial_area_share": (
                self.partial_area_ha / self.total_ha if self.total_ha > 0 else 0.0
            ),
            "window": {
                "row0": self.row0,
                "col0": self.col0,
                "nrows": self.nrows,
                "ncols": self.ncols,
            },
        }


def pixel_weights(grid: RasterGrid, geometry: MultiPolygon) -> Optional[PixelWeights]:
    """Geodesic area in hectares of each pixel's intersection with `geometry`.

    `geometry` is in EPSG:4326.  The grid must also be in EPSG:4326: the carbon
    integration always runs on the native biomass grid, and no resampling of the
    integrated quantity is ever performed.
    """
    if grid.crs not in (4326, "4326", "EPSG:4326", None):
        raise ValueError("pixel_weights requires a geographic grid (EPSG:4326)")

    west, south, east, north = multipolygon_bbox(geometry)
    win = grid.window_for_bbox(west, south, east, north)
    if win is None:
        return None
    row0, col0, nrows, ncols = win

    # The whole intersection is done in the equal-area plane (see the note in
    # ellipsoid.py): there the cell edges stay axis-aligned, clipping is exact
    # and the pieces of a contour add up to the contour.
    geom_ea = equal_area_multipolygon(geometry)
    ea_west, ea_south = equal_area_xy(west, south)
    ea_east, ea_north = equal_area_xy(east, north)

    weights = np.zeros((nrows, ncols), dtype=np.float64)
    full = 0
    partial = 0
    partial_area = 0.0
    dx_rad = abs(math.radians(grid.dx))

    for r in range(nrows):
        gr = row0 + r
        cy0 = grid.origin_y + gr * grid.dy
        cy1 = grid.origin_y + (gr + 1) * grid.dy
        south_e, north_e = min(cy0, cy1), max(cy0, cy1)
        if north_e < south or south_e > north:
            continue
        _, ea_s = equal_area_xy(0.0, south_e)
        _, ea_n = equal_area_xy(0.0, north_e)
        if ea_n < ea_south or ea_s > ea_north:
            continue
        # every pixel in a geographic row has the same area
        full_area_m2 = EQUAL_AREA_SCALE * dx_rad * (ea_n - ea_s)

        for c in range(ncols):
            gc = col0 + c
            cx0 = grid.origin_x + gc * grid.dx
            cx1 = grid.origin_x + (gc + 1) * grid.dx
            west_e, east_e = min(cx0, cx1), max(cx0, cx1)
            if east_e < west or west_e > east:
                continue
            ea_w = math.radians(west_e)
            ea_e = math.radians(east_e)

            inside_all = False
            for poly in geom_ea:
                if rect_fully_inside_ring(poly[0], ea_w, ea_s, ea_e, ea_n):
                    inside_all = not any(
                        _rect_touches_ring(hole, ea_w, ea_s, ea_e, ea_n)
                        for hole in poly[1:]
                    )
                    if inside_all:
                        break
            if inside_all:
                weights[r, c] = full_area_m2 / 10000.0
                full += 1
                continue

            area_m2 = 0.0
            for poly in geom_ea:
                clipped = clip_polygon_to_rect(poly, ea_w, ea_s, ea_e, ea_n)
                if clipped:
                    area_m2 += (
                        _planar_polygon_area(clipped, ea_w, ea_s) * EQUAL_AREA_SCALE
                    )
            if area_m2 <= 0.0:
                continue
            # numerical guard: a clip can never exceed the whole cell
            area_m2 = min(area_m2, full_area_m2)
            weights[r, c] = area_m2 / 10000.0
            partial += 1
            partial_area += area_m2 / 10000.0

    total = float(weights.sum())
    return PixelWeights(
        grid=grid,
        row0=row0,
        col0=col0,
        nrows=nrows,
        ncols=ncols,
        weights_ha=weights,
        total_ha=total,
        full_pixels=full,
        partial_pixels=partial,
        partial_area_ha=partial_area,
    )


def _rect_touches_ring(ring, west, south, east, north) -> bool:
    """True if a hole may cut into the rectangle (conservative)."""
    for x, y in ring:
        if west <= x <= east and south <= y <= north:
            return True
    return (
        point_in_multipolygon(west, south, [[list(ring)]])
        or point_in_multipolygon(east, north, [[list(ring)]])
    )


# --------------------------------------------------------------------------
# sampling a second raster onto the analysis grid
# --------------------------------------------------------------------------
def footprint_in_source(
    grid: RasterGrid, row: int, col: int, source: RasterGrid
) -> Optional[Tuple[int, int, int, int]]:
    """Window of `source` covered by one cell of `grid`, across CRS if needed."""
    w, s, e, n = grid.cell_bounds(row, col)
    corners = [(w, s), (w, n), (e, n), (e, s)]
    if grid.crs != source.crs:
        projected = []
        for x, y in corners:
            lon, lat = crs_mod.to_wgs84(x, y, grid.crs)
            projected.append(crs_mod.from_wgs84(lon, lat, source.crs))
        corners = projected
    xs = [p[0] for p in corners]
    ys = [p[1] for p in corners]
    return source.window_for_bbox(min(xs), min(ys), max(xs), max(ys))


def precompute_footprints(
    target: RasterGrid, weights: PixelWeights, source: RasterGrid
) -> Dict[Tuple[int, int], Tuple[int, int, int, int]]:
    """Cache the source window of every analysis cell.

    All Sentinel-2 scenes of one area share a grid, so this is computed once and
    reused for every scene instead of once per scene: on a 1800 ha contour that
    turns six seconds of coordinate transforms into one.
    """
    out: Dict[Tuple[int, int], Tuple[int, int, int, int]] = {}
    for r in range(weights.nrows):
        for c in range(weights.ncols):
            if weights.weights_ha[r, c] <= 0:
                continue
            win = footprint_in_source(target, weights.row0 + r, weights.col0 + c, source)
            if win is not None:
                out[(r, c)] = win
    return out


def aggregate_to_grid(
    target: RasterGrid,
    weights: PixelWeights,
    source: RasterGrid,
    source_data: np.ndarray,
    mode: str = "mean",
    valid: Optional[np.ndarray] = None,
    footprints: Optional[Dict[Tuple[int, int], Tuple[int, int, int, int]]] = None,
) -> Tuple[np.ndarray, np.ndarray]:
    """Bring a finer raster onto the analysis grid.

    mode="mean"     average of valid source pixels (for spectral indices)
    mode="fraction" share of source pixels that are valid/true (for GFC loss)

    Returns (values, coverage) on the analysis window; `coverage` is the share
    of the cell for which the source had data, so a caller can tell "nothing
    there" from "not observed".
    """
    out = np.full((weights.nrows, weights.ncols), np.nan, dtype=np.float64)
    cov = np.zeros((weights.nrows, weights.ncols), dtype=np.float64)
    if valid is None:
        valid = np.isfinite(source_data)

    for r in range(weights.nrows):
        for c in range(weights.ncols):
            if weights.weights_ha[r, c] <= 0:
                continue
            if footprints is not None:
                win = footprints.get((r, c))
            else:
                win = footprint_in_source(target, weights.row0 + r, weights.col0 + c, source)
            if win is None:
                continue
            sr, sc, nr, nc = win
            block = source_data[sr : sr + nr, sc : sc + nc]
            vblock = valid[sr : sr + nr, sc : sc + nc]
            total = block.size
            if total == 0:
                continue
            good = int(vblock.sum())
            cov[r, c] = good / total
            if good == 0:
                continue
            if mode == "fraction":
                out[r, c] = good / total
            else:
                out[r, c] = float(np.nanmean(np.where(vblock, block, np.nan)))
    return out, cov
