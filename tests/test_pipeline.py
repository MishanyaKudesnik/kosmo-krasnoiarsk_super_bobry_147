"""End-to-end behaviour of the analysis pipeline.

`test_core.py` pins the arithmetic.  This module pins the promises a user can
check from the outside: that an arbitrary contour is analysed by the same code
path as a prepared one, that a contour reaching outside the data says so instead
of inventing the missing part, that a contour entirely outside the data returns
a complete, well-formed refusal rather than an error page, and that running the
same request twice gives the same numbers.

The scenarios are named after what a juror would try: draw something new, draw
something half outside, draw something far away, change the period.
"""

from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.join(ROOT, "src") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "src"))

from canopy.core.status import (
    NO_BIOMASS_DATA,
    OK,
    PARTIAL_COVERAGE,
    UNITS_UNAVAILABLE_PARTIAL_COVERAGE,
)
from canopy.engine import AnalysisRequest, run_analysis
from canopy.dal.discovery import AVAILABLE, PARTIAL, UNAVAILABLE

# A pentagon nobody prepared: inside the Mordovia coverage, but not one of the
# supplied areas and not a rectangle.
ARBITRARY_INSIDE = {
    "type": "Polygon",
    "coordinates": [[
        [43.182, 54.858], [43.213, 54.861], [43.219, 54.879],
        [43.196, 54.888], [43.179, 54.874], [43.182, 54.858],
    ]],
}

# The same neighbourhood, dragged west across the edge of the biomass tile
# (43.1698 E), so that roughly half of it has no data underneath.
HALF_OUTSIDE = {
    "type": "Polygon",
    "coordinates": [[
        [43.150, 54.858], [43.190, 54.858], [43.190, 54.888],
        [43.150, 54.888], [43.150, 54.858],
    ]],
}

# Central Moscow: no product of this dataset reaches it.
FAR_OUTSIDE = {
    "type": "Polygon",
    "coordinates": [[
        [37.60, 55.70], [37.64, 55.70], [37.64, 55.73], [37.60, 55.73], [37.60, 55.70],
    ]],
}


def _run(geometry, year_start=2019, year_end=2024):
    return run_analysis(
        AnalysisRequest.parse(
            {"geometry": geometry, "year_start": year_start, "year_end": year_end}
        )
    )


def _product(result, name):
    for p in result.readiness.get("products", []):
        if p["product"] == name:
            return p
    raise AssertionError(f"{name} missing from readiness")


def _q(node):
    """A Quantity is a value with a status; unwrap the number or None."""
    return node.get("value") if isinstance(node, dict) else node


def test_arbitrary_polygon_runs_the_whole_chain():
    """A contour drawn on the spot produces every section, with real numbers."""
    r = _run(ARBITRARY_INSIDE)
    assert r.status.code == OK, r.status.as_dict()

    area = r.geometry_info["area_ha"]
    assert 100 < area < 200000, area
    assert r.geometry_info["geodesic_method"], "the area must say how it was measured"

    carbon = r.carbon
    assert carbon["cf"] == 0.47
    assert abs(carbon["co2_per_c"] - 44.0 / 12.0) < 1e-12
    assert carbon["pool"], "the pool must be named in the result, not only in the docs"
    start, end = carbon["state_start"], carbon["state_end"]
    assert start["total_tc"] > 0 and end["total_tc"] > 0
    assert start["valid_pixels"] > 0, "a stock computed from no pixels"
    assert _q(carbon["E"]) is not None
    assert abs(carbon["area_valid_ha"] - area) < area * 1e-6

    # the stock is the carbon fraction of the biomass, over the measured area
    for state in (start, end):
        expected = state["agb_mean_t_ha"] * 0.47
        assert abs(state["mean_tc_ha"] - expected) < expected * 1e-6, state

    # E must be the stock difference times 44/12, not an independent number
    delta_c = _q(carbon["delta_c_tc"])
    assert abs(_q(carbon["E"]) - (-delta_c * 44.0 / 12.0)) < 1e-6

    assert r.uncertainty["interval_kind"] == "scenario"
    assert r.uncertainty["scenarios"], "an interval without its scenarios is a claim"
    assert r.units["disclaimer"], "units must carry the wording of the case"
    assert r.ledger, "no evidence chain"
    assert r.manifest["inputs"], "no reproduction manifest"
    for entry in r.manifest["inputs"]:
        assert entry.get("sha256"), f"input without a checksum: {entry}"


def test_partial_coverage_is_reported_not_filled_in():
    """Half outside the data: the covered part is computed, the gap is stated."""
    r = _run(HALF_OUTSIDE)
    cci = _product(r, "CCI_AGB")
    assert cci["state"] == PARTIAL, cci
    assert 0.1 < cci["coverage_fraction"] < 0.95, cci["coverage_fraction"]

    carbon = r.carbon
    assert carbon["area_gap_ha"] > 0, "a gap of zero would mean the hole was filled"
    total = carbon["area_valid_ha"] + carbon["area_gap_ha"]
    assert abs(total - carbon["area_requested_ha"]) < total * 1e-6, (
        "calculated area and gap must add up to what was asked for"
    )

    # units are refused, and the refusal names the rule it follows
    assert r.units["status"]["code"] == UNITS_UNAVAILABLE_PARTIAL_COVERAGE, r.units["status"]
    assert _q(r.units["Q"]) is None, "units issued on partial coverage"
    assert any(w["code"] == PARTIAL_COVERAGE for w in r.warnings), r.warnings


def test_polygon_outside_all_data_refuses_completely_and_cleanly():
    """Far from the data: a full, readable refusal -- every section, no crash."""
    r = _run(FAR_OUTSIDE)
    assert r.status.code == NO_BIOMASS_DATA, r.status.as_dict()
    assert r.status.message, "a refusal without a reason is a bug"

    assert _product(r, "CCI_AGB")["state"] == UNAVAILABLE
    assert r.readiness["blocked"], "nothing was marked as refused"

    # every section the front end renders must exist and be well formed
    for name in ("carbon", "change", "uncertainty", "baseline", "units", "evidence"):
        section = getattr(r, name)
        assert isinstance(section, dict) and section, f"{name} is empty"
    assert _q(r.carbon["E"]) is None
    assert _q(r.units["Q"]) is None
    assert r.carbon["status"]["code"] == NO_BIOMASS_DATA

    # and the payload must survive the trip to the browser
    json.dumps(r.as_dict(), ensure_ascii=False)


def test_shorter_period_changes_the_answer_consistently():
    """A different period is a different question, answered by the same code."""
    long_run = _run(ARBITRARY_INSIDE, 2019, 2024)
    short_run = _run(ARBITRARY_INSIDE, 2020, 2021)
    assert short_run.status.code == OK

    assert short_run.carbon["delta_years"] == 1
    assert long_run.carbon["delta_years"] == 5
    assert abs(short_run.geometry_info["area_ha"] - long_run.geometry_info["area_ha"]) < 1e-9

    # the intensity is per hectare per year, so it must not simply scale with the period
    e_long, e_short = _q(long_run.carbon["E"]), _q(short_run.carbon["E"])
    assert e_long is not None and e_short is not None
    per_ha_year = _q(short_run.carbon["e_per_ha_year"])
    area = short_run.carbon["area_valid_ha"]
    assert abs(per_ha_year - e_short / (area * 1)) < 1e-6


def test_same_request_twice_gives_the_same_numbers():
    """Determinism, checked on the numbers a juror would read off the screen."""
    a = _run(ARBITRARY_INSIDE)
    b = _run(ARBITRARY_INSIDE)
    fields = [
        ("geometry_info", "area_ha"),
        ("carbon", "area_valid_ha"),
        ("carbon", "delta_c_tc"),
        ("carbon", "E"),
        ("carbon", "e_per_ha_year"),
        ("units", "R"),
        ("units", "H"),
        ("units", "UNC"),
        ("units", "R_adj"),
        ("units", "Q"),
        ("uncertainty", "sigma_e"),
    ]
    for section, key in fields:
        va, vb = _q(getattr(a, section)[key]), _q(getattr(b, section)[key])
        assert va == vb, f"{section}.{key}: {va} != {vb}"
    assert a.provenance == b.provenance, "provenance drifted between identical runs"


def test_every_number_on_screen_can_be_traced():
    """Each headline figure has a ledger entry naming its formula and inputs."""
    r = _run(ARBITRARY_INSIDE)
    nodes = {e.get("node_id"): e for e in r.ledger}
    for needed in ("A", "C_t0", "C_t1", "dC", "E_proj", "sigma_E", "E_base", "R", "Q"):
        assert needed in nodes, f"{needed} has no ledger entry (have: {sorted(nodes)})"

    for entry in r.ledger:
        if entry.get("kind") == "value":
            assert entry.get("formula"), f"{entry['node_id']}: a value without a formula"
            assert entry.get("substituted") or entry.get("inputs"), (
                f"{entry['node_id']}: a formula nobody can check -- no substitution"
            )
        if entry.get("kind") == "raster_read":
            assert entry.get("sha256"), f"{entry['node_id']}: a file read without a checksum"
            assert entry.get("source_id") and entry.get("product_version"), entry

    # the trace has to reach the files: every value must lead back to a read
    assert any(e.get("kind") == "raster_read" for e in r.ledger)
    # and the front end must be able to print a one-line provenance for them
    assert set(r.provenance) >= {"A", "C_t0", "dC", "E_proj"}, sorted(r.provenance)


def test_cause_is_never_asserted_without_a_source():
    """Where nothing confirms a cause, the verdict says so in as many words."""
    r = _run(ARBITRARY_INSIDE)
    for patch in r.change.get("patches", []):
        rung = patch.get("attribution", {}).get("rung")
        cause = patch.get("attribution", {}).get("cause")
        if rung in ("C", "D"):
            assert not cause or "не установлен" in str(cause).lower(), (
                f"patch {patch.get('id')}: cause {cause!r} claimed at rung {rung}"
            )


def test_years_shown_to_the_user_are_calendar_years():
    """Every year printed in the evidence must be a year that could exist.

    Hansen encodes the year of loss as an offset from 2000, MODIS as a day of
    the year, Sentinel-2 as a full timestamp.  Each is decoded in its own place,
    and a decoding applied twice reads plausibly right up to the moment someone
    looks at it -- the screen said "loss recorded in 4021".  Anything a user can
    read is checked here against the calendar.
    """
    import re

    r = _run(ARBITRARY_INSIDE)
    fields = []
    for patch in r.change.get("patches", []):
        for ev in patch.get("evidence", []):
            fields += [str(ev.get(k, "")) for k in ("human", "detail", "claim_note")]
        for w in patch.get("windows", []):
            fields += [str(w.get("note", "")), str(w.get("start", "")), str(w.get("end", ""))]
    fields += [str(s.get("date", "")) for s in r.change.get("scenes", [])]

    for text in fields:
        # A pixel count ("9840 пикс.") is a number, not a year.
        for token in re.findall(r"\b\d{4}\b(?!\s*пикс)", text):
            year = int(token)
            assert 1972 <= year <= 2100, f"impossible year {year} in: {text[:160]}"


def test_fixture_data_is_labelled_everywhere_it_is_used():
    """Synthetic rasters must announce themselves; silence here would be a lie."""
    r = _run(ARBITRARY_INSIDE)
    if not r.readiness.get("uses_fixtures"):
        return  # running against the real dataset -- nothing to label
    notice = r.readiness.get("fixture_notice")
    assert notice and notice.get("kind"), "fixtures in use without a notice"
    # the jury reads Russian; an English-only warning is not a warning to them
    assert "синтет" in str(notice.get("warning_ru", "")).lower(), notice
    assert "SYNTHETIC" in notice["kind"].upper(), notice["kind"]

    # and every raster entry in the ledger says which of the two sources it came from
    origins = {e.get("origin") for e in r.ledger if e.get("kind") == "raster_read"}
    assert origins and None not in origins, origins
