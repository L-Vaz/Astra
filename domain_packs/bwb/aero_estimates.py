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
from typing import List, Optional

from pydantic import BaseModel, Field

from .atmosphere import AtmosphereState
from .constants import STATIC_MARGIN_TARGET as _STATIC_MARGIN_TARGET
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
# MEASURED SENSITIVITY: 0.30 -> 0.00 raises CD0 by 22% and drops ld_max by 11%;
# 0.30 -> 0.60 raises ld_max to 19.41 (from 17.10) on the reference geometry.
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
    launch_limit_ms: Optional[float] = None
    launch_ok: Optional[bool] = None

    # stability / control (planform estimate; VSPAERO refines x_np)
    x_np_m: float = Field(description="neutral point aft of root LE apex")
    x_cg_for_target_sm_m: float

    # proxies
    root_bending_index_nm: float = Field(
        description=("W*b/8 — uniform half-span load, a conservative comparative "
                     "index (NOT elliptic; the elliptic value is W*b/(3*pi), ~18% "
                     "lower). No structural sizing."))
    tip_re: float
    roll_damping_index: float = Field(description="relative, ~span^2")

    assumptions: List[str] = Field(default_factory=list)


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
    """Half-width-weighted leading-edge sweep of the two panels."""
    b1, b2 = params.half_widths()
    return (b1 * params.sweep_inner_deg + b2 * params.sweep_outer_deg) / (b1 + b2)


def effective_c4_sweep_deg(params: BWBParams) -> float:
    """Half-width-weighted quarter-chord sweep (governs CLmax and lift slope).
    Per trapezoid: tan(Lc/4) = tan(LLE) - 0.25*(cr - ct)/half_width."""
    c_root, c_mid, c_tip = params.chords()
    b1, b2 = params.half_widths()
    t1 = math.tan(math.radians(params.sweep_inner_deg)) - 0.25 * (c_root - c_mid) / b1
    t2 = math.tan(math.radians(params.sweep_outer_deg)) - 0.25 * (c_mid - c_tip) / b2
    return math.degrees(math.atan((b1 * t1 + b2 * t2) / (b1 + b2)))


def skin_friction_coeff(re: float, laminar_fraction: Optional[float] = None) -> float:
    """Mixed laminar/turbulent flat-plate Cf (area-fraction blend).

    The default is resolved from the module constant at CALL time, not bound into the
    signature at import time. Bound as a default argument, LAMINAR_FRACTION stopped
    being the lever its own comment claims it is: changing it — including in a
    sensitivity test — moved nothing (S3-F12).
    """
    if laminar_fraction is None:
        laminar_fraction = LAMINAR_FRACTION
    re = max(re, 1e4)
    cf_turb = 0.455 / (math.log10(re) ** 2.58)          # Schlichting
    cf_lam = 1.328 / math.sqrt(re)                       # Blasius
    return laminar_fraction * cf_lam + (1.0 - laminar_fraction) * cf_turb


def form_factor(tc: float) -> float:
    """Hoerner wing form factor: 1 + 2(t/c) + 60(t/c)^4."""
    return 1.0 + 2.0 * tc + 60.0 * tc ** 4


def oswald_efficiency(ar: float, le_sweep_deg: float) -> float:
    """Raymer straight-wing / swept-wing Oswald fits, blended 0-30 deg LE sweep."""
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
    return max(0.5, min(e, 0.95))


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
    (Raymer: CLmax ~ 0.9 * Clmax * cos(Lc/4))."""
    sweep = math.radians(effective_c4_sweep_deg(params))
    return CL_MAX_2D * CL_MAX_3D_FACTOR * math.cos(sweep)


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
             launch: Optional[str] = None) -> AeroEstimate:
    """Compute the tier-0 metric panel for one geometry at one condition."""
    s = params.planform_area_m2()
    ar = params.aspect_ratio()
    mac = params.mean_aero_chord_m()
    _, _, c_tip = params.chords()
    rho_kgm3 = atmosphere.density_kgm3
    q = 0.5 * rho_kgm3 * v_ms ** 2
    re_mac = v_ms * mac / atmosphere.kinematic_viscosity_m2s

    cd0 = parasite_drag_coeff(params, re_mac)
    sweep_eff = effective_le_sweep_deg(params)
    e = oswald_efficiency(ar, sweep_eff)

    cl = weight_n / (q * s)
    k = 1.0 / (math.pi * e * ar)
    cd = cd0 + k * cl ** 2
    cl_star = math.sqrt(cd0 / k)
    ld_max = cl_star / (2.0 * cd0)

    clmax = cl_max_3d(params)
    v_stall = math.sqrt(2.0 * weight_n / (rho_kgm3 * s * clmax))
    launch_limit = LAUNCH_VSTALL_MAX.get(launch) if launch else None

    x_np = neutral_point_m(params)
    mass_kg = weight_n / 9.81

    # Each assumption states its own sensitivity where one has been measured (S3-F12b):
    # "assumes a 30% laminar run" means nothing to a reader who cannot tell whether
    # that is worth 1% or 11% of the L/D on the card.
    assumptions = [
        f"Mixed flat-plate skin friction, {LAMINAR_FRACTION:.0%} laminar run assumed "
        f"(engineering judgment — at 0% laminar, CD0 is ~22% higher and L/D max ~11% "
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

    return AeroEstimate(
        s_ref_m2=s, aspect_ratio=ar, span_m=params.span_m, mac_m=mac,
        wing_loading_kgm2=mass_kg / s,
        washout_deg=params.root_twist_deg - params.tip_twist_deg,
        v_ms=v_ms, rho_kgm3=rho_kgm3, weight_n=weight_n, reynolds_mac=re_mac,
        cd0=cd0, oswald_e=e, cl_cruise=cl, cd_cruise=cd, ld_cruise=cl / cd,
        cl_star=cl_star, ld_max=ld_max,
        endurance_fom=cl ** 1.5 / cd,
        cl_max_3d=clmax, v_stall_ms=v_stall,
        launch_limit_ms=launch_limit,
        launch_ok=(v_stall <= launch_limit) if launch_limit else None,
        x_np_m=x_np,
        x_cg_for_target_sm_m=x_np - STATIC_MARGIN_TARGET * mac,
        root_bending_index_nm=weight_n * params.span_m / 8.0,
        tip_re=v_ms * c_tip / atmosphere.kinematic_viscosity_m2s,
        roll_damping_index=params.span_m ** 2,
        assumptions=assumptions,
    )
