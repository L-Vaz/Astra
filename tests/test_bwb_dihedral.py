"""Effective dihedral from sweep (S1-H).

The load-bearing tests here are the two that RECONCILE THE UNITS, because that is the
one place this could be quietly wrong by a factor of 57.3 and still look plausible on a
card. Both reproduce numbers from the source rather than from this implementation.
"""
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from domain_packs.bwb.aero_estimates import (effective_c2_sweep_deg,
                                             effective_c4_sweep_deg,
                                             effective_le_sweep_deg, estimate)
from domain_packs.bwb.atmosphere import atmosphere_at
from domain_packs.bwb.dihedral import (CL_BETA_PER_DEGREE_OF_DIHEDRAL,
                                       effective_dihedral,
                                       sweep_cl_beta_per_rad,
                                       sweep_contribution_deg)
from domain_packs.bwb.schema import BWBParams


def _params(**over) -> BWBParams:
    base = dict(span_m=1.6, root_chord_m=0.55, centre_body_fraction=0.28,
                mid_chord_frac=0.6, taper_outer=0.4, sweep_inner_deg=35,
                sweep_outer_deg=18, root_airfoil="e474", mid_airfoil="e472",
                tip_airfoil="s5010", root_twist_deg=1.0, tip_twist_deg=-3.0,
                dihedral_deg=3.0)
    base.update(over)
    return BWBParams(**base)


# ------------------------------------------------------ the unit reconciliation

def test_the_equation_is_per_radian_not_per_degree():
    """Kuhlman's Graphs 3 and 4 both run their effective-dihedral axis to 30 degrees,
    and both reach their top-right corner at sweep 30 deg, CL 1.2.

    Treating McCormick's equation as per-radian and converting with the article's own
    per-DEGREE equivalence of -0.00021 puts that corner at 26.4 deg (taper 0.6) and
    28.8 deg (taper 1.0) — just inside both axes. Treating it as already per-degree
    puts it at 1512 deg, off by 57.3. This test pins the resolution."""
    per_rad = sweep_contribution_deg(0.6, 1.2, 30.0)
    assert per_rad == pytest.approx(26.4, abs=0.15)
    assert 0.0 < per_rad < 30.0, "must fit under Graph 3's 30-degree axis"

    taper_one = sweep_contribution_deg(1.0, 1.2, 30.0)
    assert taper_one == pytest.approx(28.8, abs=0.15)
    assert 0.0 < taper_one < 30.0, "must fit under Graph 4's 30-degree axis"

    # the wrong reading, stated so the failure mode is named in the test suite
    wrong = sweep_cl_beta_per_rad(0.6, 1.2, 30.0) / CL_BETA_PER_DEGREE_OF_DIHEDRAL
    assert wrong == pytest.approx(1512, rel=0.01)
    assert wrong / per_rad == pytest.approx(math.degrees(1.0), rel=1e-9)


def test_the_disputed_rule_of_thumb_lands_at_an_ordinary_cruise_cl():
    """Independent of the graphs. The article exists partly to dispute "three degrees
    of sweep equals one degree of geometric dihedral", on the grounds that the real
    effect depends on CL.

    Under the correct unit reading that rule is exact at CL 0.50 (taper 0.6) and 0.46
    (taper 1.0) — ordinary cruise lift coefficients, which is exactly why a CL-blind
    rule of thumb appears to work at one speed. Under the wrong reading it would be
    exact at CL 0.0087, which nothing flies at, and the article's argument would make
    no sense."""
    for taper, expect in ((0.6, 0.501), (1.0, 0.459)):
        per_unit_cl = sweep_contribution_deg(taper, 1.0, 3.0)
        assert 1.0 / per_unit_cl == pytest.approx(expect, abs=0.005)


# --------------------------------------------------------------- the directions

def test_dihedral_effect_scales_with_lift_coefficient():
    """The article's central point: swept wings have a CONTINUOUSLY VARIABLE dihedral
    effect. Strong when slow, weak when fast — which is why a swept flying wing can be
    docile in a thermal and free of Dutch roll on a fast leg."""
    p = _params()
    vals = [effective_dihedral(p, cl).sweep_deg for cl in (0.2, 0.4, 0.6, 0.8, 1.0)]
    assert vals == sorted(vals)
    assert effective_dihedral(p, 0.0).sweep_deg == pytest.approx(0.0)


def test_aft_sweep_gives_positive_dihedral_effect_and_forward_sweep_reverses_it():
    """The sign, which nothing here takes an absolute value of. This schema DOES reach
    a forward-swept half chord — measured minimum -10.8 deg across the corner scan."""
    assert sweep_contribution_deg(0.5, 0.5, 20.0) > 0
    assert sweep_contribution_deg(0.5, 0.5, -20.0) < 0
    assert sweep_contribution_deg(0.5, 0.5, 0.0) == pytest.approx(0.0)


def test_negative_cl_beta_is_the_stabilising_direction():
    """Cl_beta negative == positive dihedral effect, the convention the -0.00021
    equivalence is written in."""
    d = effective_dihedral(_params(), 0.4)
    assert d.total_deg > 0
    assert d.cl_beta_per_rad < 0


def test_geometric_dihedral_adds_one_for_one():
    """"Degrees of effective dihedral" is DEFINED as the geometric dihedral producing
    the same Cl_beta, so the geometric part is an identity, not a model."""
    a = effective_dihedral(_params(dihedral_deg=0.0), 0.4)
    b = effective_dihedral(_params(dihedral_deg=5.0), 0.4)
    assert b.total_deg - a.total_deg == pytest.approx(5.0)
    assert b.sweep_deg == pytest.approx(a.sweep_deg)


def test_taper_dependence_is_weak_and_bounded():
    """The factor (1+2*lam)/(3(1+lam)) runs 0.333 at lam=0 to 0.500 at lam=1 and is
    monotone. That bound is what makes using a single tip/root ratio for a two-panel
    BWB an acceptable approximation."""
    factors = [-sweep_cl_beta_per_rad(lam, 1.0, 45.0) / math.tan(math.radians(45.0))
               for lam in (0.0, 0.25, 0.5, 0.75, 1.0)]
    assert factors == sorted(factors)
    assert factors[0] == pytest.approx(1.0 / 3.0)
    assert factors[-1] == pytest.approx(0.5)


# ------------------------------------------------------------- the sweep line

def test_half_chord_sweep_is_its_own_line():
    """McCormick's increment is written at the HALF chord. Reusing the quarter-chord
    value would be a silent error, and on this planform not a small one: measured
    LE 23.27, c/4 16.67, c/2 9.58 degrees — the c/4 tangent is 75% larger than the
    c/2 tangent, and the dihedral increment is proportional to tan(sweep)."""
    p = _params()
    le, c4, c2 = (effective_le_sweep_deg(p), effective_c4_sweep_deg(p),
                  effective_c2_sweep_deg(p))
    assert le > c4 > c2, (le, c4, c2)
    assert c2 == pytest.approx(9.578, abs=0.01)
    wrong = sweep_contribution_deg(0.24, 0.3735, c4)
    right = sweep_contribution_deg(0.24, 0.3735, c2)
    assert wrong / right > 1.7


def test_half_chord_sweep_uses_the_tangent_convention():
    """Same averaging as its two neighbours (S6-F4). Angle-space averaging would make
    the three lines mutually inconsistent, which is the defect that let drag and stall
    run on contradictory sweeps."""
    p = _params(sweep_inner_deg=60, sweep_outer_deg=40)
    c_root, c_mid, c_tip = p.chords()
    b1, b2 = p.half_widths()
    t1 = math.tan(math.radians(60)) - 0.5 * (c_root - c_mid) / b1
    t2 = math.tan(math.radians(40)) - 0.5 * (c_mid - c_tip) / b2
    expected = math.degrees(math.atan((b1 * t1 + b2 * t2) / (b1 + b2)))
    assert effective_c2_sweep_deg(p) == pytest.approx(expected)


# ------------------------------------------------------------------ the panel

def test_the_panel_reports_it_at_both_ends_of_the_speed_range():
    """Measured on the reference wing: 5.08 deg at cruise, 8.95 deg at CLmax, of which
    the geometric dihedral is a constant 3.00. Slow flight nearly triples the sweep
    contribution — and slow flight is where the ailerons are weakest."""
    p = _params()
    panel = estimate(p, atmosphere_at(0.0), 18.0, 34.335, "hand")
    assert panel.effective_dihedral_deg == pytest.approx(5.08, abs=0.05)
    assert panel.effective_dihedral_at_clmax_deg == pytest.approx(8.95, abs=0.05)
    assert panel.effective_dihedral_at_clmax_deg > panel.effective_dihedral_deg
    assert panel.effective_dihedral_from_sweep_deg == pytest.approx(
        panel.effective_dihedral_deg - p.dihedral_deg)
    assert panel.cl_beta_per_rad < 0
    assert panel.effective_c2_sweep_deg == pytest.approx(effective_c2_sweep_deg(p))


def test_it_is_reported_not_optimised():
    """S1-H's scope. `dihedral_deg` must come out exactly as the user set it — Stage 2
    optimises it against a measured Cl_beta, Stage 1 only says what it is doing."""
    for dh in (0.0, 3.0, 8.0):
        p = _params(dihedral_deg=dh)
        panel = estimate(p, atmosphere_at(0.0), 18.0, 34.335, "hand")
        assert p.dihedral_deg == dh
        assert panel.effective_dihedral_deg == pytest.approx(
            dh + panel.effective_dihedral_from_sweep_deg)
