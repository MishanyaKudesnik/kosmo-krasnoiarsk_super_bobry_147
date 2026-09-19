"""Polygon geometry in plain Python: validation, clipping, tracing.

Scope
-----
The service needs four geometric operations and nothing more:

1. validate and normalise a user-drawn contour;
2. clip a polygon by an axis-aligned rectangle (one raster pixel);
3. test whether a point lies inside a polygon;
4. trace the outline of a set of raster cells into polygons.

All four are implemented here so the whole project runs with no GIS stack
installed.  Clipping uses Sutherland-Hodgman, which is exact when the clip
window is convex -- a pixel always is.
"""

from __future__ import annotations

from typing import List, Sequence, Tuple

Point = Tuple[float, float]
Ring = List[Point]
Polygon = List[Ring]          # [exterior, hole, ...]
MultiPolygon = List[Polygon]


class GeometryError(ValueError):
    """Raised when a request geometry cannot be interpreted."""


# --------------------------------------------------------------------------
# basic helpers
# --------------------------------------------------------------------------
def close_ring(ring: Sequence[Point]) -> Ring:
    pts = [(float(x), float(y)) for x, y in ring]
    if len(pts) >= 2 and pts[0] != pts[-1]:
        pts.append(pts[0])
    return pts


def open_ring(ring: Sequence[Point]) -> Ring:
    pts = [(float(x), float(y)) for x, y in ring]
    if len(pts) >= 2 and pts[0] == pts[-1]:
        pts = pts[:-1]
    return pts


def signed_area(ring: Sequence[Point]) -> float:
    """Planar shoelace area; sign gives the winding order."""
    pts = open_ring(ring)
    n = len(pts)
    if n < 3:
        return 0.0
    total = 0.0
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        total += x1 * y2 - x2 * y1
    return total / 2.0


def bbox_of(rings: Sequence[Sequence[Point]]) -> Tuple[float, float, float, float]:
    xs: List[float] = []
    ys: List[float] = []
    for ring in rings:
        for x, y in ring:
            xs.append(x)
            ys.append(y)
    if not xs:
        raise GeometryError("Пустая геометрия")
    return min(xs), min(ys), max(xs), max(ys)


def point_in_ring(x: float, y: float, ring: Sequence[Point]) -> bool:
    """Crossing-number test.  Points exactly on an edge may return either
    value; that is acceptable because edge pixels are handled by clipping,
    never by this test."""
    pts = open_ring(ring)
    n = len(pts)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = pts[i]
        xj, yj = pts[j]
        if (yi > y) != (yj > y):
            xin = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < xin:
                inside = not inside
        j = i
    return inside


def point_in_polygon(x: float, y: float, polygon: Polygon) -> bool:
    if not polygon or not point_in_ring(x, y, polygon[0]):
        return False
    for hole in polygon[1:]:
        if point_in_ring(x, y, hole):
            return False
    return True


def point_in_multipolygon(x: float, y: float, mp: MultiPolygon) -> bool:
    return any(point_in_polygon(x, y, p) for p in mp)


# --------------------------------------------------------------------------
# clipping
# --------------------------------------------------------------------------
def clip_ring_to_rect(
    ring: Sequence[Point], west: float, south: float, east: float, north: float
) -> Ring:
    """Sutherland-Hodgman clip of a ring by an axis-aligned rectangle.

    Returns an open ring (possibly empty).  Exact for a convex clip window.
    """
    subject = open_ring(ring)
    if len(subject) < 3:
        return []

    def clip_edge(poly: Ring, inside, intersect) -> Ring:
        if not poly:
            return []
        out: Ring = []
        prev = poly[-1]
        prev_in = inside(prev)
        for cur in poly:
            cur_in = inside(cur)
            if cur_in:
                if not prev_in:
                    out.append(intersect(prev, cur))
                out.append(cur)
            elif prev_in:
                out.append(intersect(prev, cur))
            prev, prev_in = cur, cur_in
        return out

    def ix_x(p: Point, q: Point, xc: float) -> Point:
        (x1, y1), (x2, y2) = p, q
        if x2 == x1:
            return (xc, y1)
        t = (xc - x1) / (x2 - x1)
        return (xc, y1 + t * (y2 - y1))

    def ix_y(p: Point, q: Point, yc: float) -> Point:
        (x1, y1), (x2, y2) = p, q
        if y2 == y1:
            return (x1, yc)
        t = (yc - y1) / (y2 - y1)
        return (x1 + t * (x2 - x1), yc)

    poly = subject
    poly = clip_edge(poly, lambda p: p[0] >= west, lambda a, b: ix_x(a, b, west))
    poly = clip_edge(poly, lambda p: p[0] <= east, lambda a, b: ix_x(a, b, east))
    poly = clip_edge(poly, lambda p: p[1] >= south, lambda a, b: ix_y(a, b, south))
    poly = clip_edge(poly, lambda p: p[1] <= north, lambda a, b: ix_y(a, b, north))
    return poly


def clip_polygon_to_rect(
    polygon: Polygon, west: float, south: float, east: float, north: float
) -> Polygon:
    """Clip a polygon with holes by a rectangle.  Returns [] when disjoint."""
    ext = clip_ring_to_rect(polygon[0], west, south, east, north)
    if len(ext) < 3:
        return []
    out: Polygon = [ext]
    for hole in polygon[1:]:
        h = clip_ring_to_rect(hole, west, south, east, north)
        if len(h) >= 3:
            out.append(h)
    return out


def rect_fully_inside_ring(
    ring: Sequence[Point], west: float, south: float, east: float, north: float
) -> bool:
    """True when the whole rectangle lies inside the ring.

    Cheap sufficient test: all four corners inside and no ring edge crosses the
    rectangle.  Used to skip exact clipping for interior pixels.
    """
    corners = [(west, south), (west, north), (east, north), (east, south)]
    if not all(point_in_ring(cx, cy, ring) for cx, cy in corners):
        return False
    pts = open_ring(ring)
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        if max(x1, x2) < west or min(x1, x2) > east:
            continue
        if max(y1, y2) < south or min(y1, y2) > north:
            continue
        return False  # an edge is near enough to possibly cut the cell
    return True


# --------------------------------------------------------------------------
# normalisation of incoming GeoJSON
# --------------------------------------------------------------------------
def normalise_geometry(geom: dict) -> MultiPolygon:
    """Accept a GeoJSON Polygon / MultiPolygon / Feature and return rings.

    Raises GeometryError with a message meant to be shown to the user.
    """
    if not isinstance(geom, dict):
        raise GeometryError("Геометрия должна быть объектом GeoJSON")
    kind = geom.get("type")
    if kind == "Feature":
        return normalise_geometry(geom.get("geometry") or {})
    if kind == "FeatureCollection":
        feats = geom.get("features") or []
        out: MultiPolygon = []
        for f in feats:
            out.extend(normalise_geometry(f))
        if not out:
            raise GeometryError("В FeatureCollection нет ни одного полигона")
        return out
    if kind == "Polygon":
        coords = geom.get("coordinates") or []
        if not coords:
            raise GeometryError("У полигона нет координат")
        return [[_ring(r) for r in coords]]
    if kind == "MultiPolygon":
        coords = geom.get("coordinates") or []
        if not coords:
            raise GeometryError("У мультиполигона нет координат")
        return [[_ring(r) for r in poly] for poly in coords]
    raise GeometryError(
        f"Тип геометрии {kind!r} не поддерживается: нужен Polygon, MultiPolygon, "
        "Feature или FeatureCollection"
    )


def _ring(raw: Sequence[Sequence[float]]) -> Ring:
    pts: Ring = []
    for p in raw:
        if len(p) < 2:
            raise GeometryError("В координате должно быть минимум два числа")
        lon, lat = float(p[0]), float(p[1])
        if not (-180.0 <= lon <= 180.0) or not (-90.0 <= lat <= 90.0):
            raise GeometryError(
                f"Координата ({lon}, {lat}) вне пределов WGS 84: "
                "контур должен быть в EPSG:4326"
            )
        pts.append((lon, lat))
    pts = open_ring(pts)
    if len(pts) < 3:
        raise GeometryError("В контуре должно быть не меньше трёх различных точек")
    return pts


def multipolygon_bbox(mp: MultiPolygon) -> Tuple[float, float, float, float]:
    rings = [r for poly in mp for r in poly]
    return bbox_of(rings)


# --------------------------------------------------------------------------
# tracing raster cells into polygons
# --------------------------------------------------------------------------
def trace_cells(
    cells: Sequence[Tuple[int, int]],
    origin_x: float,
    origin_y: float,
    dx: float,
    dy: float,
) -> MultiPolygon:
    """Trace a set of (row, col) cells into closed outlines.

    Every cell contributes four edges; edges shared by two selected cells
    cancel.  The surviving edges are stitched into rings.  `dy` is negative for
    a north-up raster, matching the GeoTIFF convention.
    """
    selected = set(cells)
    edges: dict = {}

    def add(p: Point, q: Point) -> None:
        # store directed edges so the stitch keeps a consistent winding
        edges.setdefault(p, []).append(q)

    for (r, c) in selected:
        x0 = origin_x + c * dx
        x1 = origin_x + (c + 1) * dx
        y0 = origin_y + r * dy
        y1 = origin_y + (r + 1) * dy
        tl, tr = (x0, y0), (x1, y0)
        br, bl = (x1, y1), (x0, y1)
        if (r - 1, c) not in selected:
            add(tl, tr)
        if (r, c + 1) not in selected:
            add(tr, br)
        if (r + 1, c) not in selected:
            add(br, bl)
        if (r, c - 1) not in selected:
            add(bl, tl)

    rings: MultiPolygon = []
    while edges:
        start = next(iter(edges))
        ring: Ring = [start]
        cur = start
        guard = 0
        while True:
            guard += 1
            if guard > 4 * len(selected) + 8:
                break
            nxts = edges.get(cur)
            if not nxts:
                break
            nxt = nxts.pop()
            if not nxts:
                edges.pop(cur, None)
            ring.append(nxt)
            cur = nxt
            if cur == start:
                break
        if len(ring) >= 4:
            rings.append([_simplify_collinear(ring)])
    return rings


def _simplify_collinear(ring: Sequence[Point]) -> Ring:
    """Drop vertices that sit on a straight run, keeping the shape identical."""
    pts = open_ring(ring)
    if len(pts) < 3:
        return list(pts)
    out: Ring = []
    n = len(pts)
    for i in range(n):
        a = pts[(i - 1) % n]
        b = pts[i]
        c = pts[(i + 1) % n]
        cross = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        if abs(cross) > 1e-15:
            out.append(b)
    return out if len(out) >= 3 else list(pts)


# --------------------------------------------------------------------------
# vectorised point-in-polygon
# --------------------------------------------------------------------------
def points_in_multipolygon(xs, ys, mp: MultiPolygon):
    """Crossing-number test evaluated on numpy arrays.

    Used for raster masks, where tens of thousands of pixel centres have to be
    classified at once.  Holes subtract, as in :func:`point_in_polygon`.
    """
    import numpy as np

    xs = np.asarray(xs, dtype=np.float64)
    ys = np.asarray(ys, dtype=np.float64)
    inside = np.zeros(xs.shape, dtype=bool)

    def ring_mask(ring: Sequence[Point]) -> "np.ndarray":
        pts = open_ring(ring)
        n = len(pts)
        acc = np.zeros(xs.shape, dtype=bool)
        for i in range(n):
            x1, y1 = pts[i]
            x2, y2 = pts[(i - 1) % n]
            cond = (y1 > ys) != (y2 > ys)
            if not cond.any():
                continue
            with np.errstate(divide="ignore", invalid="ignore"):
                xin = (x2 - x1) * (ys - y1) / (y2 - y1) + x1
            acc ^= cond & (xs < xin)
        return acc

    for poly in mp:
        m = ring_mask(poly[0])
        for hole in poly[1:]:
            m &= ~ring_mask(hole)
        inside |= m
    return inside
