"""Geodesic area on the WGS 84 ellipsoid, with no external dependencies.

Why this module exists
----------------------
The case statement forbids treating a raster pixel as one hectare and requires
the area of the actual intersection of every pixel with the request polygon.
That makes area a first-class quantity of the whole service, so it gets an
exact, testable implementation rather than a spherical approximation.

Method
------
Areas are computed on the *authalic sphere* of the WGS 84 ellipsoid.  The
authalic latitude beta is defined so that the area of any zone between two
parallels is preserved exactly when the ellipsoid is mapped to a sphere of
radius Rq.  A polygon is therefore mapped point-by-point from geodetic latitude
phi to beta, and its area is evaluated on that sphere with the standard
longitude-integral formula.

For the lat/lon rectangles used by this case the mapping is exact.  For an
arbitrary polygon drawn by a user the residual error comes only from the
difference between a geodesic edge and the image of a straight edge, which for
contours of a few kilometres is far below the precision of any input raster.

Verification
------------
`tests/test_geodesic_area.py` reproduces every published `area_ha` in
`data/methodology/areas.csv` and the sub-area in `sample_requests.geojson` to
better than 1e-6 relative error.  A sphere of radius 6371 km, by contrast, is
wrong by about 0.5 % at these latitudes.
"""

from __future__ import annotations

import math
from typing import Iterable, Sequence, Tuple

# --- WGS 84 defining parameters -------------------------------------------
WGS84_A = 6378137.0
WGS84_F = 1.0 / 298.257223563
WGS84_B = WGS84_A * (1.0 - WGS84_F)
WGS84_E2 = WGS84_F * (2.0 - WGS84_F)
WGS84_E = math.sqrt(WGS84_E2)

Point = Tuple[float, float]  # (lon, lat) in degrees


def _q(sin_phi: float) -> float:
    """The authalic auxiliary function q(phi) (Snyder 3-12)."""
    # Clamp guards against |sin| slightly above 1 from round-off at the poles.
    s = max(-1.0, min(1.0, sin_phi))
    return s / (1.0 - WGS84_E2 * s * s) + (1.0 / (2.0 * WGS84_E)) * math.log(
        (1.0 + WGS84_E * s) / (1.0 - WGS84_E * s)
    )


_Q_POLE = _q(1.0)

#: Radius of the sphere having the same surface area as the WGS 84 ellipsoid.
AUTHALIC_RADIUS = WGS84_A * math.sqrt(_Q_POLE * (1.0 - WGS84_E2) / 2.0)


def authalic_latitude(lat_deg: float) -> float:
    """Geodetic latitude in degrees -> authalic latitude in radians."""
    ratio = _q(math.sin(math.radians(lat_deg))) / _Q_POLE
    return math.asin(max(-1.0, min(1.0, ratio)))


def ring_area_m2(ring: Sequence[Point]) -> float:
    """Unsigned area in square metres of a single closed ring of (lon, lat).

    The ring may be given open or closed and in either winding order.
    """
    pts = list(ring)
    if len(pts) < 3:
        return 0.0
    if pts[0] == pts[-1]:
        pts = pts[:-1]
    if len(pts) < 3:
        return 0.0

    beta = [authalic_latitude(lat) for _lon, lat in pts]
    total = 0.0
    n = len(pts)
    for i in range(n):
        lon1 = pts[i][0]
        lon2 = pts[(i + 1) % n][0]
        dlon = math.radians(lon2 - lon1)
        # Keep each step on the short way round so the sum stays stable for
        # contours that straddle the antimeridian.
        if dlon > math.pi:
            dlon -= 2.0 * math.pi
        elif dlon < -math.pi:
            dlon += 2.0 * math.pi
        total += dlon * (2.0 + math.sin(beta[i]) + math.sin(beta[(i + 1) % n]))
    return abs(total * AUTHALIC_RADIUS * AUTHALIC_RADIUS / 2.0)


# --------------------------------------------------------------------------
# the plane in which areas add up
# --------------------------------------------------------------------------
# `ring_area_m2` above is, term for term, the trapezoid rule applied to
# (lambda, sin(beta)) -- longitude in radians against the sine of the authalic
# latitude.  That is the Lambert cylindrical equal-area projection of the
# authalic sphere: a plane in which area is exactly R_q^2 times the ordinary
# shoelace area, parallels are horizontal lines and meridians vertical ones.
#
# This matters for cutting a contour into pixels.  A polygon edge that is
# straight in lon/lat is *curved* in this plane, so splitting it at a pixel
# boundary and measuring the pieces does not return the area of the whole: on a
# 600 ha contour the pieces came to 32 m^2 more than the contour itself.  The
# fix is to do the cutting in the plane where the arithmetic is exact, so that
# the parts of a contour always sum to the contour.  Nothing is lost by it --
# the area of a whole contour is unchanged, and still reproduces the published
# `area_ha` of every supplied polygon.
EQUAL_AREA_SCALE = AUTHALIC_RADIUS * AUTHALIC_RADIUS


def equal_area_xy(lon_deg: float, lat_deg: float) -> Point:
    """(lon, lat) in degrees -> equal-area plane coordinates."""
    return math.radians(lon_deg), math.sin(authalic_latitude(lat_deg))


def equal_area_rings(rings: Iterable[Sequence[Point]]):
    return [[equal_area_xy(x, y) for x, y in ring] for ring in rings]


def equal_area_multipolygon(mp: Iterable[Iterable[Sequence[Point]]]):
    return [equal_area_rings(poly) for poly in mp]


def polygon_area_m2(rings: Iterable[Sequence[Point]]) -> float:
    """Area of a polygon given as [exterior, hole, hole, ...] in square metres."""
    rings = list(rings)
    if not rings:
        return 0.0
    area = ring_area_m2(rings[0])
    for hole in rings[1:]:
        area -= ring_area_m2(hole)
    return max(0.0, area)


def multipolygon_area_m2(polygons: Iterable[Iterable[Sequence[Point]]]) -> float:
    """Area of a list of polygons (each a list of rings) in square metres."""
    return sum(polygon_area_m2(p) for p in polygons)


def to_hectares(area_m2: float) -> float:
    return area_m2 / 10000.0


def to_km2(area_m2: float) -> float:
    return area_m2 / 1_000_000.0


def cell_area_m2(west: float, south: float, east: float, north: float) -> float:
    """Exact area of a lon/lat aligned cell.

    Used for the interior pixels of a raster window, where every pixel in a row
    has the same area and the value can be cached per row.
    """
    dlon = math.radians(east - west)
    return abs(
        WGS84_B
        * WGS84_B
        / 2.0
        * dlon
        * (_q(math.sin(math.radians(north))) - _q(math.sin(math.radians(south))))
    )


def meters_per_degree(lat_deg: float) -> Tuple[float, float]:
    """Metres per degree of longitude and of latitude at a given latitude.

    Only used for human-readable reporting of pixel size, never for area.
    """
    phi = math.radians(lat_deg)
    s = math.sin(phi)
    w = math.sqrt(1.0 - WGS84_E2 * s * s)
    m_per_deg_lat = math.radians(1.0) * WGS84_A * (1.0 - WGS84_E2) / (w ** 3)
    m_per_deg_lon = math.radians(1.0) * WGS84_A * math.cos(phi) / w
    return m_per_deg_lon, m_per_deg_lat
