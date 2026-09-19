"""Correctness tests for the parts a jury can check against the case itself.

Run with `python3 tests/run_tests.py`.  There is no pytest dependency on
purpose: the project must be verifiable on a clean Python.
"""

from __future__ import annotations

import csv
import json
import math
import os

import numpy as np

from canopy.baseline.table import BaselineTable
from canopy.carbon.stock import compute_carbon
from canopy.core.config import get_settings, project_root
from canopy.core.status import RequestError
from canopy.dal.catalog import CCI_AGB, get_catalog
from canopy.engine import AnalysisRequest, _aligned_grid, _paste, _valid_cci, run_analysis
from canopy.evidence.ladder import COMPETENCE, evaluate_ladder, may_support
from canopy.geo.crs import (
    _meridional_arc,
    sinusoidal_to_wgs84,
    utm_to_wgs84,
    utm_to_wgs84_array,
    utm_zone_for_lon,
    wgs84_to_sinusoidal,
    wgs84_to_utm,
)
from canopy.geo.ellipsoid import multipolygon_area_m2
from canopy.geo.grid import RasterGrid, pixel_weights
from canopy.geo.polygon import clip_polygon_to_rect, multipolygon_bbox, normalise_geometry
from canopy.rio.tiff import GeoTiff, write_geotiff
from canopy.units.potential import compute_units

SETTINGS = get_settings()
ROOT = project_root()


# ---------------------------------------------------------------------------
def test_case_worked_example():
    """The worked example printed in the case statement, to the last decimal.

    100 ha, one year, biomass 100 -> 104 t/ha.  Every intermediate value the
    case prints has to come out of our chain unchanged, otherwise the carbon
    engine is wrong somewhere and no other test would tell us where.
    """
    w = np.full((10, 10), 1.0)
    ok = np.ones((10, 10), dtype=bool)
    c = compute_carbon(
        2019, 2020,
        np.full((10, 10), 100.0), np.full((10, 10), 104.0),
        w, ok, ok, cf=0.47, co2_per_c=44 / 12, area_requested_ha=100.0,
    )
    assert abs(c.state_start.mean_tc_ha - 47.0) < 1e-9, c.state_start.mean_tc_ha
    assert abs(c.state_end.mean_tc_ha - 48.88) < 1e-9, c.state_end.mean_tc_ha
    assert abs(c.delta_c_tc - 188.0) < 1e-9, c.delta_c_tc
    assert abs(c.e_total.value - (-689.3333333333333)) < 1e-6, c.e_total.value
    assert abs(c.e_per_ha_year.value - (-6.893333333333)) < 1e-6

    e_base = -0.47 * 100 * 44 / 12
    assert abs(e_base - (-172.33333333)) < 1e-6
    h = 103.4
    u = compute_units(
        c.e_total.value, e_base, c.e_total.value - h, c.e_total.value + h,
        leakage=0.0, allowance=0.10, stop_ratio=1.0, buffer_share=0.15,
        coverage_fraction=1.0,
    )
    assert abs(u.r.value - 517.0) < 1e-6, u.r.value
    assert abs(u.h.value - 103.4) < 1e-6
    assert abs(u.h_over_r.value - 0.20) < 1e-9
    assert abs(u.unc.value - 0.10) < 1e-9
    assert abs(u.r_adj.value - 465.3) < 1e-6
    assert abs(u.buffer.value - 69.795) < 1e-6
    assert u.q.value == 395, u.q.value


def test_geodesic_area_matches_published():
    """Our area must reproduce every published `area_ha` to 1e-6 relative.

    A sphere of radius 6371 km is off by about 0.5 % at these latitudes, which
    would move every stock and every result by the same amount.
    """
    path = SETTINGS.methodology_path("areas.csv")
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert rows, "areas.csv is empty"
    for row in rows:
        w, s = float(row["bbox_west"]), float(row["bbox_south"])
        e, n = float(row["bbox_east"]), float(row["bbox_north"])
        geom = normalise_geometry(
            {"type": "Polygon", "coordinates": [[[e, s], [e, n], [w, n], [w, s], [e, s]]]}
        )
        got = multipolygon_area_m2(geom) / 10000.0
        want = float(row["area_ha"])
        rel = abs(got - want) / want
        assert rel < 1e-6, f"{row['aoi_id']}: {got} vs {want} (rel {rel:.2e})"

    sample = os.path.join(SETTINGS.data_root(), "sample_requests.geojson")
    if os.path.exists(sample):
        with open(sample, encoding="utf-8") as fh:
            fc = json.load(fh)
        for f in fc["features"]:
            geom = normalise_geometry(f["geometry"])
            got = multipolygon_area_m2(geom) / 10000.0
            want = float(f["properties"]["area_ha"])
            assert abs(got - want) / want < 1e-6, f"{f['id']}: {got} vs {want}"


def test_pixel_is_not_one_hectare():
    """The assumption the case forbids, quantified on the real grids."""
    step = 1.0 / 1125.0
    for lat, expected_lo, expected_hi in ((54.87, 0.55, 0.58), (56.61, 0.53, 0.56), (59.45, 0.48, 0.51)):
        grid = RasterGrid(40.0, lat + 0.02, step, -step, 80, 50, 4326)
        area = grid.nominal_pixel_area_ha(lat)
        assert expected_lo < area < expected_hi, (lat, area)
        assert area < 1.0, "a CCI pixel is never a hectare at these latitudes"


def test_pixel_weights_sum_to_area():
    """Summed intersection areas equal the geodesic area of the contour.

    The parts of a contour must add up to the contour -- otherwise the figure on
    the screen and the figure the calculation actually integrated over are two
    different numbers, and the gap reported for partial coverage is wrong by the
    difference.  The intersection is therefore carried out in the equal-area
    plane, where cutting is exact; 1e-10 here is round-off, not method.
    """
    shapes = {
        "aligned rectangle": [[43.234, 54.85025], [43.234, 54.89025],
                              [43.17, 54.89025], [43.17, 54.85025], [43.234, 54.85025]],
        "pentagon": [[43.182, 54.858], [43.213, 54.861], [43.219, 54.879],
                     [43.196, 54.888], [43.179, 54.874], [43.182, 54.858]],
        "thin sliver": [[43.180, 54.8700], [43.220, 54.8702], [43.220, 54.8712],
                        [43.180, 54.8710], [43.180, 54.8700]],
    }
    for name, ring in shapes.items():
        geom = normalise_geometry({"type": "Polygon", "coordinates": [ring]})
        grid = _aligned_grid(multipolygon_bbox(geom))
        pw = pixel_weights(grid, geom)
        truth = multipolygon_area_m2(geom) / 10000.0
        assert abs(pw.total_ha - truth) / truth < 1e-10, (name, pw.total_ha, truth)
        assert pw.partial_pixels > 0, f"{name}: a contour with no edge pixels"

    # a contour with a hole: the hole must be removed, not merely outlined
    outer = [[43.18, 54.86], [43.22, 54.86], [43.22, 54.885], [43.18, 54.885], [43.18, 54.86]]
    hole = [[43.19, 54.868], [43.21, 54.868], [43.21, 54.877], [43.19, 54.877], [43.19, 54.868]]
    holed = normalise_geometry({"type": "Polygon", "coordinates": [outer, hole]})
    pw = pixel_weights(_aligned_grid(multipolygon_bbox(holed)), holed)
    truth = multipolygon_area_m2(holed) / 10000.0
    assert abs(pw.total_ha - truth) / truth < 1e-10, (pw.total_ha, truth)
    solid = normalise_geometry({"type": "Polygon", "coordinates": [outer]})
    assert pw.total_ha < multipolygon_area_m2(solid) / 10000.0 * 0.9


def test_mean_stock_matches_baseline_reference():
    """Calibration against the numbers the organisers published.

    `baseline.csv` carries the mean stock for 2015 and 2019 computed by the case
    authors from the same rasters.  Reproducing them exercises raster reading,
    CF, and the area weighting in one shot.  With integer rasters an exact match
    is impossible -- one unit of quantisation spread over a contour is about
    2e-6 in relative terms -- so the tolerance is 1e-4, which still fails
    immediately if the aggregation method is wrong (centre-in-polygon and
    all_touched are off by 0.4 % and 3.6 % here).
    """
    table = BaselineTable()
    catalog = get_catalog()
    checked = 0
    for aoi_id, area in table.areas.items():
        geom = normalise_geometry(
            {
                "type": "Polygon",
                "coordinates": [[
                    [area.east, area.south], [area.east, area.north],
                    [area.west, area.north], [area.west, area.south],
                    [area.east, area.south],
                ]],
            }
        )
        bbox = (area.west, area.south, area.east, area.north)
        grid = _aligned_grid(bbox)
        pw = pixel_weights(grid, geom)
        for year, published in ((2015, area.reference_2015), (2019, area.reference_2019)):
            assets = catalog.query(CCI_AGB, bbox=bbox, year=year)
            if not assets:
                continue
            agb, have = _paste(grid, assets, band=1)
            sd, _ = _paste(grid, assets, band=2)
            valid = _valid_cci(agb, sd, have)
            sl = (slice(pw.row0, pw.row0 + pw.nrows), slice(pw.col0, pw.col0 + pw.ncols))
            w = np.where(valid[sl], pw.weights_ha, 0.0)
            mean = float((agb[sl] * w).sum() / w.sum()) * SETTINGS.cf_agb
            rel = abs(mean - published) / published
            assert rel < 1e-4, f"{aoi_id} {year}: {mean:.6f} vs {published:.6f} (rel {rel:.2e})"
            checked += 1
    assert checked >= 2, "no biomass rasters were available to calibrate against"


def test_units_edge_cases():
    """Every branch the case names explicitly."""
    base = dict(leakage=0.0, allowance=0.10, stop_ratio=1.0, buffer_share=0.15,
                coverage_fraction=1.0)

    # R <= 0 -> Q = 0 and H/R is not computed at all
    u = compute_units(100.0, 50.0, 40.0, 60.0, **base)
    assert u.q.value == 0
    assert u.h_over_r.value is None, "H/R must not be computed when R <= 0"
    assert u.status.code == "RESULT_NOT_ABOVE_BASELINE"

    # R exactly 0
    u = compute_units(100.0, 100.0, 90.0, 110.0, **base)
    assert u.r.value == 0 and u.q.value == 0 and u.h_over_r.value is None

    # H/R exactly 1 -> Q = 0, boundary inclusive
    u = compute_units(0.0, 100.0, -100.0, 100.0, **base)
    assert abs(u.h_over_r.value - 1.0) < 1e-12
    assert u.q.value == 0 and u.status.code == "UNCERTAINTY_EXCEEDS_RESULT"

    # H/R > 1
    u = compute_units(0.0, 100.0, -200.0, 200.0, **base)
    assert u.q.value == 0 and u.status.code == "UNCERTAINTY_EXCEEDS_RESULT"

    # H/R below the allowance -> no deduction
    u = compute_units(0.0, 1000.0, -50.0, 50.0, **base)
    assert abs(u.unc.value) < 1e-12
    assert abs(u.r_adj.value - 1000.0) < 1e-9

    # partial coverage -> units unavailable regardless of everything else
    u = compute_units(0.0, 1000.0, -50.0, 50.0, leakage=0.0, allowance=0.10,
                      stop_ratio=1.0, buffer_share=0.15, coverage_fraction=0.999)
    assert u.q.value is None and u.status.code == "UNITS_UNAVAILABLE_PARTIAL_COVERAGE"

    # missing baseline -> unavailable, not zero
    u = compute_units(0.0, None, -50.0, 50.0, **base)
    assert u.q.value is None and u.status.code == "UNITS_UNAVAILABLE_NO_BASELINE"

    # H = 0 is allowed but flagged
    u = compute_units(0.0, 1000.0, 0.0, 0.0, **base)
    assert u.h.value == 0.0 and u.q.value is not None and u.h.notes

    # Q floors down and the remainder is reported, never carried
    u = compute_units(0.0, 1000.0, -100.0, 100.0, **base)
    net = u.r_adj.value - u.buffer.value
    assert u.q.value == math.floor(net)
    assert abs(u.remainder - (net - math.floor(net))) < 1e-9


def test_subarea_uses_parent_trajectory():
    """CHECK_TRANSFER_01: parent trajectory applied to the request's own area."""
    path = os.path.join(SETTINGS.data_root(), "sample_requests.geojson")
    with open(path, encoding="utf-8") as fh:
        feature = json.load(fh)["features"][0]
    geom = normalise_geometry(feature["geometry"])
    props = feature["properties"]
    area_ha = multipolygon_area_m2(geom) / 10000.0
    table = BaselineTable()
    res = table.evaluate(geom, props["year_start"], props["year_end"], area_ha, 44 / 12)
    assert res.status.ok, res.status.code
    assert len(res.parts) == 1
    part = res.parts[0]
    assert part.aoi_id == props["parent_aoi_id"]
    assert abs(part.area_ha - props["area_ha"]) / props["area_ha"] < 1e-6
    parent = table.areas[props["parent_aoi_id"]]
    expected_delta = parent.stock(props["year_end"]) - parent.stock(props["year_start"])
    assert abs(part.delta_tc_ha - expected_delta) < 1e-12
    # the parent's own area is more than twice the sub-area: the trajectory is
    # per hectare, so E_base must scale with the request, not with the parent
    assert part.area_ha < parent.declared_area_ha


def test_multi_area_request_sums_parts():
    """A contour crossing two areas is summed part by part."""
    table = BaselineTable()
    ids = sorted(table.areas)
    a, b = table.areas[ids[0]], table.areas[ids[1]]
    # a contour that covers both bounding boxes entirely would be too large, so
    # take a thin strip through each and union them as a MultiPolygon
    geom = normalise_geometry(
        {
            "type": "MultiPolygon",
            "coordinates": [
                [[[a.west, a.south], [a.west + 0.004, a.south], [a.west + 0.004, a.south + 0.004],
                  [a.west, a.south + 0.004], [a.west, a.south]]],
                [[[b.west, b.south], [b.west + 0.004, b.south], [b.west + 0.004, b.south + 0.004],
                  [b.west, b.south + 0.004], [b.west, b.south]]],
            ],
        }
    )
    area_ha = multipolygon_area_m2(geom) / 10000.0
    res = table.evaluate(geom, 2019, 2024, area_ha, 44 / 12)
    assert len(res.parts) == 2, [p.aoi_id for p in res.parts]
    assert abs(sum(p.e_base_tco2e for p in res.parts) - res.e_base.value) < 1e-6


def test_request_validation():
    """Bad requests are refused with the code the API contract promises."""
    good = {"type": "Polygon", "coordinates": [[[43.17, 54.86], [43.19, 54.86],
                                                [43.19, 54.87], [43.17, 54.87], [43.17, 54.86]]]}
    for payload, code in (
        ({"geometry": good, "year_start": 2024, "year_end": 2019}, "INVALID_PERIOD"),
        ({"geometry": good, "year_start": 2020, "year_end": 2020}, "INVALID_PERIOD"),
        ({"geometry": None, "year_start": 2019, "year_end": 2024}, "INVALID_GEOMETRY"),
        ({"geometry": {"type": "Polygon", "coordinates": [[[0, 0], [0, 3], [3, 3], [3, 0], [0, 0]]]},
          "year_start": 2019, "year_end": 2024}, "AOI_TOO_LARGE"),
        ({"geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
          "year_start": 2019, "year_end": 2024}, "INVALID_GEOMETRY"),
    ):
        try:
            AnalysisRequest.parse(payload)
            raise AssertionError(f"expected {code} for {payload.get('year_start')}")
        except RequestError as exc:
            assert exc.status.code == code, (exc.status.code, code)


def test_modis_never_provides_area():
    """A methodological rule, pinned by a test rather than by a comment.

    MODIS marks 88 of 88 cells inside RU_MORDOVIA_04, which is 1889 ha against
    an area of 1832.7 ha -- 103.1 % of the site.  Anything that took area from
    this product would be wrong by construction.
    """
    assert may_support("MODIS_BURN", "date") is True
    assert may_support("MODIS_BURN", "cause") is True
    assert may_support("MODIS_BURN", "area") is False
    assert may_support("MODIS_BURN", "geometry") is False
    assert may_support("MODIS_BURN", "tonnes") is False
    assert COMPETENCE["CCI_AGB"]["tonnes"] is True
    assert COMPETENCE["S2_REFLECTANCE"]["tonnes"] is False

    nominal = 88 * (463.3127165279167 ** 2) / 10000.0
    assert nominal / 1832.7456 > 1.0, "the 103 % figure must stay reproducible"


def test_attribution_ladder_distinguishes_silence_from_denial():
    """C and D differ: 'looked and saw nothing' refutes, 'could not look' does not."""
    a1 = evaluate_ladder(True, True, True, True, True)
    assert a1.rung == "A1"
    a2 = evaluate_ladder(True, True, True, False, True)
    assert a2.rung == "A2"
    b = evaluate_ladder(True, True, None, None, True)
    assert b.rung == "B"
    c = evaluate_ladder(True, False, False, False, True)
    assert c.rung == "C", c.rung
    d = evaluate_ladder(True, None, None, None, True)
    assert d.rung == "D", d.rung
    assert d.missing, "an unresolved rung always says what was missing"
    # a conflict between date windows must be visible in the verdict
    conflict = evaluate_ladder(True, True, True, True, False)
    assert conflict.rung != "A1"


def test_crs_roundtrip():
    """Four CRS families meet in one analysis; each must survive a round trip.

    The tolerance is stated on the ground rather than in degrees: one millimetre,
    against a 20 m Sentinel-2 pixel and a ~99 m CCI pixel.  What matters is not
    the absolute accuracy of Snyder's series but that the forward and inverse
    transforms are inverses of each other, because a pixel footprint is built by
    projecting corners one way and the contour the other.
    """
    worst = 0.0
    for lon, lat in ((43.20, 54.87), (32.94, 56.61), (40.70, 59.45),
                     (41.90, 59.90), (33.00, 56.00), (44.90, 54.00)):
        zone = utm_zone_for_lon(lon)
        x, y = wgs84_to_utm(lon, lat, zone)
        lon2, lat2 = utm_to_wgs84(x, y, zone)
        metres = math.hypot(
            (lat2 - lat) * 111320.0,
            (lon2 - lon) * 111320.0 * math.cos(math.radians(lat)),
        )
        worst = max(worst, metres)
        assert metres < 1e-3, (lon, lat, metres)

        sx, sy = wgs84_to_sinusoidal(lon, lat)
        lon3, lat3 = sinusoidal_to_wgs84(sx, sy)
        assert abs(lon - lon3) < 1e-9 and abs(lat - lat3) < 1e-9

    # the meridian quarter of WGS 84, the one number that pins the arc series
    assert abs(_meridional_arc(math.pi / 2) - 10001965.7293) < 1e-3

    # the scalar and array paths are used in the same analysis; they must agree
    xs, ys = [], []
    for lon, lat in ((32.94, 56.61), (33.00, 56.00), (34.50, 57.00)):
        x, y = wgs84_to_utm(lon, lat, 36)
        xs.append(x)
        ys.append(y)
    alon, alat = utm_to_wgs84_array(np.array(xs), np.array(ys), 36)
    for i, (x, y) in enumerate(zip(xs, ys)):
        slon, slat = utm_to_wgs84(x, y, 36)
        assert abs(slon - alon[i]) < 1e-12 and abs(slat - alat[i]) < 1e-12

    # the Sentinel-2 tiles of this dataset sit in three different zones
    assert utm_zone_for_lon(32.94) == 36
    assert utm_zone_for_lon(40.70) == 37
    assert utm_zone_for_lon(43.20) == 38


def test_tiff_roundtrip():
    """The reader and the writer agree, including CRS, nodata and windows."""
    tmp = os.path.join(ROOT, "runs", "_test.tif")
    os.makedirs(os.path.dirname(tmp), exist_ok=True)
    grid = RasterGrid(43.17, 54.89, 1 / 1125, -1 / 1125, 73, 46, 4326)
    rng = np.random.default_rng(7)
    data = np.stack([
        (rng.random((46, 73)) * 300).astype(np.uint16),
        (rng.random((46, 73)) * 90).astype(np.uint16),
    ])
    write_geotiff(tmp, data, grid, epsg=4326, nodata=None, compress=True)
    with GeoTiff(tmp) as t:
        assert t.info.bands == 2 and t.info.width == 73 and t.info.height == 46
        assert t.info.epsg == 4326
        assert abs(t.info.grid.dx - 1 / 1125) < 1e-15
        assert np.array_equal(t.read(band=1), data[0])
        win = t.read(band=2, window=(5, 9, 11, 13))
        assert np.array_equal(win, data[1][5:16, 9:22])
    os.remove(tmp)


def test_polygon_clipping_is_exact():
    """Clipping a rectangle by a rectangle gives the analytic answer."""
    poly = [[(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0)]]
    clipped = clip_polygon_to_rect(poly, 1.0, 1.0, 3.0, 3.0)
    assert clipped
    from canopy.geo.polygon import signed_area
    assert abs(abs(signed_area(clipped[0])) - 1.0) < 1e-12
    assert clip_polygon_to_rect(poly, 5.0, 5.0, 6.0, 6.0) == []


def _source_lines(path):
    """Executable lines of a module: docstrings and comments removed.

    A docstring may legitimately name an area -- explaining a method on the
    example it was tuned against is documentation, not a special case in the
    code.  Only what actually runs is checked.
    """
    import ast

    text = open(path, encoding="utf-8").read()
    tree = ast.parse(text)
    doc_lines = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            if isinstance(first.value.value, str):
                doc_lines.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    for i, line in enumerate(text.splitlines(), 1):
        if i in doc_lines:
            continue
        yield i, line.split("#")[0]


AOI_NEEDLES = ("RU_TVER", "RU_VOLOGDA", "RU_MORDOVIA", "CHECK_TRANSFER")
BRANCH_MARKS = ("==", "!=", "if ", "elif ", "startswith(", "in (", "in [", "in {", "match ")

#: the two modules a user talks to; they may name an area as a *saved request*
ENTRY_MODULES = ("server.py", "cli.py")


def test_no_hardcoded_identifiers():
    """No module may branch on an area id, and none may carry a developer path.

    This is the test behind the rule `if aoi == "RU_TVER_01"` is forbidden.  It
    is stated in two parts, because the two halves of the repository live under
    different rules.  Inside the analysis packages an area id may not appear in
    running code at all: the pipeline receives a geometry and a period and has
    no idea which area, if any, it belongs to.  In `server.py` and `cli.py` an id
    may appear as the *input* of a saved demo request -- the geometry itself is
    read from `areas.geojson` at run time -- but never in a comparison, because
    a comparison is the point at which behaviour would start to depend on the
    name rather than on the data.
    """
    bad = []
    src = os.path.join(ROOT, "src")
    for dirpath, _dirs, files in os.walk(src):
        for fn in sorted(files):
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dirpath, fn)
            rel = os.path.relpath(path, ROOT)
            entry = fn in ENTRY_MODULES
            for i, code in _source_lines(path):
                for needle in ("/home/", "/Users/", "C:\\"):
                    if needle in code:
                        bad.append(f"{rel}:{i}: developer path {needle!r}")
                hit = next((n for n in AOI_NEEDLES if n in code), None)
                if not hit:
                    continue
                if not entry:
                    bad.append(f"{rel}:{i}: area id {hit!r} in the analysis pipeline")
                elif any(m in code for m in BRANCH_MARKS):
                    bad.append(f"{rel}:{i}: behaviour branches on area id {hit!r}")
    assert not bad, "hardcoded identifiers or developer paths:\n" + "\n".join(bad)


def test_demo_scenarios_are_requests_not_answers():
    """The demo button fills a form; it does not carry a result.

    A saved scenario is allowed to be convenient.  It is not allowed to be a
    short cut around the calculation, so it may contain only what a user could
    have typed: geometry and a period.  Any carbon, unit or uncertainty figure
    living in that structure would mean the demo shows a number the pipeline
    never produced.
    """
    from canopy.server import demo_scenarios

    forbidden = ("carbon", "co2", "tonnes", "units", "quantity", "uncertainty",
                 "result", "delta", "stock", "emission", "e_proj", "q")
    for scenario in demo_scenarios():
        assert scenario.get("geometry"), f"{scenario['id']}: no geometry to analyse"
        assert isinstance(scenario.get("year_start"), int)
        assert isinstance(scenario.get("year_end"), int)
        for key, value in scenario.items():
            if key.lower() in forbidden:
                raise AssertionError(f"{scenario['id']}: carries a saved result in {key!r}")
            assert not isinstance(value, (int, float)) or key in ("year_start", "year_end"), (
                f"{scenario['id']}: unexplained number in {key!r} = {value!r}"
            )


def test_catalog_matches_contours_by_bounds_not_by_name():
    """A contour is matched to data geometrically, so a new polygon just works."""
    catalog = get_catalog()
    inside = (43.18, 54.86, 43.20, 54.88)
    outside = (37.60, 55.70, 37.62, 55.72)
    assert catalog.query(CCI_AGB, bbox=inside), "no biomass found inside the coverage"
    assert not catalog.query(CCI_AGB, bbox=outside), "biomass found where there is none"
