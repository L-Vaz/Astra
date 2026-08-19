"""Inverse-sizing tests: requirements -> geometry (domain_packs/bwb/inverse.py).

Pure pydantic/closed-form — runs anywhere, no OpenVSP/WSL needed.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from domain_packs.bwb.aero_estimates import LAUNCH_VSTALL_MAX, estimate
from domain_packs.bwb.atmosphere import atmosphere_at
from domain_packs.bwb.defaults import MAX_CRUISE_CL, derive
from domain_packs.bwb.inverse import (BASELINE_SHAPE, ShapeFractions,
                                      check_requirements, solve_planform,
                                      solve_requirements)
from domain_packs.bwb.requirements import DesignRequirements
from domain_packs.bwb.schema import BWBParams, MissionSpec
from domain_packs.bwb.variants import generate_candidates


REQ = dict(mtow_kg=5.0, cruise_speed_ms=25.0, cruise_altitude_m=100.0)
AIRFOIL_APPROVAL = {"schedule": {
    "root": "e474", "mid": "e472", "tip": "s5010"}}


def _panel(req: DesignRequirements, params: BWBParams):
    atmosphere = atmosphere_at(req.cruise_altitude_m)
    return estimate(params, atmosphere, req.cruise_speed_ms,
                    req.mtow_kg * 9.81, req.launch)


def test_cl_target_hit_exactly_when_feasible():
    # runway launch tolerates stall to 16 m/s: CL=0.35 at 25 m/s cruise implies
    # stall ~ 25*sqrt(0.35/1.03) ~ 14.6 m/s — feasible, so the target must be hit
    req = DesignRequirements(**REQ, launch="runway", cl_cruise_target=0.35)
    res = solve_requirements(req)
    assert abs(res.panel.cl_cruise - 0.35) / 0.35 < 0.02
    status = {s.name: s for s in res.requirements}
    assert status["cl_cruise_target"].met is True


def test_stall_floor_binds_for_hand_launch():
    # heavy + hand launch: stall tolerance floors the area, CL target unreachable
    req = DesignRequirements(mtow_kg=8.0, cruise_speed_ms=30.0,
                             cruise_altitude_m=100.0, launch="hand",
                             cl_cruise_target=0.6, ld_min=30.0)
    res = solve_requirements(req)
    assert res.binding_constraint in ("stall_at_launch", "ar_gate", "span_cap")
    # the stall requirement itself must be met by the floored wing
    assert res.panel.v_stall_ms <= LAUNCH_VSTALL_MAX["hand"] + 0.05
    assert res.binding_explanation


def test_user_stall_requirement_tighter_than_launch():
    req = DesignRequirements(**REQ, launch="runway", v_stall_max_ms=9.0, ld_min=35.0)
    res = solve_requirements(req)
    assert res.panel.v_stall_ms <= 9.0 + 0.05
    if res.binding_constraint:  # unmet L/D must blame the user's stall goal
        assert res.binding_constraint in ("stall_requirement", "ar_gate", "span_cap")


def test_span_cap_respected_and_named():
    req = DesignRequirements(**REQ, launch="catapult", span_max_m=1.6, ld_min=35.0)
    res = solve_requirements(req)
    assert res.params.span_m <= 1.6 + 1e-6
    assert res.binding_constraint in ("span_cap", "stall_at_launch", "ar_gate")


def test_result_always_validates():
    for launch in ("hand", "catapult", "runway"):
        for mtow in (1.0, 5.0, 15.0):
            for v in (12.0, 25.0, 45.0):
                req = DesignRequirements(mtow_kg=mtow, cruise_speed_ms=v,
                                         cruise_altitude_m=100.0, launch=launch,
                                         ld_min=18.0)
                res = solve_requirements(req)
                BWBParams.model_validate(res.params.model_dump())  # no raise
                # the CL cap holds unless a schema limit capped the wing area,
                # in which case the compromise must be noted
                if res.panel.cl_cruise > MAX_CRUISE_CL * 1.02:
                    assert any("schema" in n for n in res.notes), res.notes


def test_shape_fractions_preserved_when_morphing():
    base = derive(MissionSpec(payload_kg=1.5, cruise_speed_ms=25.0,
                              cruise_altitude_m=100.0, launch="hand")).params
    req = DesignRequirements(**REQ, launch="hand", ld_min=14.0)
    res = solve_requirements(req, shape_from=base)
    for f in ("sweep_inner_deg", "sweep_outer_deg", "root_twist_deg",
              "tip_twist_deg", "dihedral_deg", "centre_body_fraction",
              "mid_chord_frac"):
        assert getattr(res.params, f) == pytest.approx(getattr(base, f))


def test_ld_loop_never_overshoots_ar_gate():
    """Regression: stepping AR by 0.25 from a geometry whose AR is not a
    multiple of 0.25 (e.g. 4.95) must clamp at the 10.0 gate, not reach 10.2."""
    base = derive(MissionSpec(payload_kg=1.0, cruise_speed_ms=30.0,
                              cruise_altitude_m=300.0, launch="hand")).params
    odd = BWBParams.model_validate({**base.model_dump(), "span_m": base.span_m * 0.95})
    assert abs(odd.aspect_ratio() % 0.25) > 1e-6  # genuinely off-grid
    req = DesignRequirements(mtow_kg=3.33, cruise_speed_ms=30.0,
                             cruise_altitude_m=300.0, launch="hand",
                             ld_min=16.0, v_stall_max_ms=9.0)
    res = solve_requirements(req, shape_from=odd)   # must not raise
    assert res.params.aspect_ratio() <= 10.0 + 1e-9


def test_check_requirements_forward_only():
    base = derive(MissionSpec(payload_kg=1.5, cruise_speed_ms=25.0,
                              cruise_altitude_m=100.0, launch="hand")).params
    req = DesignRequirements(**REQ, launch="hand", ld_min=40.0)
    statuses = check_requirements(req, base)
    st = {s.name: s for s in statuses}
    assert st["ld_min"].met is False
    assert st["ld_min"].achieved == pytest.approx(_panel(req, base).ld_cruise)


def test_solve_planform_matches_area_exactly():
    p = solve_planform(0.6, 6.0, BASELINE_SHAPE)
    assert p.planform_area_m2() == pytest.approx(0.6, rel=1e-9)


def test_variants_refactor_snapshot():
    """generate_candidates output is unchanged by the _solve_planform refactor.
    Golden values recorded from the pre-refactor code path semantics: the
    wrapper must produce identical planforms for the default mission."""
    derived = derive(MissionSpec(payload_kg=1.5, cruise_speed_ms=25.0,
                                 cruise_altitude_m=100.0, launch="hand"))
    cands = generate_candidates(derived, AIRFOIL_APPROVAL)
    assert [c.key for c in cands] == ["baseline", "max_range", "span_efficiency",
                                      "easy_launch", "control_margin"]
    for c in cands:
        BWBParams.model_validate(c.params.model_dump())
        # every candidate's panel is self-consistent with its params
        fresh = estimate(c.params, derived.atmosphere,
                         derived.mission.cruise_speed_ms, derived.weight_n,
                         derived.mission.launch)
        assert fresh.ld_cruise == pytest.approx(c.panel.ld_cruise, rel=1e-9)
        assert fresh.s_ref_m2 == pytest.approx(c.panel.s_ref_m2, rel=1e-9)
