"""Washout-only trim solve (S1-D).

Every check here is a physical DIRECTION or an algebraic IDENTITY, not a pinned
number — the pinned numbers live in the aero goldens. The directions are the part a
wrong sign would break silently, and a pitching-moment sign error on a tailless
aircraft is the kind of mistake that reads perfectly plausibly on a card.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from domain_packs.bwb.aero_estimates import estimate, neutral_point_m
from domain_packs.bwb.atmosphere import atmosphere_at
from domain_packs.bwb.schema import BWBParams
from domain_packs.bwb.trim import (CM_TRIM_TOLERANCE, pitching_moment, solve_trim,
                                   tip_twist_range)

REYNOLDS = 4.09e5
CL_CRUISE = 0.3735


def _params(**over) -> BWBParams:
    base = dict(span_m=1.6, root_chord_m=0.55, centre_body_fraction=0.28,
                mid_chord_frac=0.6, taper_outer=0.4, sweep_inner_deg=35,
                sweep_outer_deg=18, root_airfoil="e474", mid_airfoil="e472",
                tip_airfoil="s5010", root_twist_deg=1.0, tip_twist_deg=-3.0,
                dihedral_deg=3.0)
    base.update(over)
    return BWBParams(**base)


def _cg(p: BWBParams, sm: float = 0.08) -> float:
    return neutral_point_m(p) - sm * p.mean_aero_chord_m()


def _trimmable() -> BWBParams:
    """A wing that DOES trim itself inside the schema's twist range.

    Found by sweeping the legal envelope: heavy sweep (45/20 deg) plus reflexed
    sections throughout (esa40 root, mh110 mid and tip). That is the textbook flying-
    wing recipe and it is reassuring that the solve rediscovers it — sweep supplies the
    arm, reflex supplies the nose-up camber moment. Measured: Cm +0.0035 as built,
    trimming at -2.25 deg of tip twist.

    Its existence is the point. A trim model that could only ever answer "no" would be
    indistinguishable from a sign error."""
    return BWBParams(span_m=1.6, root_chord_m=0.55, centre_body_fraction=0.30,
                     mid_chord_frac=0.4, taper_outer=0.3, sweep_inner_deg=45,
                     sweep_outer_deg=20, root_airfoil="esa40", mid_airfoil="mh110",
                     tip_airfoil="mh110", root_twist_deg=1.0, tip_twist_deg=-3.0,
                     dihedral_deg=3.0)


# --------------------------------------------------------------- sign conventions

def test_moving_the_cg_aft_raises_cm():
    """Nose-up is positive, and the lift sits aft of a forward CG.

    Sliding the CG toward the lift shortens the nose-down arm, so Cm must RISE. If
    this test ever fails, every trim verdict in Stage 1 has the wrong sign."""
    p = _params()
    base = _cg(p)
    forward, _, _ = pitching_moment(p, CL_CRUISE, base - 0.02, REYNOLDS)
    aft, _, _ = pitching_moment(p, CL_CRUISE, base + 0.02, REYNOLDS)
    assert aft > forward


def test_washout_pushes_the_nose_up_on_a_swept_wing():
    """Washout unloads the TIPS. On a swept wing the tips are aft, so removing their
    lift removes a nose-down moment — this is why washout is a trim control at all,
    and why it is also what makes an outboard stall pitch the aircraft up."""
    cg = _cg(_params())
    less, _, _ = pitching_moment(_params(tip_twist_deg=0.0), CL_CRUISE, cg, REYNOLDS)
    more, _, _ = pitching_moment(_params(tip_twist_deg=-6.0), CL_CRUISE, cg, REYNOLDS)
    assert more > less


def test_reflex_supplies_nose_up_camber_moment():
    """A reflexed section has positive Cm0 — that is the whole point of using one on a
    tailless aircraft. Measured: esa40 has cm0 = +0.0537 at Re 3e5 against s5010's
    -0.0058, and swapping it in flips the camber term from -0.0012 to +0.0079."""
    cg = _cg(_params())
    _, cambered, _ = pitching_moment(_params(), CL_CRUISE, cg, REYNOLDS)
    _, reflexed, _ = pitching_moment(
        _params(tip_airfoil="esa40"), CL_CRUISE, cg, REYNOLDS)
    assert reflexed > cambered
    assert cambered < 0.0 < reflexed


# ------------------------------------------------------------------- the algebra

def test_cm_is_affine_in_tip_twist():
    """The solve claims exactness on this. Three points must be collinear — if they
    are not, the single probe in `solve_trim` is silently wrong."""
    cg = _cg(_params())
    xs = (-1.0, -3.0, -5.0)
    ys = [pitching_moment(_params(tip_twist_deg=x), CL_CRUISE, cg, REYNOLDS)[0]
          for x in xs]
    slope_lo = (ys[1] - ys[0]) / (xs[1] - xs[0])
    slope_hi = (ys[2] - ys[1]) / (xs[2] - xs[1])
    assert slope_lo == pytest.approx(slope_hi, rel=1e-9)


def test_cm_is_linear_in_cg_position():
    """Same claim for the CG inversion."""
    p = _params()
    base = _cg(p)
    ys = [pitching_moment(p, CL_CRUISE, base + d, REYNOLDS)[0]
          for d in (-0.02, 0.0, 0.02)]
    assert (ys[1] - ys[0]) == pytest.approx(ys[2] - ys[1], rel=1e-9)


def test_the_solved_twist_actually_trims():
    """Round-trip: feed the answer back in and the residual must vanish.

    Uses the trimmable fixture, because a round-trip through BWBParams cannot
    construct an out-of-range twist."""
    p = _trimmable()
    cg = _cg(p)
    sol = solve_trim(p, CL_CRUISE, cg, REYNOLDS)
    assert sol.tip_twist_in_range, "fixture must be trimmable for this test to mean anything"
    trimmed = p.model_copy(update={"tip_twist_deg": sol.tip_twist_for_trim_deg})
    cm, _, _ = pitching_moment(trimmed, CL_CRUISE, cg, REYNOLDS)
    assert cm == pytest.approx(0.0, abs=1e-12)


def test_the_solved_cg_actually_trims():
    p = _params()
    sol = solve_trim(p, CL_CRUISE, _cg(p), REYNOLDS)
    assert sol.x_cg_for_trim_m is not None
    cm, _, _ = pitching_moment(p, CL_CRUISE, sol.x_cg_for_trim_m, REYNOLDS)
    assert cm == pytest.approx(0.0, abs=1e-12)


def test_the_two_contributions_sum_to_the_whole():
    p = _params()
    cm, camber, lift = pitching_moment(p, CL_CRUISE, _cg(p), REYNOLDS)
    assert cm == pytest.approx(camber + lift, rel=1e-12)


# ------------------------------------------------------------------- the verdict

def test_the_reference_wing_cannot_trim_itself_with_washout():
    """The finding this item exists to surface, and it was previously invisible.

    Measured at the CG that gives an 8% static margin: Cm = -0.0275 nose-down, and
    zeroing it would need -11.55 deg of tip twist against a schema floor of -7. The
    burden is almost entirely GEOMETRIC, not camber: the lift-times-arm term is
    -0.0263 against only -0.0012 from the sections.
    """
    p = _params()
    sol = solve_trim(p, CL_CRUISE, _cg(p), REYNOLDS)
    assert not sol.trims_as_built
    assert sol.cm_cg < 0
    assert not sol.tip_twist_in_range
    assert sol.tip_twist_for_trim_deg < tip_twist_range()[0]
    assert abs(sol.lift_contribution) > 10 * abs(sol.cm0_contribution)


def test_trimming_by_cg_costs_static_margin():
    """Measured: the CG that trims the reference wing leaves a 0.58% static margin
    against the 8% target. It can be balanced or it can be comfortably stable — not
    both. Reporting that is the point; it is a design decision, not a defect."""
    p = _params()
    sol = solve_trim(p, CL_CRUISE, _cg(p, 0.08), REYNOLDS)
    assert sol.static_margin_at_trim is not None
    assert sol.static_margin_at_trim < 0.08
    assert sol.x_cg_for_trim_m > _cg(p, 0.08)      # the CG must move AFT to trim


def test_trim_by_washout_costs_maximum_lift():
    """Unloading the tips to trim removes exactly the lift needed at low speed. When
    the trim twist is reachable, CLmax at it must be reported and must differ."""
    p = _trimmable()
    sol = solve_trim(p, CL_CRUISE, _cg(p), REYNOLDS)
    assert sol.tip_twist_in_range
    assert sol.cl_max_at_trim_twist is not None
    assert sol.cl_max_at_trim_twist != sol.cl_max_as_built


def test_unreachable_trim_reports_none_rather_than_a_fictional_clmax():
    """When the twist cannot be reached, there is no CLmax at it. A number here would
    describe an aircraft that cannot be built."""
    sol = solve_trim(_params(), CL_CRUISE, _cg(_params()), REYNOLDS)
    assert not sol.tip_twist_in_range
    assert sol.cl_max_at_trim_twist is None


def test_tolerance_only_moves_the_boolean():
    """CM_TRIM_TOLERANCE is judgment. It must not touch a computed quantity."""
    p = _params()
    sol = solve_trim(p, CL_CRUISE, _cg(p), REYNOLDS)
    assert sol.trims_as_built == (abs(sol.cm_cg) <= CM_TRIM_TOLERANCE)
    # and a wing trimmed exactly does read as trimmed
    at_trim = solve_trim(p, CL_CRUISE, sol.x_cg_for_trim_m, REYNOLDS)
    assert at_trim.trims_as_built


def test_tip_twist_range_is_read_from_the_schema():
    """Restating the limits would let a schema change make this module lie."""
    lo, hi = tip_twist_range()
    assert lo == -7 and hi == 2
    with pytest.raises(Exception):
        _params(tip_twist_deg=lo - 0.001)


def test_trim_is_evaluated_at_the_cruise_condition_it_is_given():
    """Trim residual depends on CL — a wing balanced at cruise is not balanced at
    every speed, and pretending otherwise would hide the one thing this reports."""
    p = _params()
    cg = _cg(p)
    slow, _, _ = pitching_moment(p, 0.8, cg, REYNOLDS)
    fast, _, _ = pitching_moment(p, 0.2, cg, REYNOLDS)
    assert slow != fast


def test_panel_and_trim_agree_on_the_flight_condition():
    """Integration: the numbers the card would show come from one estimate() call."""
    p = _params()
    panel = estimate(p, atmosphere_at(0.0), 18.0, 34.335, "hand")
    sol = solve_trim(p, panel.cl_cruise, panel.x_cg_for_target_sm_m,
                     panel.reynolds_mac)
    assert sol.cl_max_as_built > 0
    assert sol.dcm_dtwist_per_deg < 0, (
        "on a swept wing, LESS washout must lower Cm — a positive slope here would "
        "invert the trim advice")


def test_a_trimmable_wing_reads_as_trimmable():
    """Sweep plus reflex, the flying-wing recipe. The solve must be able to say YES —
    a model that only ever answers "cannot trim" is not distinguishable from one with
    an inverted sign."""
    p = _trimmable()
    sol = solve_trim(p, CL_CRUISE, _cg(p), REYNOLDS)
    assert sol.tip_twist_in_range
    assert abs(sol.cm_cg) < 0.01, sol.cm_cg
    assert sol.cm0_contribution > 0.0, (
        "reflexed sections must supply a NOSE-UP camber moment; a negative value here "
        "means the section data or the sign convention is wrong")
