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

# NACA 4-series section CLmax at Re ~ 7e5 (Abbott & von Doenhoff section data).
# Fixed at that Reynolds number: cl_max_3d is bit-identical across Re 1.8e5-1.0e6,
# which is the dominant low-Re effect this tier does not model (S6-F5 / S7-CAP-4).
CL_MAX_2D = 1.30

# Raymer: finite-wing knockdown from section CLmax to wing CLmax, before sweep.
CL_MAX_3D_FACTOR = 0.90

# ENGINEERING JUDGMENT (Astra): the highest stall speed each launch method tolerates,
# m/s. NOT derived from defaults.WING_LOADING — inverting these against that table
# implies CLmax 1.02 (hand) / 0.95 (catapult) / 0.81 (runway), three different values,
# none of which equals this pack's own CL_MAX_2D * CL_MAX_3D_FACTOR = 1.17. The
# previous comment claimed they were "calibrated so the pack's own wing-loading table
# sits at the boundary"; they are three independent judgments. Revisit against
# Sadraey §5 / Raymer §5 before treating them as calibrated.
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
    endurance_fom: float = Field(description="CL^1.5/CD at cruise (power ~ 1/this)")

    # stall / launch
    cl_max_3d: float
    v_stall_ms: float
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
    cg_placement_infeasible: bool = False
    """x_np fell outside (0, root_chord): 6.8% of the envelope, up to 2.48x the root
    chord. The CG this implies may have nowhere to sit on the airframe (S6-F13b)."""

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
    t_mid = 0.5 * (t_root + t_tip)
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


def cl_max_3d(params: BWBParams) -> float:
    """Section CLmax knocked down for finite span and quarter-chord sweep
    (Raymer: CLmax ~ 0.9 * Clmax * cos(Lc/4)).

    Independent of Reynolds number, thickness and washout — verified bit-identical
    across Re 1.8e5-1.0e6, mean t/c 0.064-0.168 and washout 0-12 deg — so v_stall_ms
    and the whole launch_ok gate are too. That is the most consequential blind spot for
    a low-Re small UAV (section Clmax is ~0.9 at Re 1.5e5 and ~1.45 at 1e6). Fixing it
    needs a sourced correlation, not an invented scaling: S7-CAP-4.

    The knockdown applies to AFT sweep only (S6-F6). `math.cos` is even, so a
    forward-swept quarter chord — which happens in 5.4% of the envelope, down to
    -31.03 deg — was penalised exactly like aft sweep, while Raymer's correlation is an
    aft-sweep one."""
    sweep_deg = effective_c4_sweep_deg(params)
    return CL_MAX_2D * CL_MAX_3D_FACTOR * math.cos(math.radians(max(sweep_deg, 0.0)))


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

    clmax = cl_max_3d(params)
    v_stall = math.sqrt(2.0 * weight_n / (rho_kgm3 * s * clmax))
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
        f"Section CLmax {CL_MAX_2D} (NACA 4-series, Re~7e5) x {CL_MAX_3D_FACTOR} "
        f"finite-wing x cos(sweep) — fixed at that Reynolds number, so CLmax here "
        f"does not respond to speed or altitude",
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

    return AeroEstimate(
        s_ref_m2=s, aspect_ratio=ar, span_m=params.span_m, mac_m=mac,
        wing_loading_kgm2=mass_kg / s,
        washout_deg=params.root_twist_deg - params.tip_twist_deg,
        v_ms=v_ms, rho_kgm3=rho_kgm3, weight_n=weight_n, reynolds_mac=re_mac,
        cd0=cd0, oswald_e=e, cl_cruise=cl, cd_cruise=cd, ld_cruise=cl / cd,
        cl_star=cl_star, ld_max=ld_max,
        endurance_fom=cl ** 1.5 / cd,
        cl_max_3d=clmax, v_stall_ms=v_stall,
        cruise_feasible=cruise_feasible, stall_margin=stall_margin,
        launch_limit_ms=launch_limit,
        # `is not None`, not truthiness: a limit of 0.0 would have skipped the gate.
        # The tolerance is shared with inverse._statuses so the two surfaces cannot
        # disagree about the same breach (constants.VSTALL_TOL_MS).
        launch_ok=(v_stall <= launch_limit + VSTALL_TOL_MS)
        if launch_limit is not None else None,
        oswald_e_clamped=oswald_clamp,
        cg_placement_infeasible=cg_placement_infeasible,
        x_np_m=x_np,
        x_cg_for_target_sm_m=x_np - STATIC_MARGIN_TARGET * mac,
        root_bending_index_nm=weight_n * params.span_m / 8.0,
        tip_re=v_ms * c_tip / atmosphere.kinematic_viscosity_m2s,
        roll_damping_index=params.span_m ** 2,
        assumptions=assumptions,
    )
