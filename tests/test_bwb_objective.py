"""MDO objective tests — pure, no AeroSandbox.

The objective consumes an already-computed AsbAeroResult (a plain pydantic model),
so these tests build results by hand and check the scoring logic in isolation:
breakdown arithmetic, penalty monotonicity, structural dormancy, and the hard
feasibility floors.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from domain_packs.bwb.asb_aero import AsbAeroResult
from domain_packs.bwb.objective import (ObjectiveConfig, ObjectiveWeights,
                                        ScoreBreakdown, score)
from domain_packs.bwb.schema import BWBParams

_STRUCTURAL = ("m_root", "u_structure", "sigma_peak", "sigma_cv", "shape_3d")


def _params(**over) -> BWBParams:
    base = dict(span_m=1.8, root_chord_m=0.5, centre_body_fraction=0.28,
                mid_chord_frac=0.55, taper_outer=0.5, sweep_inner_deg=34,
                sweep_outer_deg=20, root_airfoil="e474", mid_airfoil="e472", tip_airfoil="s5010",
                root_twist_deg=2, tip_twist_deg=-3, dihedral_deg=3)
    base.update(over)
    return BWBParams(**base)


def _aero(params: BWBParams, *, l_over_d=20.0, cn_beta=0.02, cm_alpha=-0.2,
          cm_trim=0.0, x_np=None, x_cg=None, mac=None) -> AsbAeroResult:
    mac = mac if mac is not None else params.mean_aero_chord_m()
    # default CG/NP giving static margin = 0.08
    x_cg = 0.20 if x_cg is None else x_cg
    x_np = (x_cg + 0.08 * mac) if x_np is None else x_np
    return AsbAeroResult(
        velocity_ms=18.0, reynolds_mac=3.5e5, cl_target=0.33, alpha_trim_deg=3.0,
        trim_in_sweep=True, cl=0.33, cd=0.33 / l_over_d, cd_induced=0.006,
        cd_profile=0.009, l_over_d=l_over_d, cm_at_trim=cm_trim,
        cl_alpha_per_rad=5.3, cm_alpha_per_rad=cm_alpha, cn_beta_per_rad=cn_beta,
        cl_beta_per_rad=-0.06, mac_m=mac, s_ref_m2=params.planform_area_m2(),
        b_ref_m=params.span_m, x_np_m=x_np, x_cg_m=x_cg, cg_frac_mac=0.25)


def _config(params: BWBParams, **over) -> ObjectiveConfig:
    base = dict(required_cl=0.33, static_margin_target=0.08,
                s_ref_target_m2=params.planform_area_m2())
    base.update(over)
    return ObjectiveConfig(**base)


def test_breakdown_arithmetic():
    p = _params()
    bd = score(p, _aero(p), ObjectiveWeights(), _config(p))
    assert bd.feasible
    assert bd.total_penalty == pytest.approx(sum(t.contribution for t in bd.penalties.values()))
    assert bd.score == pytest.approx(bd.aero_efficiency - bd.total_penalty)
    assert bd.aero_efficiency == pytest.approx(20.0)


def test_structural_terms_dormant_by_default():
    p = _params()
    bd = score(p, _aero(p), ObjectiveWeights(), _config(p))
    assert not bd.structural_active
    for name in _STRUCTURAL:
        assert bd.penalties[name].weight == 0.0
        assert bd.penalties[name].contribution == 0.0


def test_structural_weight_on_is_still_zero_until_block_b():
    # Raising a structural weight flips 'active' but the raw term is still a
    # placeholder 0 until the beam surrogate exists -> no effect on the score yet.
    p = _params()
    w = ObjectiveWeights(w1_m_root=5.0)
    bd = score(p, _aero(p), w, _config(p))
    assert bd.structural_active
    assert bd.penalties["m_root"].raw == 0.0
    assert bd.penalties["m_root"].contribution == 0.0


def test_geometry_penalty_zero_at_target_area():
    p = _params()
    bd = score(p, _aero(p), ObjectiveWeights(), _config(p))
    assert bd.penalties["geometry"].raw == pytest.approx(0.0)


def test_geometry_penalty_grows_off_target():
    p = _params()
    cfg = _config(p, s_ref_target_m2=p.planform_area_m2() * 0.5)  # target far from actual
    bd = score(p, _aero(p), ObjectiveWeights(), cfg)
    assert bd.penalties["geometry"].raw > 0.0


def test_pitch_penalty_monotonic_in_static_margin_error():
    p = _params()
    cfg = _config(p, static_margin_target=0.08)
    mac = p.mean_aero_chord_m()
    # SM exactly on target -> ~0 penalty; SM far off -> larger penalty
    on = score(p, _aero(p, x_cg=0.20, x_np=0.20 + 0.08 * mac, mac=mac), ObjectiveWeights(), cfg)
    off = score(p, _aero(p, x_cg=0.20, x_np=0.20 + 0.20 * mac, mac=mac), ObjectiveWeights(), cfg)
    assert on.penalties["pitch"].raw == pytest.approx(0.0, abs=1e-9)
    assert off.penalties["pitch"].raw > on.penalties["pitch"].raw


def test_latdir_penalty_monotonic_in_cn_beta():
    p = _params()
    more_stable = score(p, _aero(p, cn_beta=0.05), ObjectiveWeights(), _config(p))
    less_stable = score(p, _aero(p, cn_beta=0.005), ObjectiveWeights(), _config(p))
    assert less_stable.penalties["latdir"].raw > more_stable.penalties["latdir"].raw


def test_trim_penalty_monotonic_in_residual_moment():
    p = _params()
    trimmed = score(p, _aero(p, cm_trim=0.0), ObjectiveWeights(), _config(p))
    untrimmed = score(p, _aero(p, cm_trim=0.15), ObjectiveWeights(), _config(p))
    assert untrimmed.penalties["trim"].raw > trimmed.penalties["trim"].raw


def test_pitch_instability_is_infeasible():
    p = _params()
    mac = p.mean_aero_chord_m()
    # x_np forward of x_cg -> negative static margin -> rejected
    bd = score(p, _aero(p, x_cg=0.25, x_np=0.20, mac=mac), ObjectiveWeights(), _config(p))
    assert not bd.feasible
    assert "pitch" in bd.reject_reason
    assert bd.score < -1e5


def test_latdir_floor_off_by_default():
    p = _params()
    bd = score(p, _aero(p, cn_beta=-0.01), ObjectiveWeights(), _config(p))
    assert bd.feasible  # penalised but not rejected when floor not enforced


def test_latdir_floor_enforced_rejects():
    p = _params()
    cfg = _config(p, enforce_latdir_floor=True, cn_beta_floor=0.0)
    bd = score(p, _aero(p, cn_beta=-0.01), ObjectiveWeights(), cfg)
    assert not bd.feasible
    assert "lateral-directional" in bd.reject_reason


def test_weights_scale_contribution():
    p = _params()
    base = score(p, _aero(p, cn_beta=0.005), ObjectiveWeights(w6_latdir=1.0), _config(p))
    doubled = score(p, _aero(p, cn_beta=0.005), ObjectiveWeights(w6_latdir=2.0), _config(p))
    assert doubled.penalties["latdir"].contribution == pytest.approx(
        2.0 * base.penalties["latdir"].contribution)


# --------------------------------------------------------------------------
# w10 — stall station (S1-C2)
#
# The wing that motivated this term is the golden reference: it stalls at eta
# 0.647, outboard, and -3 deg of washout is NOT enough to bring it inboard. Before
# S1-C2 nothing in the pack could see that, because CLmax was a constant.
# --------------------------------------------------------------------------

def _reference_params(**over) -> BWBParams:
    """The golden reference planform — the one measured to stall outboard."""
    base = dict(span_m=1.6, root_chord_m=0.55, centre_body_fraction=0.28,
                mid_chord_frac=0.6, taper_outer=0.4, sweep_inner_deg=35,
                sweep_outer_deg=18, root_airfoil="e474", mid_airfoil="e472",
                tip_airfoil="s5010", root_twist_deg=1.0, tip_twist_deg=-3.0,
                dihedral_deg=3.0)
    base.update(over)
    return BWBParams(**base)


def _reference_aero(p: BWBParams) -> AsbAeroResult:
    """At the reference cruise Reynolds, 409k.

    Pinned explicitly rather than inherited from `_aero`'s 3.5e5, because the stall
    STATION is Reynolds-sensitive and not only the CLmax: measured on this planform,
    the converged onset moves from eta 0.569 at a 350k cruise Reynolds to 0.628 at
    409k. Section CLmax rises with Reynolds, and it rises fastest at the tip, whose
    chord Reynolds is lowest and furthest down the steep part of the curve — so more
    speed buys the tip proportionally more capability and pushes the first stall
    outboard. A test that let this float would be measuring the fixture, not the wing.
    """
    return _aero(p).model_copy(update={"reynolds_mac": 4.09e5})


def test_outboard_stall_is_reported_and_penalised():
    """Measured: eta 0.628 at the converged stall Reynolds, outboard of 0.60."""
    p = _reference_params()
    bd = score(p, _reference_aero(p), ObjectiveWeights(), _config(p))
    assert bd.stall_is_outboard, "the reference wing stalls outboard — that is the point"
    assert bd.stall_eta > 0.60
    assert bd.penalties["stall_station"].contribution > 0.0
    assert "OUTBOARD" in bd.penalties["stall_station"].note


def test_washout_moves_stall_inboard_and_relieves_the_penalty():
    """The term must reward the lever an operator actually has.

    Measured across the schema's washout range on the reference planform, at the
    converged stall Reynolds and with the AERODYNAMIC twist reference (S1-D):

        tip twist  +1.0   0.0   -1.0   -2.0   -3.0   -5.0   -7.0
        stall eta  0.738 0.721 0.721  0.703  0.684  0.647  0.589

    Monotone (non-strictly: 0.0 and -1.0 land on the same integration station), and
    it only just clears the 0.60 flag at -7 deg — the schema FLOOR. Before the
    aerodynamic-twist correction the same sweep crossed at -5 deg with room to spare;
    the cambered s5010 tip cancels about a third of every degree of washout applied,
    so the whole usable range now sits on the wrong side of the flag but for its last
    point. That is a finding about this wing, not a test artefact.
    """
    etas, raws = [], []
    for tw in (1.0, 0.0, -1.0, -2.0, -3.0, -5.0, -7.0):
        p = _reference_params(tip_twist_deg=tw)
        bd = score(p, _reference_aero(p), ObjectiveWeights(), _config(p))
        etas.append(bd.stall_eta)
        raws.append(bd.penalties["stall_station"].raw)
    assert etas == sorted(etas, reverse=True), f"not monotone in washout: {etas}"
    assert raws == sorted(raws, reverse=True), f"penalty not monotone: {raws}"
    assert raws[0] > 0.0, f"the untwisted end of the sweep must be penalised: {raws}"
    assert raws[-1] == 0.0, (
        "the schema's washout FLOOR (-7 deg) is the only setting that clears the flag "
        f"on this wing: {raws}")


def test_stall_penalty_is_zero_inboard_with_no_cliff():
    """Zero inboard of the flag, continuous across it — not a step.

    A step would give the optimiser zero gradient on both sides and a cliff between:
    it would either ignore the term or park exactly on the boundary."""
    from domain_packs.bwb.objective import _stall_station_penalty
    assert _stall_station_penalty(0.30, 0.60) == 0.0
    assert _stall_station_penalty(0.60, 0.60) == 0.0
    assert _stall_station_penalty(0.601, 0.60) == pytest.approx(0.0025, abs=1e-6)
    assert _stall_station_penalty(1.00, 0.60) == pytest.approx(1.0)
    assert _stall_station_penalty(1.50, 0.60) == 1.0     # clipped, never > 1


def test_stall_station_uses_the_stall_reynolds_not_the_cruise_one():
    """A wing stalls at its stall speed. Measured on the reference: CLmax 1.2057 at
    the cruise Reynolds (409k) against 1.1208 converged — 7% apart. Getting this
    wrong would be the error S1-C2 exists to remove, one level up."""
    from domain_packs.bwb.objective import stall_station_at_stall_reynolds
    from domain_packs.bwb.spanwise import solve_stall
    p = _reference_params()
    a = _reference_aero(p)
    naive = solve_stall(p, a.reynolds_mac)
    converged = stall_station_at_stall_reynolds(p, a)
    assert converged.cl_max_wing < naive.cl_max_wing
    assert abs(converged.cl_max_wing / naive.cl_max_wing - 1.0) > 0.02


def test_stall_weight_scales_like_every_other_weight():
    p = _reference_params()
    base = score(p, _reference_aero(p), ObjectiveWeights(w10_stall_station=1.0), _config(p))
    doubled = score(p, _reference_aero(p), ObjectiveWeights(w10_stall_station=2.0), _config(p))
    assert doubled.penalties["stall_station"].contribution == pytest.approx(
        2.0 * base.penalties["stall_station"].contribution)
    assert base.penalties["stall_station"].contribution > 0.0
