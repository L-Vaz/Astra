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
                sweep_outer_deg=20, root_thickness_pct=13, tip_thickness_pct=9,
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
