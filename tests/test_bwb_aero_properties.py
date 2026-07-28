"""Physics-core properties over drawn inputs, not chosen examples (Stage 6).

The Stage-0 goldens pin one reference case; these assert the relationships that must
hold for EVERY legal geometry and flight condition. Each was measured as broken before
Stage 6 over the fuzzed envelope, with the measured rate in its docstring — a property
test is the only honest way to check a claim of the form "in 8.5% of geometries".

Pure `domain_packs`.
"""
import math

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from domain_packs.bwb import aero_estimates as A
from domain_packs.bwb.atmosphere import atmosphere_at
from strategies import bwb_params

_SETTINGS = settings(max_examples=300, deadline=None,
                     suppress_health_check=[HealthCheck.too_slow,
                                            HealthCheck.filter_too_much])


# ===================================================================================
# S6-F4 — one sweep convention
# ===================================================================================

@_SETTINGS
@given(params=bwb_params())
def test_c4_sweep_never_exceeds_le_sweep(params):
    """Both panels taper aft (the schema guarantees it), so the quarter-chord line
    cannot be swept further back than the leading edge. Measured broken in
    50,817/596,700 legal geometries (8.5%) while the two functions averaged in
    different spaces."""
    le = A.effective_le_sweep_deg(params)
    c4 = A.effective_c4_sweep_deg(params)
    assert c4 <= le + 1e-9, f"c/4 {c4:.3f} > LE {le:.3f}"


@_SETTINGS
@given(params=bwb_params())
def test_both_sweeps_lie_between_the_two_panel_sweeps(params):
    """A weighted mean cannot leave the interval its inputs span."""
    lo = min(params.sweep_inner_deg, params.sweep_outer_deg)
    hi = max(params.sweep_inner_deg, params.sweep_outer_deg)
    assert lo - 1e-9 <= A.effective_le_sweep_deg(params) <= hi + 1e-9


# ===================================================================================
# S6-F1 — cruise feasibility is exactly "v above stall"
# ===================================================================================

@_SETTINGS
@given(params=bwb_params(),
       v_ms=st.floats(min_value=6.0, max_value=60.0),
       mass_kg=st.floats(min_value=0.3, max_value=25.0),
       altitude_m=st.floats(min_value=0.0, max_value=4000.0))
def test_cruise_feasible_is_exactly_v_above_stall(params, v_ms, mass_kg, altitude_m):
    panel = A.estimate(params, atmosphere_at(altitude_m), v_ms, mass_kg * 9.81, "hand")
    assert panel.cruise_feasible == (panel.v_ms >= panel.v_stall_ms)
    if panel.cl_cruise > panel.cl_max_3d:
        assert not panel.cruise_feasible, \
            "cruise CL above CLmax is the same condition as cruising below stall"
    assert panel.stall_margin == pytest.approx(
        (panel.v_ms - panel.v_stall_ms) / panel.v_stall_ms, rel=1e-9)


# ===================================================================================
# S6-F9 / F10 — nothing non-physical produces a panel
# ===================================================================================

@pytest.mark.parametrize("v_ms", [0.0, -5.0, float("nan"), float("inf")], ids=repr)
@pytest.mark.parametrize("weight_n", [34.335, 0.0, -10.0], ids=repr)
def test_invalid_flight_conditions_raise(v_ms, weight_n):
    params = A.BWBParams(span_m=1.6, root_chord_m=0.55, centre_body_fraction=0.28,
                         mid_chord_frac=0.6, taper_outer=0.4, sweep_inner_deg=35,
                         sweep_outer_deg=18, root_airfoil="e474", mid_airfoil="e472", tip_airfoil="s5010", root_twist_deg=1.0,
                         tip_twist_deg=-3.0, dihedral_deg=3.0)
    with pytest.raises(ValueError):
        A.estimate(params, atmosphere_at(0.0), v_ms, weight_n, "hand")


# ===================================================================================
# S6-AE1 — the panel cannot exist in a state the physics forbids
# ===================================================================================

@pytest.mark.parametrize("bad", [
    {"oswald_e": 99.0},
    {"ld_max": -99.0},
    {"cd0": float("nan")},
    {"x_np_m": -0.1},
], ids=["e=99", "ld_max<0", "cd0=nan", "x_np<0"])
def test_aero_estimate_rejects_non_finite_and_impossible_values(bad):
    params = A.BWBParams(span_m=1.6, root_chord_m=0.55, centre_body_fraction=0.28,
                         mid_chord_frac=0.6, taper_outer=0.4, sweep_inner_deg=35,
                         sweep_outer_deg=18, root_airfoil="e474", mid_airfoil="e472", tip_airfoil="s5010", root_twist_deg=1.0,
                         tip_twist_deg=-3.0, dihedral_deg=3.0)
    good = A.estimate(params, atmosphere_at(0.0), 18.0, 34.335, "hand").model_dump()
    with pytest.raises(ValueError):
        A.AeroEstimate.model_validate({**good, **bad})


@_SETTINGS
@given(params=bwb_params(),
       v_ms=st.floats(min_value=6.0, max_value=60.0),
       mass_kg=st.floats(min_value=0.3, max_value=25.0))
def test_every_panel_satisfies_its_own_validator(params, v_ms, mass_kg):
    """The validator is only useful if real inputs can still produce a panel."""
    panel = A.estimate(params, atmosphere_at(0.0), v_ms, mass_kg * 9.81, "hand")
    assert A.AeroEstimate.model_validate(panel.model_dump()) is not None


# ===================================================================================
# S6-F3 — a clamped Oswald e is reported as clamped
# ===================================================================================

@_SETTINGS
@given(ar=st.floats(min_value=2.5, max_value=10.0),
       sweep=st.floats(min_value=-30.0, max_value=60.0))
def test_clamped_oswald_is_reported(ar, sweep):
    e, clamp = A.oswald_efficiency_detail(ar, sweep)
    assert A.OSWALD_FLOOR <= e <= A.OSWALD_CEILING
    if clamp == "floor":
        assert e == A.OSWALD_FLOOR
    elif clamp == "ceiling":
        assert e == A.OSWALD_CEILING
    else:
        assert A.OSWALD_FLOOR < e < A.OSWALD_CEILING


def test_a_clamped_panel_says_e_is_insensitive():
    """A clamped e has zero gradient in AR and sweep, and sensitivity.py takes finite
    differences of exactly this quantity."""
    params = A.BWBParams(span_m=0.8, root_chord_m=0.25, centre_body_fraction=0.12,
                         mid_chord_frac=0.35, taper_outer=0.7, sweep_inner_deg=60,
                         sweep_outer_deg=40, root_airfoil="e474", mid_airfoil="e472", tip_airfoil="s5010", root_twist_deg=2.0,
                         tip_twist_deg=-3.0, dihedral_deg=2.0)
    panel = A.estimate(params, atmosphere_at(0.0), 18.0, 34.335, "hand")
    assert panel.oswald_e_clamped == "floor", "premise: this geometry clamps e"
    assert any("INSENSITIVE" in a for a in panel.assumptions), panel.assumptions


def test_oswald_fit_crossing_is_at_ar_4_800():
    """Documenting test (S6-F3): below this AR the swept fit exceeds the straight one,
    so e rises with sweep — the wrong sign — and the unclamped fits give e > 1."""
    def gap(ar):
        straight = 1.78 * (1.0 - 0.045 * ar ** 0.68) - 0.64
        swept = 4.61 * (1.0 - 0.045 * ar ** 0.68) * math.cos(0.0) ** 0.15 - 3.1
        return swept - straight

    lo, hi = 3.0, 7.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if gap(lo) * gap(mid) <= 0:
            hi = mid
        else:
            lo = mid
    assert 0.5 * (lo + hi) == pytest.approx(A.OSWALD_FIT_CROSSING_AR, abs=1e-3)


def test_oswald_wrong_sign_band_is_documented():
    """e must not increase with sweep — but it does, between AR 3.6 and 4.7. The band
    is measured here so it cannot quietly change extent."""
    wrong_sign = [ar / 10.0 for ar in range(25, 100)
                  if A.oswald_efficiency(ar / 10.0, 12.0)
                  > A.oswald_efficiency(ar / 10.0, 6.2) + 1e-12]
    assert wrong_sign, "the documented wrong-sign band has disappeared — update S6-F3"
    assert min(wrong_sign) >= 3.0 and max(wrong_sign) <= 4.8, wrong_sign


# ===================================================================================
# S6-F5 / F6 — CLmax's blind spots, disclosed; forward sweep not penalised
# ===================================================================================

def test_clmax_is_documented_as_reynolds_independent():
    params = A.BWBParams(span_m=1.6, root_chord_m=0.55, centre_body_fraction=0.28,
                         mid_chord_frac=0.6, taper_outer=0.4, sweep_inner_deg=35,
                         sweep_outer_deg=18, root_airfoil="e474", mid_airfoil="e472", tip_airfoil="s5010", root_twist_deg=1.0,
                         tip_twist_deg=-3.0, dihedral_deg=3.0)
    assumptions = " ".join(
        A.estimate(params, atmosphere_at(0.0), 18.0, 34.335, "hand").assumptions)
    # S1-C2: this disclosure is RETIRED because the defect it disclosed is fixed.
    # CLmax responded to nothing; it is now solved from the spanwise load with each
    # section at its own local Reynolds. The assumption line must instead name the
    # method and the stall station.
    assert "Schrenk" in assumptions
    assert "semi-span" in assumptions
    assert "does not respond to speed or altitude" not in assumptions
    assert "cl_max_3d" in (A.cl_max_3d.__doc__ or "") or \
        "Reynolds" in (A.cl_max_3d.__doc__ or "")


def test_forward_sweep_is_not_penalised_like_aft_sweep():
    """`math.cos` is even, so a forward-swept quarter chord — 5.4% of the envelope,
    down to -31.03 deg — was knocked down exactly like an aft-swept one (S6-F6)."""
    forward = A.BWBParams(span_m=0.8, root_chord_m=1.2, centre_body_fraction=0.12,
                          mid_chord_frac=0.35, taper_outer=0.15, sweep_inner_deg=15,
                          sweep_outer_deg=5, root_airfoil="e474", mid_airfoil="e472", tip_airfoil="s5010", root_twist_deg=2.0,
                          tip_twist_deg=-3.0, dihedral_deg=2.0)
    from domain_packs.bwb.spanwise import solve_stall
    assert A.effective_c4_sweep_deg(forward) < 0, "premise: this planform sweeps forward"
    # S1-C2: the property is unchanged — a forward-swept quarter chord takes NO
    # knockdown — but the baseline it is compared against is now the solved spanwise
    # CLmax rather than a pair of constants.
    re = 4.0e5
    assert A.cl_max_3d(forward, re) == pytest.approx(
        solve_stall(forward, re).cl_max_wing, rel=1e-12)


def test_clmax_reynolds_dependence():
    """S7-CAP-4, RETIRED by S1-C2. Section CLmax now comes from the committed NeuralFoil
    screen at each station's own Reynolds, so the correlation is sourced rather than
    invented.

    The probe is weight, not cruise speed: CLmax is solved at the speed the wing
    actually stalls at, so cruising faster cannot change it (see
    test_clmax_does_not_depend_on_cruise_speed in the goldens module)."""
    params = A.BWBParams(span_m=1.6, root_chord_m=0.55, centre_body_fraction=0.28,
                         mid_chord_frac=0.6, taper_outer=0.4, sweep_inner_deg=35,
                         sweep_outer_deg=18, root_airfoil="e474", mid_airfoil="e472", tip_airfoil="s5010", root_twist_deg=1.0,
                         tip_twist_deg=-3.0, dihedral_deg=3.0)
    light = A.estimate(params, atmosphere_at(0.0), 18.0, 10.0, "hand")
    heavy = A.estimate(params, atmosphere_at(0.0), 18.0, 120.0, "hand")
    assert heavy.v_stall_ms > 2 * light.v_stall_ms       # premise: stall Re differs
    assert heavy.cl_max_3d > light.cl_max_3d


# ===================================================================================
# S6-F13 — the neutral-point bias is disclosed, and unplaceable CGs are flagged
# ===================================================================================

def test_np_docstring_discloses_the_bias_direction():
    description = A.AeroEstimate.model_fields["x_np_m"].description or ""
    assert "OPTIMISTIC" in description
    assert "lower bound" in description


def test_np_outside_the_root_chord_is_flagged():
    """x_np lands outside (0, root_chord) in 6.8% of the envelope, up to 2.48x the
    root chord — a CG recommendation with nowhere to put the mass."""
    params = A.BWBParams(span_m=0.8, root_chord_m=0.25, centre_body_fraction=0.28,
                         mid_chord_frac=0.6, taper_outer=0.7, sweep_inner_deg=60,
                         sweep_outer_deg=40, root_airfoil="e474", mid_airfoil="e472", tip_airfoil="s5010", root_twist_deg=2.0,
                         tip_twist_deg=-3.0, dihedral_deg=2.0)
    panel = A.estimate(params, atmosphere_at(0.0), 18.0, 34.335, "hand")
    assert panel.x_np_m > params.root_chord_m, "premise: this np is off the root chord"
    assert panel.cg_placement_infeasible is True
    assert any("unplaceable" in a for a in panel.assumptions)


# ===================================================================================
# S6-F11 — mutation cannot escape the constructor's guarantees
# ===================================================================================

@pytest.mark.parametrize("field,value", [
    ("centre_body_fraction", 0.0),
    ("root_chord_m", 0.0),
    ("span_m", 0.0),
    ("taper_outer", 0.0),
], ids=lambda v: str(v))
def test_post_construction_mutation_is_revalidated(field, value):
    """`p.centre_body_fraction = 0.0` used to be accepted, after which
    neutral_point_m returned a silently wrong 0.1727 (S6-F11)."""
    params = A.BWBParams(span_m=1.6, root_chord_m=0.55, centre_body_fraction=0.28,
                         mid_chord_frac=0.6, taper_outer=0.4, sweep_inner_deg=35,
                         sweep_outer_deg=18, root_airfoil="e474", mid_airfoil="e472", tip_airfoil="s5010", root_twist_deg=1.0,
                         tip_twist_deg=-3.0, dihedral_deg=3.0)
    with pytest.raises(ValueError):
        setattr(params, field, value)
