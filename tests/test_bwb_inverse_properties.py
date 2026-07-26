"""Solver contracts as properties (S6-J1..J4).

A deterministic 648,000-combination sweep of the legal DesignRequirements envelope
(seed 20260725) established what `inverse.solve_requirements` does and does not
guarantee. These tests pin the guarantees that held and encode the one that did not.

Held with zero violations: never raises, every returned params re-validates, span and
AR inside their gates, no NaN in any panel field, determinism.

Did NOT hold: the effective stall cap, breached in 33.1% of the envelope (95.9% at
25 kg). Since Stage 4 that is either satisfied or reported; since S6-J2 the solver's
own verdict names it instead of claiming "all requirements met".

Pure `domain_packs`.
"""
import pytest
from hypothesis import HealthCheck, given, settings

from domain_packs.bwb.aero_estimates import LAUNCH_VSTALL_MAX
from domain_packs.bwb.inverse import effective_stall_cap, solve_requirements
from domain_packs.bwb.requirements import DesignRequirements
from domain_packs.bwb.schema import BWBParams
from strategies import design_requirements

_SETTINGS = settings(max_examples=300, deadline=None,
                     suppress_health_check=[HealthCheck.too_slow,
                                            HealthCheck.filter_too_much])


@_SETTINGS
@given(req=design_requirements())
def test_solve_requirements_never_raises(req):
    """The documented contract: infeasibility is a reported binding constraint, never
    an exception. 0 failures in 648,000 probed solves — keep it that way."""
    result = solve_requirements(req)
    BWBParams.model_validate(result.params.model_dump())
    assert 0.8 - 1e-9 <= result.params.span_m <= 3.0 + 1e-9
    assert 2.5 - 1e-6 <= result.params.aspect_ratio() <= 10.0 + 1e-6


@_SETTINGS
@given(req=design_requirements())
def test_solve_requirements_is_deterministic(req):
    assert (solve_requirements(req).params.model_dump()
            == solve_requirements(req).params.model_dump())


@_SETTINGS
@given(req=design_requirements())
def test_binding_constraint_is_set_whenever_a_target_is_missed(req):
    result = solve_requirements(req)
    unmet = [s for s in result.requirements if s.met is False]
    if unmet:
        assert result.binding_constraint is not None, [s.name for s in unmet]
        assert result.binding_explanation
        assert "all requirements met" not in result.binding_explanation
    else:
        assert result.binding_constraint is None


@_SETTINGS
@given(req=design_requirements())
def test_the_effective_stall_cap_is_always_evaluated(req):
    """S6-J2: the status used to carry target=None (hence met=None, hence never unmet)
    whenever the user set no stall row — which is 24.6% of the envelope."""
    result = solve_requirements(req)
    status = next(s for s in result.requirements if s.name == "v_stall_max_ms")
    cap, _ = effective_stall_cap(req)
    assert status.target == pytest.approx(cap)
    assert status.met is not None
    assert status.met == (result.panel.v_stall_ms <= cap + 0.05)


@_SETTINGS
@given(req=design_requirements())
def test_a_launch_cap_breach_is_never_silent(req):
    result = solve_requirements(req)
    if result.panel.launch_ok is False:
        assert result.binding_constraint is not None
        assert "all requirements met" not in result.binding_explanation


# Idempotence (S6-J4) is encoded once, as an xfail, in
# tests/test_stage1_contracts.py::test_solve_requirements_is_idempotent. Re-solving
# from the solver's own output drifts by ~2.5e-5 m in the root chord for a small tail
# of inputs; it is LOW severity and unfixed, so it stays in the backlog rather than
# being asserted twice.


def test_the_minimal_stall_reproducer_is_diagnosed_correctly():
    """The smallest overshoot the fuzz found: stall 5.24 against a 5.00 cap."""
    req = DesignRequirements(mtow_kg=0.5, cruise_speed_ms=8.0, cruise_altitude_m=0.0,
                             launch="hand", span_max_m=0.8, v_stall_max_ms=5.0)
    result = solve_requirements(req)
    assert result.panel.v_stall_ms > 5.0
    assert result.binding_constraint == "stall_requirement"


@pytest.mark.parametrize("launch", sorted(LAUNCH_VSTALL_MAX))
def test_the_effective_cap_is_the_tighter_of_the_two(launch):
    loose = DesignRequirements(mtow_kg=2.0, cruise_speed_ms=18.0,
                               cruise_altitude_m=100.0, launch=launch,
                               v_stall_max_ms=20.0)
    tight = loose.model_copy(update={"v_stall_max_ms": 6.0})
    assert effective_stall_cap(loose) == (LAUNCH_VSTALL_MAX[launch], f"{launch} launch")
    assert effective_stall_cap(tight) == (6.0, "your requirement")
