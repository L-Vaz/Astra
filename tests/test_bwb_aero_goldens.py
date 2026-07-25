"""Golden values and invariants for the equation-tier physics core (S0-7, S0-8, S0-9).

`domain_packs/bwb/aero_estimates.py` produces every L/D, CD0, Oswald e, CLmax, stall
speed and neutral point the user ever sees. It runs on every request and reaches ~99%
line coverage through other tests — yet before this module NOTHING asserted a single
value it returns. Coverage measured execution, not correctness.

Demonstration of the gap: changing the laminar-run fraction from 0.30 to 0.20 moves
cruise L/D from 17.0037 to 16.3299 (-4.0%) — a different number on every screen in the
product — and the whole suite still passed green.

Three layers here, each with a different job:

1. GOLDENS (S0-7/S0-8) — exact expected outputs. These are NOT claims that the physics
   is right; several known biases are documented in
   docs/superpowers/specs/2026-07-25-stage1-limitations-and-hardening-design.md. They
   are a tripwire: any edit that moves a number must move a golden too, deliberately,
   with a ledger entry. Regenerate with the snippet in `_regenerate()` below.
2. INVARIANTS (S0-9) — relationships that must hold for ANY legal input. A solver fuzz
   over 117,600 flight cases found zero violations of these, so they encode behaviour
   the code already gets right and must not lose.
3. KNOWN DEFECTS — asserted as `xfail(strict=True)` so a fix flips the marker.

Pure `domain_packs` — runs on any interpreter, no WSL, no OpenVSP.
"""
import math

import pytest

from domain_packs.bwb.aero_estimates import (CL_MAX_2D, CL_MAX_3D_FACTOR,
                                             INTERFERENCE_MARGIN,
                                             LAMINAR_FRACTION,
                                             LAUNCH_VSTALL_MAX,
                                             STATIC_MARGIN_TARGET,
                                             cl_max_3d,
                                             effective_c4_sweep_deg,
                                             effective_le_sweep_deg, estimate,
                                             form_factor, mean_tc,
                                             neutral_point_m,
                                             parasite_drag_coeff,
                                             oswald_efficiency,
                                             skin_friction_coeff)
from domain_packs.bwb.atmosphere import atmosphere_at
from domain_packs.bwb.schema import BWBParams, MissionDerived

# --- the reference case every golden below is measured at ---------------------------
REFERENCE_PARAMS = dict(
    span_m=1.6, root_chord_m=0.55, centre_body_fraction=0.28, mid_chord_frac=0.6,
    taper_outer=0.4, sweep_inner_deg=35, sweep_outer_deg=18, root_thickness_pct=14,
    tip_thickness_pct=10, root_twist_deg=1.0, tip_twist_deg=-3.0, dihedral_deg=3.0,
)
REFERENCE_ALTITUDE_M = 0.0
REFERENCE_SPEED_MS = 18.0
REFERENCE_WEIGHT_N = 3.5 * 9.81
REFERENCE_LAUNCH = "hand"

# Exact floats emitted by the live code. rel=1e-12 keeps this a tripwire for real
# changes rather than a platform-precision lottery.
GOLDEN_PANEL = {
    "s_ref_m2": 0.4632320000000001,
    "aspect_ratio": 5.5263885051119095,
    "span_m": 1.6,
    "mac_m": 0.33196149949341447,
    "wing_loading_kgm2": 7.555609284332687,
    "washout_deg": 4.0,
    "v_ms": 18.0,
    "rho_kgm3": 1.224999155888,
    "weight_n": 34.335,
    "reynolds_mac": 409065.98276939226,
    "cd0": 0.01215973103075618,
    "oswald_e": 0.8194042063729294,
    "cl_cruise": 0.37349749636505963,
    "cd_cruise": 0.021965596161195606,
    "ld_cruise": 17.003749573839468,
    "cl_star": 0.4159170474696608,
    "ld_max": 17.10223056816233,
    "endurance_fom": 10.391746591388154,
    "cl_max_3d": 1.1208492861603039,
    "v_stall_ms": 10.390650635946146,
    "launch_limit_ms": 10.5,
    "x_np_m": 0.24976166981040424,
    "x_cg_for_target_sm_m": 0.22320474985093108,
    "root_bending_index_nm": 6.867000000000001,
    "tip_re": 162659.55482175123,
    "roll_damping_index": 2.5600000000000005,
}

GOLDEN_GEOMETRY = {
    "mean_tc": 0.11851063829787235,
    "form_factor_mean_tc": 1.2488566127991974,
    "effective_le_sweep_deg": 22.76,
    "effective_c4_sweep_deg": 16.666400854229398,
    "cl_max_3d": 1.1208492861603039,
    "neutral_point_m": 0.24976166981040424,
}

GOLDEN_CF = {
    0.0: 0.012892296409079974,       # the max(re, 1e4) floor launders invalid input
    -5.0: 0.012892296409079974,      # ... including a NEGATIVE Reynolds number
    1e3: 0.012892296409079974,
    1e4: 0.012892296409079974,
    1e5: 0.006269033583206196,
    5e5: 0.004137424748583963,
    1e6: 0.003527930659928086,
    5e6: 0.0025332664183959908,
}

GOLDEN_FORM_FACTOR = {
    0.05: 1.100375,
    0.10: 1.206,
    0.14: 1.3030496,
    0.20: 1.496,
}

# Includes the CLAMP cells deliberately: 23% of the legal (AR x sweep) space is pinned
# at 0.50 or 0.95, where e has zero gradient in both AR and sweep. Pinning them means
# the clamped fraction cannot grow unnoticed.
GOLDEN_OSWALD = {
    (2.5, 0.0): 0.95, (2.5, 30.0): 0.95, (2.5, 45.0): 0.9092395090605572,
    (2.5, 60.0): 0.7061399389301677,
    (3.0, 0.0): 0.95, (3.0, 30.0): 0.95, (3.0, 60.0): 0.6601243685965343,
    (5.0, 0.0): 0.9007058293690858, (5.0, 15.0): 0.8851322056231066,
    (5.0, 30.0): 0.805082771890826, (5.0, 45.0): 0.688117130642075,
    (5.0, 60.0): 0.5,
    (8.0, 0.0): 0.8105923299393963, (8.0, 30.0): 0.5766805244080753,
    (8.0, 45.0): 0.5,
    (10.0, 0.0): 0.7566172960495666, (10.0, 15.0): 0.6274691585687779,
    (10.0, 30.0): 0.5,
}

GOLDEN_CD0_VS_RE = {
    2e5: 0.014559180449339992,
    5e5: 0.011587048636979973,
    1e6: 0.009880132359741459,
    2e6: 0.008513445410127012,
}


@pytest.fixture(scope="module")
def reference_params() -> BWBParams:
    return BWBParams(**REFERENCE_PARAMS)


@pytest.fixture(scope="module")
def reference_panel(reference_params):
    return estimate(reference_params, atmosphere_at(REFERENCE_ALTITUDE_M),
                    REFERENCE_SPEED_MS, REFERENCE_WEIGHT_N, REFERENCE_LAUNCH)


# ===================================================================================
# 1. Goldens
# ===================================================================================

@pytest.mark.parametrize("field", sorted(GOLDEN_PANEL))
def test_golden_panel_field(reference_panel, field):
    """Every numeric field of the reference panel, pinned exactly.

    A failure here is not necessarily a bug — it means a number the user sees changed.
    Confirm it was intended, update the golden, and record why in the limitations
    ledger.
    """
    assert getattr(reference_panel, field) == pytest.approx(
        GOLDEN_PANEL[field], rel=1e-12)


def test_golden_panel_launch_ok(reference_panel):
    assert reference_panel.launch_ok is True


def test_golden_panel_covers_every_float_field(reference_panel):
    """If a float field is added to AeroEstimate, it must be pinned too — otherwise
    the golden layer silently stops covering the panel."""
    floats = {k for k, v in reference_panel.model_dump().items()
              if isinstance(v, float)}
    assert floats - set(GOLDEN_PANEL) == set(), (
        "new float field(s) on AeroEstimate are not pinned in GOLDEN_PANEL")


@pytest.mark.parametrize("name", sorted(GOLDEN_GEOMETRY))
def test_golden_geometry_component(reference_params, name):
    fns = {
        "mean_tc": lambda p: mean_tc(p),
        "form_factor_mean_tc": lambda p: form_factor(mean_tc(p)),
        "effective_le_sweep_deg": effective_le_sweep_deg,
        "effective_c4_sweep_deg": effective_c4_sweep_deg,
        "cl_max_3d": cl_max_3d,
        "neutral_point_m": neutral_point_m,
    }
    assert fns[name](reference_params) == pytest.approx(
        GOLDEN_GEOMETRY[name], rel=1e-12)


@pytest.mark.parametrize("re", sorted(GOLDEN_CF))
def test_golden_skin_friction(re):
    assert skin_friction_coeff(re) == pytest.approx(GOLDEN_CF[re], rel=1e-12)


@pytest.mark.parametrize("tc", sorted(GOLDEN_FORM_FACTOR))
def test_golden_form_factor(tc):
    assert form_factor(tc) == pytest.approx(GOLDEN_FORM_FACTOR[tc], rel=1e-12)


@pytest.mark.parametrize("key", sorted(GOLDEN_OSWALD))
def test_golden_oswald_efficiency(key):
    ar, sweep = key
    assert oswald_efficiency(ar, sweep) == pytest.approx(
        GOLDEN_OSWALD[key], rel=1e-12)


@pytest.mark.parametrize("re", sorted(GOLDEN_CD0_VS_RE))
def test_golden_cd0_vs_reynolds(reference_params, re):
    assert parasite_drag_coeff(reference_params, re) == pytest.approx(
        GOLDEN_CD0_VS_RE[re], rel=1e-12)


def test_declared_constants_are_pinned():
    """These are global levers: one edit rescales every drag and L/D in the product.
    Changing one must be a deliberate, reviewed act, not a drive-by.
    """
    assert LAMINAR_FRACTION == 0.30
    assert INTERFERENCE_MARGIN == 1.10
    assert CL_MAX_2D == 1.30
    assert CL_MAX_3D_FACTOR == 0.90
    assert STATIC_MARGIN_TARGET == 0.08
    assert LAUNCH_VSTALL_MAX == {"hand": 10.5, "catapult": 13.0, "runway": 16.0}


def test_static_margin_target_is_not_duplicated_inconsistently():
    """STATIC_MARGIN_TARGET is declared twice — here and as
    MissionDerived.static_margin_target. Equal today, independently declared, free to
    drift. Pin them together until one is removed.
    """
    assert (MissionDerived.model_fields["static_margin_target"].default
            == STATIC_MARGIN_TARGET)


# ===================================================================================
# 2. Invariants — zero violations across 117,600 fuzzed flight cases; keep it that way
# ===================================================================================

_ENVELOPE = [
    (alt, v, mass)
    for alt in (0.0, 1000.0, 4000.0)
    for v in (8.0, 18.0, 40.0, 60.0)
    for mass in (0.3, 2.0, 10.0, 25.0)
]


@pytest.mark.parametrize("alt,v,mass", _ENVELOPE)
def test_invariants_hold_across_the_envelope(reference_params, alt, v, mass):
    e = estimate(reference_params, atmosphere_at(alt), v, mass * 9.81, "hand")

    # every output is finite — no NaN/inf leaks into a user-facing number
    for field, value in e.model_dump().items():
        if isinstance(value, float):
            assert math.isfinite(value), f"{field} is not finite"

    # a parabolic polar cannot beat its own best point
    assert e.ld_cruise <= e.ld_max + 1e-9

    # the best-L/D lift coefficient must be reachable before stall
    assert e.cl_star <= e.cl_max_3d + 1e-9

    # field definitions, locked against refactor drift
    assert e.endurance_fom == pytest.approx(
        e.cl_cruise ** 1.5 / e.cd_cruise, rel=1e-9)
    assert e.x_cg_for_target_sm_m == pytest.approx(
        e.x_np_m - STATIC_MARGIN_TARGET * e.mac_m, abs=1e-12)

    # the neutral point sits aft of the apex, and so does the CG it implies
    assert e.x_np_m > 0.0
    assert e.x_cg_for_target_sm_m > 0.0


def test_neutral_point_moves_aft_with_more_sweep(reference_params):
    """Physics direction, not a magnitude: sweep must move the aerodynamic centre
    aft. Verified strictly monotonic over the legal range."""
    previous = None
    for outer in (5.0, 12.0, 20.0, 28.0, 36.0):
        p = reference_params.model_copy(update={"sweep_outer_deg": outer})
        x_np = neutral_point_m(p)
        if previous is not None:
            assert x_np > previous, f"x_np did not move aft at sweep {outer}"
        previous = x_np


def test_higher_aspect_ratio_lowers_induced_drag(reference_params):
    """k = 1/(pi e AR): more span efficiency per unit lift as AR rises."""
    low = estimate(reference_params.model_copy(update={"span_m": 1.2}),
                   atmosphere_at(0.0), 18.0, REFERENCE_WEIGHT_N, "hand")
    high = estimate(reference_params.model_copy(update={"span_m": 2.4}),
                    atmosphere_at(0.0), 18.0, REFERENCE_WEIGHT_N, "hand")
    assert high.aspect_ratio > low.aspect_ratio
    k_low = 1.0 / (math.pi * low.oswald_e * low.aspect_ratio)
    k_high = 1.0 / (math.pi * high.oswald_e * high.aspect_ratio)
    assert k_high < k_low


def test_oswald_blend_is_continuous_at_its_breakpoints():
    """The 0-30 degree blend between Raymer's straight- and swept-wing fits must not
    JUMP at either end.

    Tolerance note: stepping 1e-9 degrees legitimately moves e by ~1e-11 (the local
    slope is ~5e-3 per degree), so the test cannot demand bitwise equality. A real
    discontinuity here would be a step between the two fits — order 0.05-0.1 — so 1e-6
    sits four orders below anything meaningful while staying immune to float noise.
    """
    for ar in (2.5, 5.0, 8.0, 10.0):
        assert oswald_efficiency(ar, 0.0) == pytest.approx(
            oswald_efficiency(ar, 1e-9), abs=1e-6)
        assert oswald_efficiency(ar, 30.0) == pytest.approx(
            oswald_efficiency(ar, 30.0 - 1e-9), abs=1e-6)


# ===================================================================================
# 3. Known defects — each xfail flips when its plan item lands
# ===================================================================================

@pytest.mark.xfail(strict=True,
                   reason="S6-F4: effective_le_sweep_deg averages ANGLES while "
                          "effective_c4_sweep_deg averages TANGENTS. By Jensen's "
                          "inequality the quarter-chord sweep exceeds the leading-edge "
                          "sweep in 8.5% of legal geometries — geometrically impossible "
                          "when both panels taper aft, which the schema guarantees.")
def test_quarter_chord_sweep_never_exceeds_leading_edge_sweep():
    p = BWBParams(span_m=1.8, root_chord_m=0.25, centre_body_fraction=0.45,
                  mid_chord_frac=0.85, taper_outer=0.7, sweep_inner_deg=60,
                  sweep_outer_deg=40, root_thickness_pct=14, tip_thickness_pct=9,
                  root_twist_deg=2.0, tip_twist_deg=-3.0, dihedral_deg=2.0)
    assert effective_c4_sweep_deg(p) <= effective_le_sweep_deg(p) + 1e-9


@pytest.mark.xfail(strict=True,
                   reason="S6-F5: CL_MAX_2D is hardcoded at Re~7e5, so cl_max_3d is "
                          "bit-identical across Re 1.8e5-1.0e6. Section CLmax is ~0.9 "
                          "at Re 1.5e5 and ~1.45 at 1e6, so v_stall and the entire "
                          "launch gate are blind to Reynolds — the dominant low-Re effect.")
def test_clmax_responds_to_reynolds_number(reference_params):
    slow = estimate(reference_params, atmosphere_at(0.0), 8.0, REFERENCE_WEIGHT_N, "hand")
    fast = estimate(reference_params, atmosphere_at(0.0), 45.0, REFERENCE_WEIGHT_N, "hand")
    assert fast.reynolds_mac > 4 * slow.reynolds_mac       # the premise holds
    assert fast.cl_max_3d != slow.cl_max_3d                # the response does not


@pytest.mark.xfail(strict=True,
                   reason="S6-F1: cl_cruise > cl_max_3d in 22.31% of the envelope with "
                          "no feasibility flag. AeroEstimate has no cruise_feasible / "
                          "stall_margin field, and variants.py ranks on ld_cruise, so "
                          "infeasible points compete against feasible ones as equals.")
def test_cruise_beyond_stall_is_flagged(reference_params):
    e = estimate(reference_params, atmosphere_at(0.0), 8.0, 8.0 * 9.81, "hand")
    assert e.cl_cruise > e.cl_max_3d, "premise: this case cruises beyond stall"
    assert getattr(e, "cruise_feasible", None) is False


@pytest.mark.xfail(strict=True,
                   reason="S6-F9: the flight condition is unvalidated. v_ms=-5 returns "
                          "a plausible-looking panel with reynolds_mac=-34348 because "
                          "the max(re, 1e4) floor launders it; NaN propagates silently.")
def test_non_physical_flight_conditions_are_rejected(reference_params):
    with pytest.raises((ValueError, AssertionError)):
        estimate(reference_params, atmosphere_at(0.0), -5.0, REFERENCE_WEIGHT_N, "hand")


@pytest.mark.xfail(strict=True,
                   reason="S6-F10: LAUNCH_VSTALL_MAX.get(launch) returns None for an "
                          "unknown method, so launch_ok becomes None and the gate "
                          "silently self-disables instead of erroring.")
def test_unknown_launch_method_does_not_disable_the_gate(reference_params):
    with pytest.raises((ValueError, KeyError)):
        estimate(reference_params, atmosphere_at(0.0), REFERENCE_SPEED_MS,
                 REFERENCE_WEIGHT_N, "glider")


@pytest.mark.xfail(strict=True,
                   reason="S3-F7: the field description and the user-facing assumptions "
                          "string both say 'elliptic half-span load', but the formula "
                          "W*b/8 is the UNIFORM-load result (elliptic is W*b/(3*pi)) — "
                          "17.8% apart.")
def test_root_bending_index_matches_its_elliptic_label(reference_panel):
    elliptic = REFERENCE_WEIGHT_N * REFERENCE_PARAMS["span_m"] / (3.0 * math.pi)
    assert reference_panel.root_bending_index_nm == pytest.approx(elliptic, rel=1e-9)


def _regenerate():  # pragma: no cover - developer helper, never run by the suite
    """Print fresh goldens after an INTENDED physics change.

        python -c "import tests.test_bwb_aero_goldens as g; g._regenerate()"

    Paste the output over the dicts above, and record the reason in the limitations
    ledger. Never regenerate to make a red suite green without that step.
    """
    panel = estimate(BWBParams(**REFERENCE_PARAMS),
                     atmosphere_at(REFERENCE_ALTITUDE_M), REFERENCE_SPEED_MS,
                     REFERENCE_WEIGHT_N, REFERENCE_LAUNCH)
    print("GOLDEN_PANEL = {")
    for key, value in panel.model_dump().items():
        if isinstance(value, float):
            print(f"    {key!r}: {value!r},")
    print("}")
