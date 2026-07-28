"""BWB domain pack — equation-level aerodynamic estimates (tier 0).

Classical conceptual-design methods (Raymer, Hoerner, Anderson): wetted-area
parasite-drag buildup, Oswald span efficiency, stall speed, planform neutral
point, and structural/handling proxies. Everything is closed-form, runs in
microseconds, and exists so candidate designs can be *compared and ranked
before any solver runs*.

These are textbook estimates, not predictions of record: VSPAERO replaces the
induced-drag and neutral-point numbers (tier 1), OpenFOAM replaces everything
(tier 2). Every constant is declared in the returned assumptions list.

Pydantic-only; safe to import from both the FoamAgent conda env (py3.12) and
the OpenVSP venv (py3.10).
"""
import math
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from .atmosphere import AtmosphereState
from .constants import STATIC_MARGIN_TARGET as _STATIC_MARGIN_TARGET
from .constants import VSTALL_TOL_MS
from .schema import BWBParams

# --- declared engineering constants -----------------------------------------
#
# Each of these is either cited to a source or explicitly labelled engineering
# judgment, with the measured sensitivity of the numbers the user sees (S3-F12). They
# are global levers: one line changes every drag figure and every L/D in the product,
# and "it looked about right" is not something a reader can check.

# ENGINEERING JUDGMENT (Astra): fraction of the chord assumed to run laminar.
# A smooth small-UAV wing at Re 5e5-1e6 can hold laminar flow over roughly the first
# third of the chord, but the real value depends on surface finish, trip location and
# turbulence level — none of which this tier models.
# MEASURED SENSITIVITY (re-measured after S6-F2's transition-Re correction, which
# reduced this lever's leverage because the laminar segment is no longer understated):
# 0.30 -> 0.00 raises CD0 by 9.4% and drops ld_max by 4.4%; 0.30 -> 0.60 raises ld_max
# to 18.41 (from 16.14) on the reference geometry.
LAMINAR_FRACTION = 0.30

# ENGINEERING JUDGMENT (Astra): the ONLY allowance for prop, nacelle, gaps, camera,
# antennas and servo horns. Raymer tabulates interference separately per component;
# this collapses all of it into one factor. The pack's maximum CD0 is 0.0233 while
# measured small-UAV flying wings sit at 0.020-0.035, so this whole band is at the
# optimistic edge of the real range.
INTERFERENCE_MARGIN = 1.10

# S1-C2: CL_MAX_2D = 1.30 and CL_MAX_3D_FACTOR = 0.90 ARE GONE.
#
# `CL_MAX_2D` was one section value for every aircraft, cited to "NACA 4-series at
# Re ~ 7e5" — the wrong section family (conventionally cambered, i.e. what a TAILED
# aircraft uses) at roughly 3.5x the Reynolds number this project flies at. Capability
# now comes per station from the committed NeuralFoil screen.
#
# `CL_MAX_3D_FACTOR` was an unsourced allowance for the fact that a wing does not stall
# everywhere at once. That is no longer an allowance — `spanwise.solve_stall` computes
# WHERE the wing first stalls, so the gap the 0.90 stood in for is now the result rather
# than an input.

# ENGINEERING JUDGMENT (Astra): the highest stall speed each launch method tolerates,
# m/s. NOT derived from defaults.WING_LOADING — inverting these against that table
# implies CLmax 1.02 (hand) / 0.95 (catapult) / 0.81 (runway), three different values.
# They are three independent judgments. Notably the hand figure of 1.02 is close to
# what the Schrenk solve now returns for a real tailless section set, where the old
# constant chain gave an optimistic 1.17.
# Revisit against Sadraey §5 / Raymer §5 before treating them as calibrated.
# `test_bwb_disclosure.py::test_the_implied_clmax_values_disagree_with_the_packs_own_cap`
# keeps that inconsistency measured rather than remembered.
LAUNCH_VSTALL_MAX = {"hand": 10.5, "catapult": 13.0, "runway": 16.0}

# Declared once, in constants.py — schema.MissionDerived reads the same object, so the
# CG this places and the margin it reports cannot drift apart (S3-F12c).
STATIC_MARGIN_TARGET = _STATIC_MARGIN_TARGET


class AeroEstimate(BaseModel):
    """Equation-level metric panel for one geometry at one flight condition."""
    evidence: str = "equation"

    # geometry echoes
    s_ref_m2: float
    aspect_ratio: float
    span_m: float
    mac_m: float
    wing_loading_kgm2: float
    washout_deg: float = Field(description="root twist minus tip twist")

    # flight condition
    v_ms: float
    rho_kgm3: float
    weight_n: float
    reynolds_mac: float

    # drag polar
    cd0: float
    oswald_e: float
    cl_cruise: float
    cd_cruise: float
    ld_cruise: float
    cl_star: float = Field(description="CL of best L/D: sqrt(pi e AR CD0)")
    ld_max: float
    power_fom: float = Field(
        description="CL^1.5/CD at cruise. Level-flight power required scales as "
                    "1/this, so larger is better. A figure of merit for comparing "
                    "SHAPES — not a time, and not an endurance in minutes. Renamed "
                    "from endurance_fom in S1-A; the quantity is unchanged.")

    # stall / launch
    cl_max_3d: float
    v_stall_ms: float
    stall_station_frac: float = Field(
        default=0.0,
        description="Where stall begins, as a fraction of semi-span (0 = root, 1 = tip). "
                    "Computed from the spanwise load (S1-C2), not assumed.")
    stall_is_outboard: bool = Field(
        default=False,
        description="True when stall begins outboard. On a swept tailless wing that "
                    "moves the centre of lift forward and pitches the aircraft UP, "
                    "deeper into the stall, with no tail to recover with.")
    cruise_feasible: bool = True
    """False when the requested cruise speed is BELOW the stall speed (S6-F1).

    22.31% of the flight envelope was in this state with nothing reporting it — the
    same condition as cl_cruise > cl_max_3d, reaching cl_cruise 136.43 — while
    launch_ok still read True and ranking compared these points against real ones on
    ld_cruise as equals."""
    stall_margin: float = 0.0
    """(v_ms - v_stall_ms) / v_stall_ms. Negative means the cruise point is below
    stall; 0.2 means 20% above it."""
    launch_limit_ms: Optional[float] = None
    launch_ok: Optional[bool] = None

    # S1-E: profile drag from the measured section data, integrated across the span.
    # REPORTED, NOT USED IN THE POLAR — see domain_packs/bwb/section_drag.py.
    cd_profile_sections: float = Field(
        default=0.0,
        description="Strip-integrated section CD at the cruise CL and each station's "
                    "own Reynolds. Includes the VISCOUS lift-dependent part, so it is "
                    "not interchangeable with cd0 and must never be added to it.")
    cd0_sections_at_zero_lift: float = Field(
        default=0.0,
        description="The same integral at CL = 0 — the quantity comparable with cd0.")
    cd0_model_disagreement: float = Field(
        default=0.0,
        description="cd0 / cd0_sections_at_zero_lift. Measured 1.375 on the reference "
                    "wing: the wetted-area correlation reads 37% higher than the sum "
                    "of the measured section drags, and its own lumped interference "
                    "margin is only 1.10. Unresolved — reported, not reconciled.")
    cd_sections_span_clamped: float = Field(
        default=0.0,
        description="Fraction of the half-span whose local lift coefficient fell off "
                    "the stored CL ladder at the CRUISE CL and was clamped to its end.")

    # S1-G: the planform's INVISCID span efficiency, from Prandtl's lifting line.
    # REPORTED, NOT USED IN THE POLAR — see domain_packs/bwb/lifting_line.py for why.
    span_efficiency_planar: float = Field(
        default=0.0,
        description="e = 1/(1+delta) from the lifting-line Fourier solution: the "
                    "planform's induced drag against the elliptic minimum, with no "
                    "viscous or interference content. Monotone, unclamped, e <= 1 by "
                    "construction. NOT the Oswald e used in `cd_cruise`.")
    span_efficiency_delta: float = Field(
        default=0.0,
        description="delta = SUM n (A_n/A_1)^2. Zero for an elliptic wing.")
    oswald_e_span_gap: float = Field(
        default=0.0,
        description="span_efficiency_planar - oswald_e. A large positive gap is "
                    "EXPECTED, not a defect: Raymer's Oswald fit carries viscous "
                    "lift-dependent drag and interference that the inviscid solve "
                    "does not. Surfaced so the two are never confused.")

    oswald_e_clamped: Optional[str] = Field(
        default=None,
        description=("'floor' | 'ceiling' | None. When set, Oswald e is pinned at a "
                     "bound and has ZERO gradient in both AR and sweep, so no "
                     "consumer may treat it as a smooth function (S6-F3)."))

    # stability / control (planform estimate; VSPAERO refines x_np)
    x_np_m: float = Field(
        description=("Neutral point aft of the root LE apex. AREA-weighted planform "
                     "centroid: it ignores the higher lift-per-area of the outer "
                     "panels, which biases x_np FORWARD and the static margin "
                     "OPTIMISTIC. Treat the CG recommendation as a lower bound on "
                     "required nose-heaviness until VSPAERO confirms it (S6-F13)."))
    x_cg_for_target_sm_m: float

    # trim, washout-only (S1-D). See domain_packs/bwb/trim.py for the derivation.
    cm_trim_residual: float = Field(
        default=0.0,
        description="Pitching moment about x_cg_for_target_sm_m at the cruise CL, "
                    "nose-up positive. Zero means the wing flies itself with no "
                    "control input. A tailless aircraft has nothing else to trim with.")
    trims_as_built: bool = Field(
        default=True,
        description="|cm_trim_residual| within trim.CM_TRIM_TOLERANCE.")
    tip_twist_for_trim_deg: Optional[float] = Field(
        default=None,
        description="Tip twist that would zero the residual, holding everything else "
                    "fixed. None when twist has no pitch authority on this planform.")
    trim_twist_reachable: bool = Field(
        default=False,
        description="That twist is inside the schema's tip-twist range. False means "
                    "washout ALONE cannot trim this wing — the sweep, the CG or the "
                    "sections must change.")
    x_cg_for_trim_m: Optional[float] = Field(
        default=None,
        description="CG that trims the wing at cruise CL instead. Trim and static "
                    "margin are two demands on one CG position; this is the other one.")
    static_margin_at_trim: Optional[float] = Field(
        default=None,
        description="Static margin left over at x_cg_for_trim_m. Below the target "
                    "means balance was bought with stability; negative means the wing "
                    "can be balanced or stable but not both.")

    cg_placement_infeasible: bool = False
    """x_np fell outside (0, root_chord): 6.8% of the envelope, up to 2.48x the root
    chord. The CG this implies may have nowhere to sit on the airframe (S6-F13b)."""

    # lateral-directional (S1-H) — reported, never optimised. See dihedral.py.
    effective_dihedral_deg: float = Field(
        default=0.0,
        description="Total dihedral effect at the CRUISE CL, in degrees of equivalent "
                    "geometric dihedral: the wing's own dihedral plus the contribution "
                    "of sweep (McCormick p.547). Scales with CL, so it is a different "
                    "number at every speed.")
    effective_dihedral_from_sweep_deg: float = Field(
        default=0.0,
        description="The sweep half of the total. Negative when the half-chord line "
                    "sweeps forward, which this schema reaches.")
    effective_dihedral_at_clmax_deg: float = Field(
        default=0.0,
        description="The same total at CLmax — the slow end, where dihedral effect is "
                    "strongest and roll authority is weakest.")
    cl_beta_per_rad: float = Field(
        default=0.0,
        description="Roll due to sideslip at cruise, per radian. Negative is "
                    "stabilising. Superseded by the VLM value whenever one exists.")
    effective_c2_sweep_deg: float = Field(
        default=0.0,
        description="Half-chord sweep, tangent-averaged. The line McCormick's dihedral "
                    "increment is written at — NOT the quarter chord.")

    # proxies
    root_bending_index_nm: float = Field(
        description=("W*b/8 — uniform half-span load, a conservative comparative "
                     "index (NOT elliptic; the elliptic value is W*b/(3*pi), ~18% "
                     "lower). No structural sizing."))
    tip_re: float
    roll_damping_index: float = Field(description="relative, ~span^2")

    assumptions: List[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def output_consistency(self) -> "AeroEstimate":
        """A panel cannot be constructed in a state the physics forbids (S6-AE1).

        AtmosphereState in this same pack enforces finiteness, positivity and
        cross-consistency; this model accepted NaN, inf, oswald_e=99 and ld_max=-99.

        Scope is deliberately output-consistency, and deliberately EXCLUDES
        `cl_cruise <= cl_max_3d`: 22% of the legal envelope violates that, and it is a
        flag (cruise_feasible) rather than a rejection. The five cross-checks below are
        the S0-9 invariants, which held with zero violations over 117,600 fuzzed cases
        — asserting them here makes them structural rather than merely observed.
        """
        for name, value in self.model_dump().items():
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError(f"{name} is not finite ({value!r})")
        for name in ("s_ref_m2", "aspect_ratio", "span_m", "mac_m", "v_ms",
                     "rho_kgm3", "weight_n", "cd0", "cd_cruise", "cl_max_3d",
                     "v_stall_ms"):
            if getattr(self, name) <= 0.0:
                raise ValueError(f"{name} must be positive, got {getattr(self, name)}")
        if not 0.0 < self.oswald_e <= 1.0:
            raise ValueError(f"oswald_e must be in (0, 1], got {self.oswald_e}")
        if self.ld_cruise > self.ld_max + 1e-9:
            raise ValueError(
                f"ld_cruise {self.ld_cruise} exceeds ld_max {self.ld_max} — a "
                f"parabolic polar cannot beat its own best point")
        if self.x_np_m <= 0.0:
            raise ValueError(f"x_np_m must be aft of the apex, got {self.x_np_m}")
        return self


# ---------------------------------------------------------------------------
# component estimates
# ---------------------------------------------------------------------------

def mean_tc(params: BWBParams) -> float:
    """Area-weighted mean thickness ratio across the two panels."""
    c_root, c_mid, c_tip = params.chords()
    b1, b2 = params.half_widths()
    a1 = b1 * (c_root + c_mid) / 2.0
    a2 = b2 * (c_mid + c_tip) / 2.0
    t_root = params.root_thickness_pct / 100.0
    t_tip = params.tip_thickness_pct / 100.0
    # S1-C: the blend station has its OWN section now, so its thickness is read
    # rather than assumed to be the mean of root and tip. Before this the geometry
    # built by OpenVSP and the t/c used for form factor could disagree whenever the
    # mid section was not exactly halfway.
    t_mid = params.mid_thickness_pct / 100.0
    return (a1 * (t_root + t_mid) / 2.0 + a2 * (t_mid + t_tip) / 2.0) / (a1 + a2)


def effective_le_sweep_deg(params: BWBParams) -> float:
    """Half-width-weighted leading-edge sweep, averaged in TANGENT space.

    One convention for both sweeps (S6-F4). This function used to average ANGLES while
    effective_c4_sweep_deg averaged tangents, and by Jensen's inequality that made the
    quarter-chord sweep EXCEED the leading-edge sweep in 8.5% of legal geometries —
    geometrically impossible when both panels taper aft, which the schema guarantees.
    Worked case: span 1.8, cr 0.25, cbf 0.45, mcf 0.85, taper 0.7, sweeps 60/40 gave
    LE 49.000 and c/4 50.493, with both PER-PANEL values correctly below their LE
    sweep. One definition feeds Oswald e and the other feeds CLmax, so drag and stall
    were using contradictory conventions.

    Tangent averaging is the geometrically meaningful mean: it is the sweep of the line
    joining the panel leading edges over the same span.
    """
    b1, b2 = params.half_widths()
    t1 = math.tan(math.radians(params.sweep_inner_deg))
    t2 = math.tan(math.radians(params.sweep_outer_deg))
    return math.degrees(math.atan((b1 * t1 + b2 * t2) / (b1 + b2)))


def effective_c4_sweep_deg(params: BWBParams) -> float:
    """Half-width-weighted quarter-chord sweep (governs CLmax and lift slope).
    Per trapezoid: tan(Lc/4) = tan(LLE) - 0.25*(cr - ct)/half_width."""
    c_root, c_mid, c_tip = params.chords()
    b1, b2 = params.half_widths()
    t1 = math.tan(math.radians(params.sweep_inner_deg)) - 0.25 * (c_root - c_mid) / b1
    t2 = math.tan(math.radians(params.sweep_outer_deg)) - 0.25 * (c_mid - c_tip) / b2
    return math.degrees(math.atan((b1 * t1 + b2 * t2) / (b1 + b2)))


def effective_c2_sweep_deg(params: BWBParams) -> float:
    """Half-width-weighted HALF-CHORD sweep (S1-H).

    Same tangent-space average as the other two, for the same reason (S6-F4). Per
    trapezoid: tan(Lc/2) = tan(LLE) - 0.5*(cr - ct)/half_width — the 0.5 being the
    chord fraction, exactly as 0.25 gives the quarter chord.

    Exists because McCormick's sweep contribution to effective dihedral is written at
    the HALF chord specifically, and reusing the quarter-chord value would silently
    change the answer: on the reference wing the two differ by about 8 degrees, and the
    dihedral increment is proportional to tan(sweep)."""
    c_root, c_mid, c_tip = params.chords()
    b1, b2 = params.half_widths()
    t1 = math.tan(math.radians(params.sweep_inner_deg)) - 0.5 * (c_root - c_mid) / b1
    t2 = math.tan(math.radians(params.sweep_outer_deg)) - 0.5 * (c_mid - c_tip) / b2
    return math.degrees(math.atan((b1 * t1 + b2 * t2) / (b1 + b2)))


RE_FLOOR = 1e4
"""ENGINEERING JUDGMENT (Astra): lowest Reynolds number the correlations are applied at.

Both fits — Blasius laminar and Raymer eq. 12.27 turbulent — are calibrated well above
this, and a small-UAV tip chord can fall below it at low speed. Below the floor the value returned is the FLOOR's, not the input's,
which is a substitution and is now visible: it used to be applied silently, so Cf came
back at exactly 0.012892296409079974 for Re = 1000, 1.0, 0.0 and -5.0 alike, and that
value is 3.26x below true Blasius at Re 1e3 — non-conservative (S6-F2c)."""


def skin_friction_coeff(re: float, laminar_fraction: Optional[float] = None) -> float:
    """Mixed laminar/turbulent flat-plate Cf (area-fraction blend).

    The laminar segment is evaluated at the TRANSITION Reynolds number, not at the
    full-chord one (S6-F2a). Blasius Cf ~ 1/sqrt(Re), so evaluating the first 30% of
    the chord at the full-chord Re understated its friction by 1/sqrt(0.30) = 1.83x and
    made CD0 systematically optimistic: measured -12.4% at Re 2e5, -10.1% at 5e5,
    -8.5% at 1e6, -7.1% at 2e6. Every L/D in the product inherited that bias.

    The turbulent fit is Raymer eq. 12.27, NOT Schlichting as the comment used to say
    (Schlichting's own mean-Cf form is 17.6-19.3% lower) (S6-F2b).

    The default laminar fraction is resolved from the module constant at CALL time, not
    bound into the signature at import time: bound as a default argument,
    LAMINAR_FRACTION stopped being the lever its own comment claims it is (S3-F12).
    """
    if laminar_fraction is None:
        laminar_fraction = LAMINAR_FRACTION
    if not math.isfinite(re) or re <= 0.0:
        # The old max(re, 1e4) floor laundered these: Cf came back at exactly
        # 0.012892296409079974 for Re = 1000, 1.0, 0.0 and -5.0 alike.
        raise ValueError(f"Reynolds number must be finite and positive, got {re!r}")
    re = max(re, RE_FLOOR)
    cf_turb = 0.455 / (math.log10(re) ** 2.58)          # Raymer eq. 12.27
    # Blasius, at the transition point rather than at the trailing edge
    re_transition = max(laminar_fraction * re, 1.0)
    cf_lam = 1.328 / math.sqrt(re_transition)
    return laminar_fraction * cf_lam + (1.0 - laminar_fraction) * cf_turb


def form_factor(tc: float) -> float:
    """Hoerner wing form factor: 1 + 2(t/c) + 60(t/c)^4."""
    return 1.0 + 2.0 * tc + 60.0 * tc ** 4


OSWALD_FLOOR, OSWALD_CEILING = 0.5, 0.95
OSWALD_FIT_CROSSING_AR = 4.800
"""Raymer's straight- and swept-wing fits cross here. Below it the swept fit exceeds
the straight one, so e INCREASES with sweep (wrong sign) for AR 3.6-4.7, and the
unclamped fits give physically impossible e > 1 (1.123 at AR 2.5) which the ceiling
hides rather than flags. Documented, measured by test, and deliberately NOT patched:
replacing the fits would move every L/D for a model that is not obviously better
(S6-F3, deferred as a sourced piece of work)."""


def oswald_efficiency_detail(ar: float, le_sweep_deg: float) -> tuple:
    """(e, clamp) where clamp is "floor" | "ceiling" | None (S6-F3).

    23% of the legal (AR x sweep) space is pinned at a clamp — 11.7% at 0.500 and
    11.2% at 0.950 — where e has ZERO gradient in both arguments. sensitivity.py takes
    finite-difference derivatives of exactly this quantity, so a consumer that cannot
    see the clamp reports a derivative of 0 as if it were physics.
    """
    e_straight = 1.78 * (1.0 - 0.045 * ar ** 0.68) - 0.64
    cos_le = math.cos(math.radians(le_sweep_deg))
    e_swept = 4.61 * (1.0 - 0.045 * ar ** 0.68) * cos_le ** 0.15 - 3.1
    if le_sweep_deg <= 0.0:
        e = e_straight
    elif le_sweep_deg >= 30.0:
        e = e_swept
    else:
        w = le_sweep_deg / 30.0
        e = (1.0 - w) * e_straight + w * e_swept
    if e < OSWALD_FLOOR:
        return OSWALD_FLOOR, "floor"
    if e > OSWALD_CEILING:
        return OSWALD_CEILING, "ceiling"
    return e, None


def oswald_efficiency(ar: float, le_sweep_deg: float) -> float:
    """Raymer straight-wing / swept-wing Oswald fits, blended 0-30 deg LE sweep."""
    return oswald_efficiency_detail(ar, le_sweep_deg)[0]


def parasite_drag_coeff(params: BWBParams, re_mac: float) -> float:
    """Wetted-area buildup: CD0 = Cf * FF * (Swet/Sref) * interference margin.
    All-wing aircraft: exposed area = reference area (no fuselage)."""
    tc = mean_tc(params)
    cf = skin_friction_coeff(re_mac)
    ff = form_factor(tc)
    swet_ratio = 1.977 + 0.52 * tc  # Raymer wetted-area fit for wings
    return cf * ff * swet_ratio * INTERFERENCE_MARGIN


def reynolds_at(params: BWBParams, atmosphere: AtmosphereState, v_ms: float) -> float:
    """Chord Reynolds number at the mean aerodynamic chord, for a given speed."""
    return v_ms * params.mean_aero_chord_m() / atmosphere.kinematic_viscosity_m2s


def cl_max_3d(params: BWBParams, reynolds_mac: float) -> float:
    """Wing CLmax, computed from where the wing actually stalls (S1-C2).

    Was `CL_MAX_2D * 0.90 * cos(sweep)` — one hard-coded section value and one
    unsourced knockdown, identical for every aircraft ever produced here, and
    independent of Reynolds number, thickness and washout. That was the most
    consequential blind spot in the pack for a low-Re small UAV.

    Now `spanwise.solve_stall` finds the station where local demand first reaches local
    capability (Schrenk, NACA TM-948 — see that module). The 0.90 is gone because the
    non-simultaneous-stall allowance it stood in for is now the computed result.

    REYNOLDS IS REQUIRED, and is a real argument rather than a convenience: section
    CLmax falls with Reynolds, and the tip runs at a small fraction of the MAC value
    (measured on the reference wing: 163k at the tip against 409k at the MAC). Pass the
    Reynolds at the speed you care about — `stall_solution` iterates it to the actual
    stall speed, which is NOT the cruise speed.

    THE SWEEP FACTOR IS A PATCH, NOT PART OF THE METHOD. TM-948 is a straight
    monoplane-wing derivation with no sweep term anywhere. `cos(Lc/4)` is retained as a
    separately applied correction and is the weakest link in this result. It is applied
    to AFT sweep only (S6-F6): `math.cos` is even, so a forward-swept quarter chord —
    5.4% of the envelope, down to -31.03 deg — would otherwise be penalised exactly like
    aft sweep."""
    from .spanwise import solve_stall
    sweep_deg = effective_c4_sweep_deg(params)
    wing = solve_stall(params, reynolds_mac).cl_max_wing
    return wing * math.cos(math.radians(max(sweep_deg, 0.0)))


def stall_solution(params: BWBParams, atmosphere: AtmosphereState, weight_n: float,
                   max_iter: int = 8, tol: float = 1e-4):
    """(v_stall_ms, cl_max_3d, spanwise solution) solved self-consistently.

    A wing stalls at its STALL speed, not at cruise, so the Reynolds number that sets
    CLmax depends on the answer. Measured on the golden reference: evaluating at the
    cruise Reynolds (409k) gives CLmax 1.206, while the self-consistent stall Reynolds
    (235k) gives 1.133 — 6% lower, and V_stall 3% higher. Ignoring that would be the
    same error this item exists to remove, committed at aircraft level instead of
    station level.

    Converges in 3-4 iterations from the cruise-speed start."""
    from .spanwise import solve_stall
    s = params.planform_area_m2()
    mac = params.mean_aero_chord_m()
    rho = atmosphere.density_kgm3
    nu = atmosphere.kinematic_viscosity_m2s
    cos_sweep = math.cos(math.radians(max(effective_c4_sweep_deg(params), 0.0)))

    # Start from a plausible speed rather than an arbitrary Reynolds: any positive
    # start converges, but starting near the answer keeps it to a few passes.
    v = math.sqrt(2.0 * weight_n / (rho * s * 1.0))
    sol = None
    for _ in range(max_iter):
        sol = solve_stall(params, max(v * mac / nu, 1.0))
        clmax = sol.cl_max_wing * cos_sweep
        v_new = math.sqrt(2.0 * weight_n / (rho * s * clmax))
        if abs(v_new - v) <= tol * max(v, 1e-6):
            v = v_new
            break
        v = v_new
    return v, sol.cl_max_wing * cos_sweep, sol


def neutral_point_m(params: BWBParams) -> float:
    """Planform aerodynamic centre: area-weighted quarter-MAC of the two
    trapezoids, measured aft of the root leading-edge apex. Ignores the small
    twist/camber shift; VSPAERO's x_np supersedes this at tier 1."""
    c_root, c_mid, c_tip = params.chords()
    b1, b2 = params.half_widths()

    def panel_ac(cr: float, ct: float, half_w: float, x_le_root: float,
                 sweep_deg: float) -> tuple:
        taper = ct / cr if cr > 0 else 1.0
        y_mac = (half_w / 3.0) * (1.0 + 2.0 * taper) / (1.0 + taper)
        mac = (2.0 / 3.0) * cr * (1.0 + taper + taper ** 2) / (1.0 + taper)
        x_le_mac = x_le_root + y_mac * math.tan(math.radians(sweep_deg))
        area = half_w * (cr + ct) / 2.0
        return x_le_mac + 0.25 * mac, area

    x1, a1 = panel_ac(c_root, c_mid, b1, 0.0, params.sweep_inner_deg)
    x_le_mid = b1 * math.tan(math.radians(params.sweep_inner_deg))
    x2, a2 = panel_ac(c_mid, c_tip, b2, x_le_mid, params.sweep_outer_deg)
    return (a1 * x1 + a2 * x2) / (a1 + a2)


# ---------------------------------------------------------------------------
# full panel
# ---------------------------------------------------------------------------

def estimate(params: BWBParams, atmosphere: AtmosphereState, v_ms: float,
             weight_n: float,
             launch: Optional[Literal["hand", "catapult", "runway"]] = None
             ) -> AeroEstimate:
    """Compute the tier-0 metric panel for one geometry at one condition.

    Validates its flight condition (S6-F9/F10). Before this: v_ms=0 raised a bare
    ZeroDivisionError, v_ms=-5 returned a plausible-looking panel with
    reynolds_mac=-34348 (the Reynolds floor laundered it), NaN propagated silently
    through every field, weight_n=inf gave cl=inf, and an unrecognised launch string
    silently DISABLED the launch gate — estimate(..., launch="glider") returned
    limit=None, ok=None beside a stall speed that would have failed all three real
    limits.
    """
    if not math.isfinite(v_ms) or v_ms <= 0.0:
        raise ValueError(f"cruise speed must be finite and positive, got {v_ms!r}")
    if not math.isfinite(weight_n) or weight_n <= 0.0:
        raise ValueError(f"weight must be finite and positive, got {weight_n!r}")
    if launch is not None and launch not in LAUNCH_VSTALL_MAX:
        raise ValueError(
            f"unknown launch method {launch!r}; expected one of "
            f"{sorted(LAUNCH_VSTALL_MAX)}")

    s = params.planform_area_m2()
    ar = params.aspect_ratio()
    mac = params.mean_aero_chord_m()
    _, _, c_tip = params.chords()
    rho_kgm3 = atmosphere.density_kgm3
    q = 0.5 * rho_kgm3 * v_ms ** 2
    re_mac = v_ms * mac / atmosphere.kinematic_viscosity_m2s

    cd0 = parasite_drag_coeff(params, re_mac)
    sweep_eff = effective_le_sweep_deg(params)
    e, oswald_clamp = oswald_efficiency_detail(ar, sweep_eff)

    cl = weight_n / (q * s)
    k = 1.0 / (math.pi * e * ar)
    cd = cd0 + k * cl ** 2
    cl_star = math.sqrt(cd0 / k)
    ld_max = cl_star / (2.0 * cd0)

    # S1-C2: solved self-consistently — CLmax depends on Reynolds, Reynolds on the stall
    # speed, and the stall speed on CLmax. See `stall_solution`.
    v_stall, clmax, stall = stall_solution(params, atmosphere, weight_n)
    launch_limit = LAUNCH_VSTALL_MAX[launch] if launch is not None else None

    x_np = neutral_point_m(params)
    mass_kg = weight_n / 9.81
    root_chord = params.root_chord_m

    # 22.31% of the flight envelope reported a cruise point BEYOND STALL with nothing
    # saying so — cl_cruise reached 136.43 while launch_ok stayed True (S6-F1).
    cruise_feasible = v_ms >= v_stall
    stall_margin = (v_ms - v_stall) / v_stall
    # x_np lands outside (0, root_chord) in 6.8% of the envelope, up to 2.48x the root
    # chord: a CG recommendation with nowhere to put the mass (S6-F13b).
    cg_placement_infeasible = not (0.0 < x_np < root_chord)

    # Each assumption states its own sensitivity where one has been measured (S3-F12b):
    # "assumes a 30% laminar run" means nothing to a reader who cannot tell whether
    # that is worth 1% or 11% of the L/D on the card.
    assumptions = [
        f"Mixed flat-plate skin friction, {LAMINAR_FRACTION:.0%} laminar run assumed, "
        f"with the laminar segment evaluated at the transition Reynolds number "
        f"(engineering judgment — at 0% laminar, CD0 is ~9% higher and L/D max ~4% "
        f"lower)",
        f"Hoerner form factor + Raymer wetted-area fit; +{INTERFERENCE_MARGIN - 1:.0%} "
        f"interference margin — the only allowance for prop, gaps, antennas and "
        f"camera (engineering judgment; measured small-UAV flying wings sit at "
        f"CD0 0.020-0.035)",
        f"Oswald e = {e:.3f} from Raymer fits (AR {ar:.2f}, effective LE sweep "
        f"{sweep_eff:.1f} deg)",
        f"CLmax {clmax:.3f} computed from the spanwise load (Schrenk, NACA TM-948): "
        f"stall begins at {stall.stall_eta:.0%} semi-span"
        + (" — OUTBOARD, which pitches a swept tailless wing UP into the stall"
           if stall.stall_is_outboard else " (inboard)")
        + f". Sections evaluated at their own local Reynolds; solved self-consistently "
          f"at the stall speed, not at cruise. The cos(sweep) factor is a PATCH — "
          f"TM-948 is a straight-wing method with no sweep term",
        "Neutral point = planform quarter-MAC estimate; VSPAERO x_np supersedes it",
        f"Static margin target {STATIC_MARGIN_TARGET:.0%} MAC when placing the CG "
        f"(engineering judgment, tailless-BWB practice)",
        "Root bending proxy W*b/8 — UNIFORM half-span load, a conservative "
        "comparative index. Not elliptic loading: the elliptic result is W*b/(3*pi), "
        "about 18% lower. No structural sizing.",
    ]
    if launch:
        assumptions.append(
            f"Launch '{launch}' tolerates stall up to {launch_limit} m/s — engineering "
            f"judgment, not derived from the pack's wing-loading table")
    if oswald_clamp:
        assumptions.append(
            f"Oswald e is AT ITS {oswald_clamp.upper()} ({e:.2f}) for this AR and "
            f"sweep, so it is INSENSITIVE here: changing aspect ratio or sweep moves "
            f"it by nothing at all (S6-F3)")
    if not cruise_feasible:
        assumptions.append(
            f"This condition is BELOW the stall speed: cruise CL {cl:.2f} exceeds "
            f"CLmax {clmax:.2f}, so the aircraft cannot hold {v_ms:.1f} m/s in level "
            f"flight. The L/D shown is arithmetic, not flight.")
    if cg_placement_infeasible:
        assumptions.append(
            f"Neutral point {x_np:.3f} m lies outside the root chord "
            f"(0 to {root_chord:.3f} m), so the CG this implies may be physically "
            f"unplaceable on the airframe")

    # S1-D: trim about the CG this panel already recommends, at the CL it already
    # computed. Imported here rather than at module scope because trim.py imports
    # `neutral_point_m` from this module.
    from .trim import solve_trim
    x_cg_target = x_np - STATIC_MARGIN_TARGET * mac
    trim = solve_trim(params, cl, x_cg_target, re_mac)

    # S1-G: reported beside the Oswald fit, never substituted for it.
    from .lifting_line import span_efficiency
    delta_ll, e_planar = span_efficiency(params)

    # S1-E: the same treatment for profile drag — measured section data beside the
    # wetted-area correlation, with the gap between them stated rather than averaged.
    from .section_drag import profile_drag
    sec_drag = profile_drag(params, cl, re_mac)

    # S1-H: dihedral effect scales with CL, so it is reported at both ends of the
    # useful range — cruise, and CLmax where it is strongest and the ailerons weakest.
    from .dihedral import effective_dihedral
    dihedral_cruise = effective_dihedral(params, cl)
    dihedral_slow = effective_dihedral(params, clmax)

    return AeroEstimate(
        s_ref_m2=s, aspect_ratio=ar, span_m=params.span_m, mac_m=mac,
        wing_loading_kgm2=mass_kg / s,
        washout_deg=params.root_twist_deg - params.tip_twist_deg,
        v_ms=v_ms, rho_kgm3=rho_kgm3, weight_n=weight_n, reynolds_mac=re_mac,
        cd0=cd0, oswald_e=e, cl_cruise=cl, cd_cruise=cd, ld_cruise=cl / cd,
        cl_star=cl_star, ld_max=ld_max,
        power_fom=cl ** 1.5 / cd,
        cl_max_3d=clmax, v_stall_ms=v_stall,
        stall_station_frac=stall.stall_eta, stall_is_outboard=stall.stall_is_outboard,
        cruise_feasible=cruise_feasible, stall_margin=stall_margin,
        launch_limit_ms=launch_limit,
        # `is not None`, not truthiness: a limit of 0.0 would have skipped the gate.
        # The tolerance is shared with inverse._statuses so the two surfaces cannot
        # disagree about the same breach (constants.VSTALL_TOL_MS).
        launch_ok=(v_stall <= launch_limit + VSTALL_TOL_MS)
        if launch_limit is not None else None,
        cd_profile_sections=sec_drag.cd_profile,
        cd0_sections_at_zero_lift=sec_drag.cd_profile_at_zero_lift,
        cd0_model_disagreement=sec_drag.disagreement_ratio,
        cd_sections_span_clamped=(sec_drag.span_fraction_below_ladder
                                  + sec_drag.span_fraction_above_ladder),
        span_efficiency_planar=e_planar, span_efficiency_delta=delta_ll,
        oswald_e_span_gap=e_planar - e,
        oswald_e_clamped=oswald_clamp,
        cg_placement_infeasible=cg_placement_infeasible,
        x_np_m=x_np,
        x_cg_for_target_sm_m=x_cg_target,
        cm_trim_residual=trim.cm_cg,
        trims_as_built=trim.trims_as_built,
        tip_twist_for_trim_deg=trim.tip_twist_for_trim_deg,
        trim_twist_reachable=trim.tip_twist_in_range,
        x_cg_for_trim_m=trim.x_cg_for_trim_m,
        static_margin_at_trim=trim.static_margin_at_trim,
        effective_dihedral_deg=dihedral_cruise.total_deg,
        effective_dihedral_from_sweep_deg=dihedral_cruise.sweep_deg,
        effective_dihedral_at_clmax_deg=dihedral_slow.total_deg,
        cl_beta_per_rad=dihedral_cruise.cl_beta_per_rad,
        effective_c2_sweep_deg=dihedral_cruise.half_chord_sweep_deg,
        root_bending_index_nm=weight_n * params.span_m / 8.0,
        tip_re=v_ms * c_tip / atmosphere.kinematic_viscosity_m2s,
        roll_damping_index=params.span_m ** 2,
        assumptions=assumptions,
    )
