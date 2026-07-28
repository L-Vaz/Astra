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
   with a ledger entry. The values live in `tests/data/aero_golden.json`; regenerate
   only via `python verification/verify_aero_goldens.py --update`, never by hand.
2. INVARIANTS (S0-9) — relationships that must hold for ANY legal input. A solver fuzz
   over 117,600 flight cases found zero violations of these, so they encode behaviour
   the code already gets right and must not lose.
3. KNOWN DEFECTS — asserted as `xfail(strict=True)` so a fix flips the marker.

Pure `domain_packs` — runs on any interpreter, no WSL, no OpenVSP.
"""
import json
import math
from pathlib import Path

import pytest

from domain_packs.bwb.aero_estimates import (INTERFERENCE_MARGIN,
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

# --- the committed golden file ------------------------------------------------------
# One file, two readers: this module asserts it, and verification/verify_aero_goldens.py
# both asserts it (as a printed table) and is the only thing allowed to rewrite it
# (`--update`, which the golden policy binds to a limitations-ledger entry). Keeping the
# values out of the test module is what makes "regenerate" a reviewable, single-command
# act rather than a hand-edit of expected numbers.
GOLDEN_PATH = Path(__file__).parent / "data" / "aero_golden.json"
if not GOLDEN_PATH.exists():  # pragma: no cover - environment error, not a test outcome
    raise RuntimeError(
        f"missing golden file {GOLDEN_PATH}; "
        f"create it with `python verification/verify_aero_goldens.py --update`")
GOLDEN = json.loads(GOLDEN_PATH.read_text())

# The reference case travels WITH the values, so a golden can never be compared against
# a case it was not measured at.
REFERENCE_PARAMS = GOLDEN["reference"]["params"]
REFERENCE_ALTITUDE_M = GOLDEN["reference"]["altitude_m"]
REFERENCE_SPEED_MS = GOLDEN["reference"]["speed_ms"]
REFERENCE_WEIGHT_N = GOLDEN["reference"]["weight_n"]
# S1-C2: CLmax depends on Reynolds, so the geometry golden must pin the Reynolds it
# was measured at. This is the reference panel's own cruise MAC value.
REFERENCE_RE_MAC = GOLDEN["panel"]["reynolds_mac"]
REFERENCE_LAUNCH = GOLDEN["reference"]["launch"]

GOLDEN_PANEL = GOLDEN["panel"]
GOLDEN_GEOMETRY = GOLDEN["geometry"]
# Stored as [key..., value] pairs rather than JSON objects: object keys are strings, and
# round-tripping a float through str() is exactly the silent precision loss a golden
# file must not have.
GOLDEN_CF = GOLDEN["skin_friction_coeff"]
GOLDEN_FORM_FACTOR = GOLDEN["form_factor"]
GOLDEN_OSWALD = GOLDEN["oswald_efficiency"]
GOLDEN_CD0_VS_RE = GOLDEN["cd0_vs_reynolds"]
GOLDEN_SWEEP_CASE = GOLDEN["sweep_case"]


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
    assert reference_panel.launch_ok is GOLDEN["panel_launch_ok"]


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
        # S1-C2: CLmax takes a Reynolds number now, pinned at the reference panel's
        # cruise value so this golden stays a fixed-condition probe.
        "cl_max_3d_at_cruise_re": lambda p: cl_max_3d(p, REFERENCE_RE_MAC),
        "stall_station_frac": lambda p: estimate(
            p, atmosphere_at(0.0), REFERENCE_SPEED_MS, REFERENCE_WEIGHT_N,
            "hand").stall_station_frac,
        "neutral_point_m": neutral_point_m,
    }
    assert fns[name](reference_params) == pytest.approx(
        GOLDEN_GEOMETRY[name], rel=1e-12)


@pytest.mark.parametrize("re,expected", GOLDEN_CF, ids=repr)
def test_golden_skin_friction(re, expected):
    assert skin_friction_coeff(re) == pytest.approx(expected, rel=1e-12)


@pytest.mark.parametrize("re", [0.0, -5.0, float("nan"), float("inf")], ids=repr)
def test_invalid_reynolds_raises(re):
    """These used to return exactly 0.012892296409079974 — the floor's value — so an
    impossible Reynolds number produced a plausible Cf (S6-F2c)."""
    with pytest.raises(ValueError, match="Reynolds"):
        skin_friction_coeff(re)


def test_cf_below_the_floor_is_pinned_and_documented():
    """0 < Re < 1e4 is out of range for both correlations; the floor's value is
    returned and the constant is public so a caller can see it happened."""
    from domain_packs.bwb.aero_estimates import RE_FLOOR

    assert skin_friction_coeff(1e3) == pytest.approx(skin_friction_coeff(RE_FLOOR),
                                                     rel=1e-12)
    assert RE_FLOOR == 1e4


@pytest.mark.parametrize("tc,expected", GOLDEN_FORM_FACTOR, ids=repr)
def test_golden_form_factor(tc, expected):
    assert form_factor(tc) == pytest.approx(expected, rel=1e-12)


@pytest.mark.parametrize("ar,sweep,expected", GOLDEN_OSWALD, ids=repr)
def test_golden_oswald_efficiency(ar, sweep, expected):
    """(AR x LE-sweep) grid including the clamp plateaus, where e has zero gradient in
    both arguments — so the clamped fraction cannot grow unnoticed."""
    assert oswald_efficiency(ar, sweep) == pytest.approx(expected, rel=1e-12)


@pytest.mark.parametrize("re,expected", GOLDEN_CD0_VS_RE, ids=repr)
def test_golden_cd0_vs_reynolds(reference_params, re, expected):
    assert parasite_drag_coeff(reference_params, re) == pytest.approx(
        expected, rel=1e-12)


@pytest.mark.parametrize("name", ["effective_le_sweep_deg", "effective_c4_sweep_deg"])
def test_golden_sweep_case(name):
    """Both effective sweeps on the worked case where the quarter-chord sweep exceeds
    the leading-edge sweep — the geometry behind the S6-F4 xfail. Pinned so the Stage-6
    fix has to move these two numbers deliberately."""
    fns = {"effective_le_sweep_deg": effective_le_sweep_deg,
           "effective_c4_sweep_deg": effective_c4_sweep_deg}
    params = BWBParams(**GOLDEN_SWEEP_CASE["params"])
    assert fns[name](params) == pytest.approx(GOLDEN_SWEEP_CASE[name], rel=1e-12)


def test_declared_constants_are_pinned():
    """These are global levers: one edit rescales every drag and L/D in the product.
    Changing one must be a deliberate, reviewed act, not a drive-by.
    """
    assert LAMINAR_FRACTION == 0.30
    assert INTERFERENCE_MARGIN == 1.10
    # S1-C2: CL_MAX_2D (1.30) and CL_MAX_3D_FACTOR (0.90) are no longer pinned here
    # because they no longer exist. Both were unsourced global levers; CLmax is now
    # solved from the spanwise load instead of assumed. Their absence is asserted
    # below so they cannot quietly return.
    assert STATIC_MARGIN_TARGET == 0.08
    assert LAUNCH_VSTALL_MAX == {"hand": 10.5, "catapult": 13.0, "runway": 16.0}


def test_the_unsourced_clmax_constants_stay_deleted():
    """S1-C2 removed `CL_MAX_2D = 1.30` (wrong section family at 3.5x the wrong
    Reynolds) and `CL_MAX_3D_FACTOR = 0.90` (an unsourced allowance for the wing not
    stalling everywhere at once). Both are now computed — the first per station from
    the section screen, the second as the result of finding where stall begins.

    Reintroducing either would mean a guessed number is back in the stall chain, which
    sets wing area, which sets everything."""
    from domain_packs.bwb import aero_estimates as A
    for gone in ("CL_MAX_2D", "CL_MAX_3D_FACTOR"):
        assert not hasattr(A, gone), (
            f"aero_estimates.{gone} is back — CLmax is being assumed again instead of "
            f"solved from the spanwise load")


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
    assert e.power_fom == pytest.approx(
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

def test_quarter_chord_sweep_never_exceeds_leading_edge_sweep():
    """S6-F4, landed: both sweeps average in tangent space now. Averaging one in angle
    space and the other in tangent space made c/4 exceed LE in 8.5% of legal
    geometries — impossible when both panels taper aft."""
    p = BWBParams(**GOLDEN_SWEEP_CASE["params"])
    assert effective_c4_sweep_deg(p) <= effective_le_sweep_deg(p) + 1e-9


def test_clmax_responds_to_reynolds_number(reference_params):
    """S6-F5 / S7-CAP-4, RETIRED by S1-C2. This was an xfail backlog entry: CL_MAX_2D
    was hardcoded at Re~7e5, so cl_max_3d came back bit-identical across Re 1.8e5-1.0e6
    while real section CLmax runs ~0.9 at Re 1.5e5 and ~1.45 at 1e6 — v_stall and the
    whole launch gate were blind to the dominant low-Re effect.

    Capability now comes per station from the committed NeuralFoil screen at that
    station's own Reynolds, so the response exists. Kept as a live regression guard.

    THE PROBE HAD TO CHANGE, and the reason is itself a result. CLmax is solved at the
    speed the wing actually STALLS at, not at cruise — so cruising faster cannot change
    how much lift the wing can make, and the old cruise-speed probe now correctly
    measures nothing. Weight is the lever: a heavier aircraft stalls faster, which
    raises the stall Reynolds, which raises CLmax."""
    light = estimate(reference_params, atmosphere_at(0.0), 18.0, 10.0, "hand")
    heavy = estimate(reference_params, atmosphere_at(0.0), 18.0, 120.0, "hand")
    assert heavy.v_stall_ms > 2 * light.v_stall_ms          # the premise holds
    assert heavy.cl_max_3d > light.cl_max_3d, (
        "CLmax must RISE with Reynolds — the boundary layer separates later")


def test_clmax_does_not_depend_on_cruise_speed(reference_params):
    """The other half of the same result. Stall is a property of the wing, its weight
    and the air — not of how fast you happen to be cruising. Before S1-C2 this held for
    the wrong reason (CLmax was a constant); it now holds for the right one, and that
    is worth pinning so a future edit cannot make cruise speed leak into stall."""
    slow = estimate(reference_params, atmosphere_at(0.0), 8.0, REFERENCE_WEIGHT_N, "hand")
    fast = estimate(reference_params, atmosphere_at(0.0), 45.0, REFERENCE_WEIGHT_N, "hand")
    assert fast.reynolds_mac > 4 * slow.reynolds_mac        # the premise holds
    assert fast.cl_max_3d == pytest.approx(slow.cl_max_3d, rel=1e-12)
    assert fast.v_stall_ms == pytest.approx(slow.v_stall_ms, rel=1e-12)


def test_cruise_beyond_stall_is_flagged(reference_params):
    """S6-F1, landed: 22.31% of the envelope cruised below its own stall speed with
    launch_ok still True and nothing on the panel saying so."""
    e = estimate(reference_params, atmosphere_at(0.0), 8.0, 8.0 * 9.81, "hand")
    assert e.cl_cruise > e.cl_max_3d, "premise: this case cruises beyond stall"
    assert e.cruise_feasible is False
    assert e.stall_margin < 0


def test_a_feasible_cruise_reports_its_margin(reference_params, reference_panel):
    assert reference_panel.cruise_feasible is True
    assert reference_panel.stall_margin == pytest.approx(
        (reference_panel.v_ms - reference_panel.v_stall_ms)
        / reference_panel.v_stall_ms, rel=1e-12)


@pytest.mark.parametrize("v_ms", [0.0, -5.0, float("nan"), float("inf")], ids=repr)
def test_non_physical_speeds_are_rejected(reference_params, v_ms):
    """S6-F9, landed. v_ms=-5 used to return a plausible panel with
    reynolds_mac=-34348, laundered by the Reynolds floor."""
    with pytest.raises(ValueError, match="cruise speed"):
        estimate(reference_params, atmosphere_at(0.0), v_ms, REFERENCE_WEIGHT_N, "hand")


@pytest.mark.parametrize("weight_n", [0.0, -10.0, float("nan"), float("inf")],
                         ids=repr)
def test_non_physical_weights_are_rejected(reference_params, weight_n):
    with pytest.raises(ValueError, match="weight"):
        estimate(reference_params, atmosphere_at(0.0), REFERENCE_SPEED_MS, weight_n,
                 "hand")


def test_unknown_launch_method_does_not_disable_the_gate(reference_params):
    """S6-F10, landed: it returned limit=None, ok=None beside a stall speed that would
    have failed all three real limits."""
    with pytest.raises(ValueError, match="launch method"):
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


def test_golden_file_is_regenerable_by_the_verification_script():
    """The golden file has exactly one writer, and it must still be there.

    After an INTENDED physics change:

        python verification/verify_aero_goldens.py --update

    and record the physical reason and its source in the limitations ledger. Never
    regenerate to turn a red suite green without that step.
    """
    script = Path(__file__).parent.parent / "verification" / "verify_aero_goldens.py"
    assert script.exists(), "the only sanctioned way to update the goldens is missing"
    assert "--update" in script.read_text()
