"""Stage-1 contracts: what the app PROMISES the user, asserted (S0-10, S0-12).

Every test here states a contract the UI already implies. The ones marked
`xfail(strict=True)` are contracts the code currently breaks — each cites the plan item
that fixes it. `strict` matters: when a fix lands, the test XPASSes, which FAILS the
run until the marker is removed. So the backlog cannot silently drift out of date, and
`pytest -rx` prints the remaining work.

Findings come from executed probes, not review: a solver fuzz over 648,000 legal
requirement sets (verification/fuzz_inverse_solver.py, seed 20260725) and an API sweep
over every Stage-1 endpoint.

Pure `domain_packs` — no WSL, no OpenVSP, no server.
"""
import math

import pytest
from hypothesis import HealthCheck, given, settings

from domain_packs.bwb import intake as I
from domain_packs.bwb.aero_estimates import LAUNCH_VSTALL_MAX
from domain_packs.bwb.inverse import solve_requirements
from domain_packs.bwb.requirements import DesignRequirements
from domain_packs.bwb.schema import BWBParams
from strategies import bwb_params, design_requirements

BRIEF = ("drone for supervising a farm, fly at 100 m, span under 2 m, "
         "carry my 180 g camera, 45 minutes, hand launched")


def _row(ik: I.Intake, rid: str) -> I.IntakeRow:
    row = next((r for r in ik.rows if r.id == rid), None)
    assert row is not None, f"missing row {rid}"
    return row


def _derived_numbers(ik: I.Intake) -> tuple:
    return tuple((r.id, r.number) for r in ik.rows if r.tier == "derived")


# ===================================================================================
# Solver contracts — properties, not examples (these PASS; keep them passing)
# ===================================================================================

@settings(max_examples=150, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
@given(req=design_requirements())
def test_solve_requirements_never_raises(req: DesignRequirements):
    """inverse.py's documented contract: infeasibility is never an error.

    Verified over 648,000 grid cases with zero exceptions; this keeps it true for
    inputs the grid never tried.
    """
    res = solve_requirements(req)
    BWBParams.model_validate(res.params.model_dump())


@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
@given(req=design_requirements())
def test_solved_geometry_stays_inside_every_schema_gate(req: DesignRequirements):
    res = solve_requirements(req)
    p = res.params
    assert 0.8 - 1e-9 <= p.span_m <= 3.0 + 1e-9
    assert 0.25 - 1e-9 <= p.root_chord_m <= 1.2 + 1e-9
    assert 2.5 - 1e-6 <= p.aspect_ratio() <= 10.0 + 1e-6
    assert p.chords()[2] >= 0.04 - 1e-9
    area = p.planform_area_m2()
    assert math.isfinite(area) and area > 0
    assert area == pytest.approx(res.panel.s_ref_m2, rel=1e-9)


@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
@given(req=design_requirements())
def test_user_span_cap_is_always_honoured(req: DesignRequirements):
    """Unlike the stall cap, the span cap genuinely holds — 0 violations in 648,000
    cases. Lock it so the Stage-4 stall fix cannot regress it."""
    res = solve_requirements(req)
    if req.span_max_m is not None:
        assert res.params.span_m <= req.span_max_m + 1e-6


@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
@given(req=design_requirements())
def test_binding_constraint_is_set_exactly_when_a_target_is_missed(req):
    res = solve_requirements(req)
    unmet = [s for s in res.requirements if s.met is False]
    if unmet:
        assert res.binding_constraint is not None
        assert res.binding_explanation
    else:
        assert res.binding_constraint is None


@settings(max_examples=60, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
@given(req=design_requirements())
def test_solve_requirements_is_deterministic(req: DesignRequirements):
    assert (solve_requirements(req).params.model_dump()
            == solve_requirements(req).params.model_dump())


@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
@given(params=bwb_params())
def test_panel_is_finite_for_any_legal_geometry(params: BWBParams):
    from domain_packs.bwb.aero_estimates import estimate
    from domain_packs.bwb.atmosphere import atmosphere_at
    panel = estimate(params, atmosphere_at(100.0), 18.0, 2.0 * 9.81, "hand")
    for field, value in panel.model_dump().items():
        if isinstance(value, float):
            assert math.isfinite(value), f"{field} not finite"


# ===================================================================================
# Known-broken contracts — each flips when its plan item lands
# ===================================================================================

@pytest.mark.xfail(strict=True,
                   reason="S6-J4: re-solving from the solver's own output is not a "
                          "fixed point in ~0.7% of cases, so 'morph the existing "
                          "design' can drift under repeated edits — which is exactly "
                          "how Stage 1 uses it (every row edit re-solves).")
@settings(max_examples=400, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
@given(req=design_requirements())
def test_solve_requirements_is_idempotent(req: DesignRequirements):
    first = solve_requirements(req)
    second = solve_requirements(req, shape_from=first.params)
    third = solve_requirements(req, shape_from=second.params)
    assert second.params.span_m == pytest.approx(third.params.span_m, abs=1e-6)
    assert second.params.root_chord_m == pytest.approx(
        third.params.root_chord_m, abs=1e-6)


@pytest.mark.xfail(strict=True,
                   reason="S6-J2: when the user sets no stall row, _statuses emits "
                          "met=None, so a launch-cap breach never sets a binding "
                          "constraint. Measured: a 20 kg / 0.9 m-span design needs a "
                          "29.6 m/s hand throw and is reported as 'all requirements "
                          "met'. Only panel.launch_ok knows, and no surface reads it.")
def test_launch_cap_breach_is_reported():
    req = DesignRequirements(mtow_kg=20.0, cruise_speed_ms=18.0,
                             cruise_altitude_m=100.0, launch="hand",
                             span_max_m=0.9)
    res = solve_requirements(req)
    cap = LAUNCH_VSTALL_MAX[req.launch]
    assert res.panel.v_stall_ms > cap, "premise: this design breaches the launch cap"
    assert res.binding_constraint is not None, (
        f"stall {res.panel.v_stall_ms:.1f} m/s vs {cap} cap, yet the verdict is "
        f"{res.binding_explanation!r}")


# S2-D1 (sign, units, thousands separators, exponents, magnitudes), S2-D4 (brief
# anchoring, ambiguity, launch precedence) and S2-D5 (parse merges rather than resets)
# LANDED in Stage 2. Their markers are retired and the contracts now live, un-xfailed
# and considerably wider, in tests/test_bwb_intake_parsing.py.
#
# One of the retired assertions was itself wrong and is worth recording: it demanded
# that "1,500 g" NOT parse to 1.5, on the grounds that 1,500 -> 1.5 was the
# thousands-separator bug. But 1,500 g IS 1.5 kg, so the buggy path and the correct one
# happened to agree on that input; the discriminating case is "1,500" with no unit,
# which must be 1500, not 1.5. The replacement test uses that.


@pytest.mark.xfail(strict=True,
                   reason="S3-D2: to_requirements clamps into schema range and reports "
                          "nothing. The row keeps the typed value and the verdict still "
                          "says 'feasible', so the user never learns their number was "
                          "replaced.")
@pytest.mark.parametrize("rid,typed", [
    ("span_max", "100 m"),      # used as 3.0
    ("mtow_cap", "500 kg"),     # used as 25
    ("altitude", "99999 m"),    # used as 4000
])
def test_clamped_values_are_disclosed(rid, typed):
    ik = I.derive(I.apply_edits(I.default_intake(), [{"id": rid, "value": typed}]))
    used = getattr(I.to_requirements(ik), {
        "span_max": "span_max_m", "mtow_cap": "mtow_kg",
        "altitude": "cruise_altitude_m"}[rid])
    typed_number = _row(ik, rid).number
    assert typed_number is not None
    if typed_number != pytest.approx(used):
        # the value WAS rewritten — the intake must say so somewhere the UI can show
        disclosed = (getattr(ik.feasibility, "warnings", None)
                     or getattr(_row(ik, rid), "warning", None)
                     or [n for n in ik.notes if rid.split("_")[0] in n.lower()])
        assert disclosed, (
            f"{rid}: user typed {typed!r}, solver used {used!r}, nothing disclosed")


@pytest.mark.xfail(strict=True,
                   reason="S3-E2/E3: six of twelve editable rows drive zero "
                          "computation. They are presented identically to rows that "
                          "do, with no 'recorded only' marking.")
@pytest.mark.parametrize("rid,value", [
    ("mission_type", "Racing / speed"),
    ("structure", "carbon fibre"),
    ("wind", "20 m/s gusts"),
])
def test_editable_rows_either_affect_the_design_or_say_they_do_not(rid, value):
    base = I.derive(I.default_intake())
    edited = I.derive(I.apply_edits(I.default_intake(), [{"id": rid, "value": value}]))
    if _derived_numbers(base) == _derived_numbers(edited):
        row = _row(edited, rid)
        assert getattr(row, "recorded_only", False) is True, (
            f"{rid} changes nothing but is not marked recorded-only")


def test_reparsing_a_brief_preserves_user_edits():
    """S2-D5, landed: the merge path is what the parse endpoint uses now.

    `parse_brief` itself still returns a fresh list — that is its job, and the
    deliberate start-over case (`?replace=true`) needs it. What changed is that no
    endpoint calls it directly on a session that already has an intake."""
    ik = I.derive(I.parse_brief(BRIEF))
    ik = I.derive(I.apply_edits(ik, [
        {"id": "mtow_cap", "value": "4.5 kg", "status": "fixed"},
        {"id": "custom_max_chord", "label": "Max chord", "value": "0.35 m"},
    ]))
    reparsed = I.derive(I.merge_brief(ik, BRIEF))
    assert _row(reparsed, "mtow_cap").number == pytest.approx(4.5)
    assert any(r.id == "custom_max_chord" for r in reparsed.rows)
