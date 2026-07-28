"""AeroSandbox evaluator (block A) tests.

These need the aerosandbox stack; skipped where it is not installed (e.g. the
OpenVSP venv). They check the BWBParams -> Airplane adapter mirrors vsp_builder
station-for-station, and that the evaluated aero is physical and deterministic.
"""
import inspect
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

pytest.importorskip("aerosandbox", reason="aerosandbox not installed in this env")

from domain_packs.bwb import asb_aero
from domain_packs.bwb.defaults import derive
from domain_packs.bwb.schema import BWBParams, MissionSpec


def test_the_thickness_snap_is_gone():
    """S1-C: replaces test_thickness_snap_error_is_bounded.

    This tier used to build every station as a symmetric NACA 00xx snapped to an
    integer percent, worth up to 12.5% t/c error at the thinnest legal section and —
    worse — scoring a different shape than the preview drew. The snap was justified
    by NeuralFoil's NACA-4 parametrization being fast, but NeuralFoil takes arbitrary
    coordinates, so it never held once sections were real.

    Nothing may reintroduce it: the fast tier must score the aerofoil that was
    chosen."""
    for gone in ("tc_snap_error", "MAX_TC_SNAP_ERROR", "_symmetric_naca"):
        assert not hasattr(asb_aero, gone), (
            f"asb_aero.{gone} is back — the fast tier is approximating the section "
            f"again instead of building it")
    source = inspect.getsource(asb_aero.bwbparams_to_airplane)
    for station in ("root_airfoil", "mid_airfoil", "tip_airfoil"):
        assert f"p.{station}" in source, (
            f"the airplane builder no longer reads p.{station}")


def test_the_section_reaches_the_airplane_unrounded():
    """S1-C: replaces test_the_probed_snap_case_is_within_the_bound, which pinned the
    size of a rounding error that no longer exists. The station thickness the fast
    tier builds must now be the section's own, to full precision."""
    p = _derived().params
    plane = asb_aero.bwbparams_to_airplane(p)
    names = [x.airfoil.name.lower() for x in plane.wings[0].xsecs]
    for chosen in (p.root_airfoil, p.mid_airfoil, p.tip_airfoil):
        assert any(chosen.lower() in n for n in names), (
            f"section {chosen!r} did not reach the airplane; got {names}")


def _derived():
    return derive(MissionSpec(payload_kg=1.0, cruise_speed_ms=18.0,
                              cruise_altitude_m=300.0, launch="hand"))


@pytest.fixture(scope="module")
def derived():
    return _derived()


@pytest.fixture(scope="module")
def params(derived):
    return derived.params


def test_adapter_mirrors_vsp_builder_stations(params):
    ap = asb_aero.bwbparams_to_airplane(params)
    wing = ap.wings[0]
    assert wing.symmetric
    assert len(wing.xsecs) == 3  # root / blend / tip, like vsp_builder XSec_0/1/2
    c_root, c_mid, c_tip = params.chords()
    assert wing.xsecs[0].chord == pytest.approx(c_root)
    assert wing.xsecs[1].chord == pytest.approx(c_mid)
    assert wing.xsecs[2].chord == pytest.approx(c_tip)
    # blend twist uses centre_body_fraction, exactly as vsp_builder
    mid_twist = params.root_twist_deg + (params.tip_twist_deg - params.root_twist_deg) * params.centre_body_fraction
    assert wing.xsecs[1].twist == pytest.approx(mid_twist)
    # reference geometry taken from Astra's analytic planform
    assert ap.s_ref == pytest.approx(params.planform_area_m2())
    assert ap.b_ref == pytest.approx(params.span_m)


def test_evaluate_is_physical(params, derived):
    r = asb_aero.evaluate(params, derived)
    assert 5.0 < r.l_over_d < 60.0          # sane subsonic L/D band
    assert r.cl == pytest.approx(derived.required_cl, rel=0.05)  # trims at cruise CL
    assert r.trim_in_sweep
    assert r.cd_induced > 0 and r.cd_profile > 0                 # viscous split present
    assert r.cd == pytest.approx(r.cd_induced + r.cd_profile, rel=0.15)
    assert r.x_np_m > r.x_cg_m               # pitch-stable swept planform
    assert r.static_margin > 0
    assert r.cm_alpha_per_rad < 0            # stable pitch slope


def test_evaluate_is_deterministic(params, derived):
    a = asb_aero.evaluate(params, derived)
    b = asb_aero.evaluate(params, derived)
    assert a.l_over_d == pytest.approx(b.l_over_d, rel=1e-9)
    assert a.x_np_m == pytest.approx(b.x_np_m, rel=1e-9)
    assert a.cn_beta_per_rad == pytest.approx(b.cn_beta_per_rad, rel=1e-9)


def test_more_outer_sweep_moves_np_aft(params, derived):
    """Sign check matching the analytic sensitivity table: more LE sweep -> neutral
    point moves aft (more pitch stability)."""
    swept = BWBParams.model_validate({**params.model_dump(),
                                      "sweep_outer_deg": params.sweep_outer_deg + 8})
    base = asb_aero.evaluate(params, derived)
    more = asb_aero.evaluate(swept, derived)
    assert more.x_np_m > base.x_np_m
