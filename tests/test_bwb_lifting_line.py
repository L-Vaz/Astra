"""Span efficiency from Prandtl's lifting line (S1-G).

The load-bearing test here is `test_elliptic_wing_has_zero_delta`. Lifting-line theory
has exactly one closed-form answer, and a solver that reproduces it to machine
precision has its collocation, its harmonics and its indexing all correct. Everything
else in this file is a trend or a bound.
"""
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from domain_packs.bwb.aero_estimates import estimate, oswald_efficiency
from domain_packs.bwb.atmosphere import atmosphere_at
from domain_packs.bwb.lifting_line import (N_TERMS, delta_from_coefficients,
                                           fourier_coefficients, span_efficiency,
                                           span_efficiency_tapered)
from domain_packs.bwb.schema import BWBParams


def _reference() -> BWBParams:
    return BWBParams(span_m=1.6, root_chord_m=0.55, centre_body_fraction=0.28,
                     mid_chord_frac=0.6, taper_outer=0.4, sweep_inner_deg=35,
                     sweep_outer_deg=18, root_airfoil="e474", mid_airfoil="e472",
                     tip_airfoil="s5010", root_twist_deg=1.0, tip_twist_deg=-3.0,
                     dihedral_deg=3.0)


def test_elliptic_wing_has_zero_delta():
    """The one analytic case. An elliptic chord distribution puts every harmonic above
    the first at zero, so delta = 0 and e = 1 exactly. A collocation, harmonic-ordering
    or indexing error could not survive this."""
    span, ar = 1.0, 6.0
    s_ref = span * span / ar
    chords = [(4.0 * s_ref / (math.pi * span))
              * math.sin((i + 1) * math.pi / (2.0 * N_TERMS))
              for i in range(N_TERMS)]
    coeffs = fourier_coefficients(chords, span, 2.0 * math.pi)
    assert delta_from_coefficients(coeffs) == pytest.approx(0.0, abs=1e-20)


def test_e_never_exceeds_one():
    """delta is a sum of squares, so this holds BY CONSTRUCTION — which is the point.
    Raymer's unclamped fits return e = 1.123 at AR 2.5, a wing beating the elliptic
    minimum, and the old clamp hid that rather than flagging it."""
    for ar in (2.0, 3.0, 5.0, 8.0, 12.0, 20.0):
        for taper in (0.15, 0.3, 0.5, 0.7, 1.0):
            delta, e = span_efficiency_tapered(ar, taper)
            assert delta >= 0.0, (ar, taper, delta)
            assert 0.0 < e <= 1.0, (ar, taper, e)


def test_delta_rises_monotonically_with_aspect_ratio():
    """Longer wings depart further from elliptic on a fixed taper. Monotone by
    construction here — no fits to cross, no plateau."""
    deltas = [span_efficiency_tapered(ar, 0.5)[0]
              for ar in (4.0, 6.0, 8.0, 10.0, 12.0, 16.0, 20.0)]
    assert deltas == sorted(deltas)


def test_taper_optimum_sits_where_classical_theory_puts_it():
    """Measured at AR 5.83: delta minimises at taper 0.4 (0.0084) and worsens both
    ways — 0.0180 at 0.2, 0.0466 at 1.0. The classical optimum for a straight tapered
    wing is around 0.35-0.40, and recovering it from first principles is the check
    that the physics, not just the arithmetic, is right."""
    tapers = [0.2, 0.3, 0.4, 0.5, 0.7, 1.0]
    deltas = [span_efficiency_tapered(5.83, t)[0] for t in tapers]
    best = tapers[deltas.index(min(deltas))]
    assert 0.3 <= best <= 0.45, dict(zip(tapers, deltas))
    assert deltas[-1] == max(deltas), "rectangular must be the worst of the family"


def test_converged_in_the_number_of_harmonics():
    """N_TERMS is a discretisation choice; it must not be a physical one."""
    coarse = span_efficiency_tapered(5.83, 0.5, n_terms=10)[0]
    fine = span_efficiency_tapered(5.83, 0.5, n_terms=160)[0]
    assert abs(fine - coarse) < 1e-3, (coarse, fine)


def test_the_published_datapoint_does_not_reproduce_and_that_is_recorded():
    """The plan carried "AR 5.83, taper 0.5 -> delta 0.0557, e 0.947" as the validation
    target. It does not reproduce: this solver gives delta 0.0112, e 0.9889.

    This test PINS THE DISAGREEMENT rather than papering over it. The elliptic case
    above says the solver is right, so the discrepancy is about WHICH QUANTITY the
    source is reporting — a whole-aircraft Oswald efficiency (viscous lift-dependent
    drag and interference included, typically 0.75-0.85) is not a planar inviscid span
    efficiency (0.98-0.99). Because of it, S1-G deliberately did NOT swap the drag
    polar. If someone later reconciles the two, this test is where the reconciliation
    must be recorded — deleting it silently is how the question would get lost.
    """
    delta, e = span_efficiency_tapered(5.83, 0.5)
    assert delta == pytest.approx(0.0112, abs=5e-4)
    assert e == pytest.approx(0.9889, abs=5e-4)
    assert not (0.05 < delta < 0.06), (
        "the published datapoint now reproduces — reconcile the two definitions and "
        "revisit whether the drag polar should use the lifting-line value")


def test_span_efficiency_is_reported_but_does_not_drive_the_polar():
    """The whole S1-G decision, asserted. If someone wires the lifting-line value into
    `cd_cruise`, every L/D on every card moves by roughly 20% of the induced term and
    this test is the tripwire."""
    p = _reference()
    panel = estimate(p, atmosphere_at(0.0), 18.0, 34.335, "hand")
    delta, e_planar = span_efficiency(p)

    assert panel.span_efficiency_planar == pytest.approx(e_planar)
    assert panel.span_efficiency_delta == pytest.approx(delta)
    # the POLAR still uses the Raymer fit
    assert panel.oswald_e == pytest.approx(
        oswald_efficiency(p.aspect_ratio(),
                          __import__("domain_packs.bwb.aero_estimates",
                                     fromlist=["x"]).effective_le_sweep_deg(p)))
    k = 1.0 / (math.pi * panel.oswald_e * panel.aspect_ratio)
    assert panel.cd_cruise == pytest.approx(panel.cd0 + k * panel.cl_cruise ** 2)
    # and the gap is reported rather than hidden
    assert panel.oswald_e_span_gap == pytest.approx(
        panel.span_efficiency_planar - panel.oswald_e)
    assert panel.oswald_e_span_gap > 0.1, (
        "measured 0.978 inviscid against 0.816 from the Oswald fit — if this gap ever "
        "closes on its own, one of the two models changed meaning")


def test_bwb_planform_is_solvable_across_the_envelope():
    """The centre body makes this chord distribution far from a simple taper. The
    solver must not go singular on any legal geometry."""
    for cbf in (0.12, 0.28, 0.45):
        for mcf in (0.3, 0.6, 0.85):
            for tap in (0.15, 0.4, 0.7):
                p = _reference().model_copy(update={
                    "centre_body_fraction": cbf, "mid_chord_frac": mcf,
                    "taper_outer": tap})
                delta, e = span_efficiency(p)
                assert delta >= 0.0 and 0.0 < e <= 1.0, (cbf, mcf, tap, delta, e)
