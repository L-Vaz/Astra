"""Profile drag from measured section data (S1-E).

The point of this module is that the wetted-area chain is BLIND to the aerofoil. So the
first test is that this one is not, and the rest are about being honest when the data
runs out.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from domain_packs.bwb.aero_estimates import estimate, parasite_drag_coeff
from domain_packs.bwb.airfoils import cl_ladder
from domain_packs.bwb.atmosphere import atmosphere_at
from domain_packs.bwb.schema import BWBParams
from domain_packs.bwb.section_drag import profile_drag

REYNOLDS = 4.09e5


def _params(**over) -> BWBParams:
    base = dict(span_m=1.6, root_chord_m=0.55, centre_body_fraction=0.28,
                mid_chord_frac=0.6, taper_outer=0.4, sweep_inner_deg=35,
                sweep_outer_deg=18, root_airfoil="e474", mid_airfoil="e472",
                tip_airfoil="s5010", root_twist_deg=1.0, tip_twist_deg=-3.0,
                dihedral_deg=3.0)
    base.update(over)
    return BWBParams(**base)


def test_drag_responds_to_the_aerofoil():
    """The whole reason this module exists.

    `parasite_drag_coeff` sees the section only through its thickness ratio, so two
    sections of the same t/c give it identical drag. Measured at cruise CL: swapping the
    tip from mh60 to e474 moves the strip integral from 0.01021 to 0.01106, +8.3%.
    """
    a = profile_drag(_params(tip_airfoil="mh60"), 0.3735, REYNOLDS)
    b = profile_drag(_params(tip_airfoil="e474"), 0.3735, REYNOLDS)
    assert b.cd_profile > a.cd_profile
    assert abs(b.cd_profile / a.cd_profile - 1.0) > 0.05


def test_drag_rises_with_lift():
    """A section's CD grows with its lift coefficient — the boundary layer thickens and
    pressure drag grows. This is exactly the viscous lift-dependent drag the classical
    polar has no home for, which is why `cd_profile` must not be added to a CD0."""
    cds = [profile_drag(_params(), cl, REYNOLDS).cd_profile
           for cl in (0.2, 0.35, 0.5, 0.65, 0.8)]
    assert cds == sorted(cds), cds


def test_drag_falls_with_reynolds():
    """Higher Reynolds, thinner boundary layer, lower skin friction."""
    lo = profile_drag(_params(), 0.3735, 1.0e5).cd_profile
    hi = profile_drag(_params(), 0.3735, 1.0e6).cd_profile
    assert hi < lo


def test_the_two_models_disagree_and_the_gap_is_reported():
    """Measured on the reference wing at cruise Reynolds:

        strip-integrated profile CD at zero lift   0.00990
        Raymer wetted-area CD0                     0.01360   (ratio 1.375)
        Raymer without its 1.10 interference margin 0.01237   (ratio 1.250)

    So the lumped 1.10 does not account for the gap. This test pins the DISAGREEMENT,
    not either value: if a future change makes the two agree, that is a real event and
    it should force a decision about which one drives the polar.
    """
    p = _params()
    d = profile_drag(p, 0.3735, REYNOLDS)
    assert d.cd0_wetted_area == pytest.approx(parasite_drag_coeff(p, REYNOLDS))
    assert d.disagreement_ratio == pytest.approx(1.375, abs=0.02)
    assert d.disagreement_ratio > 1.10, (
        "the wetted-area buildup already carries a 1.10 interference margin; if the "
        "residual gap ever drops below it, the two models have been reconciled and "
        "S1-E's deferral should be revisited")


def test_the_polar_still_uses_the_wetted_area_buildup():
    """The S1-E decision, asserted. Substituting the section buildup would take the
    reference wing's cruise L/D from 15.93 to 20.16 — a 27% gain from a model change,
    not a better aircraft. If someone wires it in, this is the tripwire."""
    import math
    p = _params()
    panel = estimate(p, atmosphere_at(0.0), 18.0, 34.335, "hand")
    k = 1.0 / (math.pi * panel.oswald_e * panel.aspect_ratio)
    assert panel.cd_cruise == pytest.approx(panel.cd0 + k * panel.cl_cruise ** 2)
    assert panel.cd0 == pytest.approx(parasite_drag_coeff(p, panel.reynolds_mac))
    # ...and the section numbers are nonetheless present and different
    assert panel.cd_profile_sections > 0
    assert panel.cd_profile_sections != panel.cd0
    assert panel.cd0_model_disagreement > 1.1


def test_off_ladder_span_is_reported_not_hidden():
    """A clamped drag value is indistinguishable from a measured one once it is inside
    an integral. Measured on the reference wing: at CL = 0 the washed-out tips carry
    negative local lift and 66.6% of the span falls below the ladder's 0.0 floor; at
    CL = 1.05, 94.4% sits above its 0.9 ceiling."""
    lo, hi = cl_ladder()[0], cl_ladder()[-1]
    assert (lo, hi) == (0.0, 0.9)

    at_zero = profile_drag(_params(), 0.0, REYNOLDS)
    assert at_zero.span_fraction_below_ladder > 0.5
    assert at_zero.span_fraction_clamped_at_zero_lift > 0.5

    high = profile_drag(_params(), 1.05, REYNOLDS)
    assert high.span_fraction_above_ladder > 0.9
    assert high.span_fraction_below_ladder == 0.0

    cruise = profile_drag(_params(), 0.3735, REYNOLDS)
    assert cruise.span_fraction_below_ladder == 0.0
    assert cruise.span_fraction_above_ladder == 0.0


def test_the_tables_own_error_is_carried():
    """5.04% median, p90 16.52%. A consumer that quotes this drag to four decimals
    without it is over-claiming by two orders of magnitude."""
    d = profile_drag(_params(), 0.3735, REYNOLDS)
    assert d.ladder_interpolation_error_pct == pytest.approx(5.04)


def test_hoisted_surface_matches_the_table_walk():
    """The bilinear surface is a PERFORMANCE hoist of `airfoils.section_cd`, not a
    different model. If they diverge, the speedup has changed the physics."""
    from domain_packs.bwb.airfoils import section_cd
    from domain_packs.bwb.section_drag import _surface
    surf = _surface("s5010")
    for re in (1.2e5, 3.0e5, 4.09e5, 7.5e5):
        for cl in (0.0, 0.12, 0.3, 0.55, 0.9):
            assert surf.at(cl, re) == pytest.approx(section_cd("s5010", cl, re)[0],
                                                    rel=1e-12), (re, cl)


def test_runs_across_the_geometry_envelope():
    for cbf in (0.12, 0.28, 0.45):
        for tap in (0.15, 0.4, 0.7):
            # root twist is 1.0, and the schema rejects wash-in, so the tip twist
            # sweep stops there rather than at the field's +2 ceiling.
            for tw in (1.0, -3.0, -7.0):
                p = _params(centre_body_fraction=cbf, taper_outer=tap,
                            tip_twist_deg=tw)
                d = profile_drag(p, 0.4, REYNOLDS)
                assert d.cd_profile > 0
                assert 0.0 <= d.span_fraction_below_ladder <= 1.0
                assert 0.0 <= d.span_fraction_above_ladder <= 1.0
