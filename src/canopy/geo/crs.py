"""Coordinate reference systems used by this dataset, in plain Python.

Four CRS families meet in a single analysis:

* EPSG:4326        geographic       ESA CCI Biomass, Hansen GFC, request geometry
* EPSG:32636/7/8   UTM zones 36-38  Sentinel-2 L2A tiles 36VVH / 37VEF / 38ULF
* MODIS sinusoidal                  MCD64A1 burned area, sphere R = 6371007.181 m

Only conversions to and from WGS 84 geographic coordinates are needed, because
the analysis grid is always the native CCI grid in EPSG:4326 and every other
source is sampled into it.  Implementing the two projections directly removes
the last reason to depend on a GIS stack.

Accuracy: the transverse Mercator series is Snyder's, good to a few millimetres
within a UTM zone -- far tighter than the 20 m Sentinel-2 grid it is used for.
"""

from __future__ import annotations

import math
from typing import Tuple

from .ellipsoid import WGS84_A, WGS84_E2, WGS84_F

_K0 = 0.9996
_FALSE_EASTING = 500000.0
_FALSE_NORTHING_S = 10000000.0
_EP2 = WGS84_E2 / (1.0 - WGS84_E2)          # second eccentricity squared

#: Sphere used by the MODIS sinusoidal grid (identical to the authalic radius).
MODIS_SPHERE_R = 6371007.181


class CRSError(ValueError):
    pass


# --------------------------------------------------------------------------
# UTM  (EPSG:326xx north, 327xx south)
# --------------------------------------------------------------------------
def utm_zone_for_lon(lon: float) -> int:
    return int((lon + 180.0) / 6.0) % 60 + 1


def utm_epsg(zone: int, northern: bool = True) -> int:
    return (32600 if northern else 32700) + zone


def parse_utm_epsg(epsg: int) -> Tuple[int, bool]:
    if 32601 <= epsg <= 32660:
        return epsg - 32600, True
    if 32701 <= epsg <= 32760:
        return epsg - 32700, False
    raise CRSError(f"EPSG:{epsg} is not a UTM zone")


_E2, _E4, _E6, _E8 = (WGS84_E2 ** k for k in (1, 2, 3, 4))
#: leading coefficient of the meridional arc, i.e. M(phi) ~ A * _M0 * phi
_M0 = 1 - _E2 / 4 - 3 * _E4 / 64 - 5 * _E6 / 256 - 175 * _E8 / 16384


def _meridional_arc(phi: float) -> float:
    """Distance along the meridian from the equator, to order e^8."""
    return WGS84_A * (
        _M0 * phi
        - (3 * _E2 / 8 + 3 * _E4 / 32 + 45 * _E6 / 1024 + 105 * _E8 / 4096)
        * math.sin(2 * phi)
        + (15 * _E4 / 256 + 45 * _E6 / 1024 + 525 * _E8 / 16384) * math.sin(4 * phi)
        - (35 * _E6 / 3072 + 175 * _E8 / 12288) * math.sin(6 * phi)
        + (315 * _E8 / 131072) * math.sin(8 * phi)
    )


def _meridional_radius(phi: float) -> float:
    """dM/dphi -- the meridional radius of curvature."""
    s = math.sin(phi)
    return WGS84_A * (1.0 - WGS84_E2) / (1.0 - WGS84_E2 * s * s) ** 1.5


def _footpoint_latitude(m: float) -> float:
    """Inverse of :func:`_meridional_arc`.

    Snyder gives a truncated series for this step; its residual is about half a
    millimetre and, being independent of the distance from the central meridian,
    it biases every inverse transform in the same direction.  Newton's method on
    the arc itself converges in three passes to machine precision and makes the
    forward and inverse transforms exact inverses of each other, which is what
    the pixel-footprint arithmetic downstream relies on.
    """
    phi = m / (WGS84_A * _M0)
    for _ in range(6):
        delta = (_meridional_arc(phi) - m) / _meridional_radius(phi)
        phi -= delta
        if abs(delta) < 1e-14:
            break
    return phi


def wgs84_to_utm(lon: float, lat: float, zone: int, northern: bool = True) -> Tuple[float, float]:
    phi = math.radians(lat)
    lam = math.radians(lon)
    lam0 = math.radians((zone - 1) * 6 - 180 + 3)

    sin_p, cos_p, tan_p = math.sin(phi), math.cos(phi), math.tan(phi)
    n = WGS84_A / math.sqrt(1.0 - WGS84_E2 * sin_p * sin_p)
    t = tan_p * tan_p
    c = _EP2 * cos_p * cos_p
    a = (lam - lam0) * cos_p
    m = _meridional_arc(phi)

    x = _K0 * n * (
        a
        + (1 - t + c) * a ** 3 / 6.0
        + (5 - 18 * t + t * t + 72 * c - 58 * _EP2) * a ** 5 / 120.0
    ) + _FALSE_EASTING
    y = _K0 * (
        m
        + n * tan_p * (
            a * a / 2.0
            + (5 - t + 9 * c + 4 * c * c) * a ** 4 / 24.0
            + (61 - 58 * t + t * t + 600 * c - 330 * _EP2) * a ** 6 / 720.0
        )
    )
    if not northern:
        y += _FALSE_NORTHING_S
    return x, y


def utm_to_wgs84(x: float, y: float, zone: int, northern: bool = True) -> Tuple[float, float]:
    x -= _FALSE_EASTING
    if not northern:
        y -= _FALSE_NORTHING_S
    lam0 = math.radians((zone - 1) * 6 - 180 + 3)

    m = y / _K0
    phi1 = _footpoint_latitude(m)
    sin_1, cos_1, tan_1 = math.sin(phi1), math.cos(phi1), math.tan(phi1)
    c1 = _EP2 * cos_1 * cos_1
    t1 = tan_1 * tan_1
    n1 = WGS84_A / math.sqrt(1 - WGS84_E2 * sin_1 * sin_1)
    r1 = WGS84_A * (1 - WGS84_E2) / (1 - WGS84_E2 * sin_1 * sin_1) ** 1.5
    d = x / (n1 * _K0)

    phi = phi1 - (n1 * tan_1 / r1) * (
        d * d / 2.0
        - (5 + 3 * t1 + 10 * c1 - 4 * c1 * c1 - 9 * _EP2) * d ** 4 / 24.0
        + (61 + 90 * t1 + 298 * c1 + 45 * t1 * t1 - 252 * _EP2 - 3 * c1 * c1) * d ** 6 / 720.0
    )
    lam = lam0 + (
        d
        - (1 + 2 * t1 + c1) * d ** 3 / 6.0
        + (5 - 2 * c1 + 28 * t1 - 3 * c1 * c1 + 8 * _EP2 + 24 * t1 * t1) * d ** 5 / 120.0
    ) / cos_1
    return math.degrees(lam), math.degrees(phi)


# --------------------------------------------------------------------------
# MODIS sinusoidal
# --------------------------------------------------------------------------
def wgs84_to_sinusoidal(lon: float, lat: float) -> Tuple[float, float]:
    phi = math.radians(lat)
    lam = math.radians(lon)
    return MODIS_SPHERE_R * lam * math.cos(phi), MODIS_SPHERE_R * phi


def sinusoidal_to_wgs84(x: float, y: float) -> Tuple[float, float]:
    phi = y / MODIS_SPHERE_R
    cos_p = math.cos(phi)
    if abs(cos_p) < 1e-12:
        return 0.0, math.degrees(phi)
    return math.degrees(x / (MODIS_SPHERE_R * cos_p)), math.degrees(phi)


# --------------------------------------------------------------------------
# generic dispatch used by the raster layer
# --------------------------------------------------------------------------
SINUSOIDAL = "MODIS_SINUSOIDAL"


def to_wgs84(x: float, y: float, crs) -> Tuple[float, float]:
    """Project a native raster coordinate to (lon, lat)."""
    if crs in (4326, "4326", "EPSG:4326", None):
        return x, y
    if crs == SINUSOIDAL:
        return sinusoidal_to_wgs84(x, y)
    zone, northern = parse_utm_epsg(int(crs))
    return utm_to_wgs84(x, y, zone, northern)


def from_wgs84(lon: float, lat: float, crs) -> Tuple[float, float]:
    """Project (lon, lat) into a raster's native coordinates."""
    if crs in (4326, "4326", "EPSG:4326", None):
        return lon, lat
    if crs == SINUSOIDAL:
        return wgs84_to_sinusoidal(lon, lat)
    zone, northern = parse_utm_epsg(int(crs))
    return wgs84_to_utm(lon, lat, zone, northern)


def describe(crs) -> str:
    if crs in (4326, "4326", "EPSG:4326", None):
        return "EPSG:4326 (WGS 84 geographic)"
    if crs == SINUSOIDAL:
        return "MODIS sinusoidal (sphere R=6371007.181 m)"
    try:
        zone, northern = parse_utm_epsg(int(crs))
        return f"EPSG:{int(crs)} (UTM zone {zone}{'N' if northern else 'S'})"
    except CRSError:
        return str(crs)


# --------------------------------------------------------------------------
# vectorised inverse projection
# --------------------------------------------------------------------------
def _footpoint_latitude_array(m):
    """Array form of :func:`_footpoint_latitude`, same Newton iteration."""
    import numpy as np

    phi = np.asarray(m, dtype=np.float64) / (WGS84_A * _M0)
    for _ in range(6):
        s = np.sin(phi)
        arc = WGS84_A * (
            _M0 * phi
            - (3 * _E2 / 8 + 3 * _E4 / 32 + 45 * _E6 / 1024 + 105 * _E8 / 4096)
            * np.sin(2 * phi)
            + (15 * _E4 / 256 + 45 * _E6 / 1024 + 525 * _E8 / 16384) * np.sin(4 * phi)
            - (35 * _E6 / 3072 + 175 * _E8 / 12288) * np.sin(6 * phi)
            + (315 * _E8 / 131072) * np.sin(8 * phi)
        )
        rho = WGS84_A * (1.0 - WGS84_E2) / (1.0 - WGS84_E2 * s * s) ** 1.5
        delta = (arc - m) / rho
        phi = phi - delta
        if np.max(np.abs(delta)) < 1e-14:
            break
    return phi


def utm_to_wgs84_array(x, y, zone: int, northern: bool = True):
    """numpy version of :func:`utm_to_wgs84`.

    A Sentinel-2 crop is ~50 000 pixels and the contour mask needs every centre
    in geographic coordinates; doing that pixel by pixel dominated the runtime of
    a whole analysis, so the same series is evaluated on arrays.
    """
    import numpy as np

    x = np.asarray(x, dtype=np.float64) - _FALSE_EASTING
    y = np.asarray(y, dtype=np.float64)
    if not northern:
        y = y - _FALSE_NORTHING_S
    lam0 = math.radians((zone - 1) * 6 - 180 + 3)

    m = y / _K0
    phi1 = _footpoint_latitude_array(m)
    sin_1, cos_1, tan_1 = np.sin(phi1), np.cos(phi1), np.tan(phi1)
    c1 = _EP2 * cos_1 * cos_1
    t1 = tan_1 * tan_1
    n1 = WGS84_A / np.sqrt(1 - WGS84_E2 * sin_1 * sin_1)
    r1 = WGS84_A * (1 - WGS84_E2) / (1 - WGS84_E2 * sin_1 * sin_1) ** 1.5
    d = x / (n1 * _K0)

    phi = phi1 - (n1 * tan_1 / r1) * (
        d ** 2 / 2.0
        - (5 + 3 * t1 + 10 * c1 - 4 * c1 * c1 - 9 * _EP2) * d ** 4 / 24.0
        + (61 + 90 * t1 + 298 * c1 + 45 * t1 * t1 - 252 * _EP2 - 3 * c1 * c1) * d ** 6 / 720.0
    )
    lam = lam0 + (
        d
        - (1 + 2 * t1 + c1) * d ** 3 / 6.0
        + (5 - 2 * c1 + 28 * t1 - 3 * c1 * c1 + 8 * _EP2 + 24 * t1 * t1) * d ** 5 / 120.0
    ) / cos_1
    return np.degrees(lam), np.degrees(phi)


def sinusoidal_to_wgs84_array(x, y):
    import numpy as np

    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    phi = y / MODIS_SPHERE_R
    cos_p = np.cos(phi)
    lon = np.where(np.abs(cos_p) < 1e-12, 0.0, x / (MODIS_SPHERE_R * np.maximum(cos_p, 1e-12)))
    return np.degrees(lon), np.degrees(phi)


def to_wgs84_array(x, y, crs):
    """Vectorised counterpart of :func:`to_wgs84`."""
    import numpy as np

    if crs in (4326, "4326", "EPSG:4326", None):
        return np.asarray(x, dtype=np.float64), np.asarray(y, dtype=np.float64)
    if crs == SINUSOIDAL:
        return sinusoidal_to_wgs84_array(x, y)
    zone, northern = parse_utm_epsg(int(crs))
    return utm_to_wgs84_array(x, y, zone, northern)
