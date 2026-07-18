"""BWB domain pack — machine-readable first-principles derivation graph.

Turns the tier-0 estimate into an interrogable equation tree: every metric is
a node carrying its formula, the actual substituted numbers, its inputs
(children), and a source citation (textbook method vs declared engineering
default). The cockpit's "why" panel renders this; clicking CD expands
CD = CD0 + k*CL^2, clicking CD0 expands the wetted-area buildup, down to the
Reynolds-number leaves.

Values are computed with the SAME shared helpers as aero_estimates.estimate()
so they cannot drift — tests/test_bwb_derivation.py asserts node values equal
the AeroEstimate fields exactly.

Pydantic-only; safe in both the FoamAgent (py3.12) and OpenVSP (py3.10) envs.
"""
import math
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from .aero_estimates import (CL_MAX_2D, CL_MAX_3D_FACTOR, INTERFERENCE_MARGIN,
                             LAMINAR_FRACTION, LAUNCH_VSTALL_MAX, MU_AIR,
                             STATIC_MARGIN_TARGET, effective_c4_sweep_deg,
                             effective_le_sweep_deg, form_factor, mean_tc,
                             neutral_point_m, oswald_efficiency,
                             skin_friction_coeff)
from .schema import BWBParams

ROOTS = ["ld_cruise", "cd_cruise", "cl_cruise", "cd0", "oswald_e", "cl_star",
         "ld_max", "cl_max_3d", "v_stall_ms", "x_np_m", "reynolds_mac",
         "wing_loading_kgm2"]


class DerivationNode(BaseModel):
    id: str
    label: str
    symbol: str
    value: float
    unit: str = ""
    formula: str = ""            # "CD = CD0 + k·CL²"
    substitution: str = ""       # "0.0132 + 0.0578 × 0.412² = 0.0230"
    children: List[str] = Field(default_factory=list)
    source: str = ""             # citation or "engineering default (...)"
    kind: Literal["computed", "constant", "input"] = "computed"


class DerivationGraph(BaseModel):
    roots: List[str]
    nodes: Dict[str, DerivationNode]


def _g(v: float, sig: int = 4) -> str:
    """Compact numeric formatting for substitution strings."""
    return f"{v:.{sig}g}"


def derive_graph(params: BWBParams, rho_kgm3: float, v_ms: float,
                 weight_n: float, launch: Optional[str] = None) -> DerivationGraph:
    """Full derivation graph for one geometry at one flight condition."""
    nodes: Dict[str, DerivationNode] = {}

    def add(node: DerivationNode) -> DerivationNode:
        nodes[node.id] = node
        return node

    def inp(id_: str, label: str, symbol: str, value: float, unit: str = "",
            source: str = "user requirement / geometry parameter") -> None:
        add(DerivationNode(id=id_, label=label, symbol=symbol, value=value,
                           unit=unit, kind="input", source=source))

    def const(id_: str, label: str, symbol: str, value: float, source: str,
              unit: str = "") -> None:
        add(DerivationNode(id=id_, label=label, symbol=symbol, value=value,
                           unit=unit, kind="constant", source=source))

    # ---------------- inputs -------------------------------------------------
    mass_kg = weight_n / 9.81
    inp("mtow_kg", "Takeoff mass", "m", mass_kg, "kg")
    inp("v_ms", "Cruise speed", "V", v_ms, "m/s")
    inp("rho", "Air density", "ρ", rho_kgm3, "kg/m³",
        source="ISA troposphere at cruise altitude")
    for pid, label, sym, unit in [
            ("span_m", "Wingspan", "b", "m"),
            ("root_chord_m", "Root chord", "c_r", "m"),
            ("centre_body_fraction", "Centre-body fraction", "f_cb", ""),
            ("mid_chord_frac", "Mid-chord fraction", "f_m", ""),
            ("taper_outer", "Outer taper", "λ", ""),
            ("sweep_inner_deg", "Inner LE sweep", "Λ_i", "deg"),
            ("sweep_outer_deg", "Outer LE sweep", "Λ_o", "deg"),
            ("root_thickness_pct", "Root thickness", "t/c_r", "%"),
            ("tip_thickness_pct", "Tip thickness", "t/c_t", "%")]:
        inp(pid, label, sym, float(getattr(params, pid)), unit)

    # ---------------- constants ---------------------------------------------
    const("mu_air", "Air viscosity", "μ", MU_AIR,
          "dynamic viscosity of air at ~15 °C", "kg/(m·s)")
    const("laminar_fraction", "Laminar-run fraction", "f_lam", LAMINAR_FRACTION,
          "engineering default (LAMINAR_FRACTION = 0.30): smooth small-UAV wing at Re ~ 5e5–1e6")
    const("interference_margin", "Interference margin", "k_int", INTERFERENCE_MARGIN,
          "engineering default (+10%): excrescences, gaps, antennas")
    const("cl_max_2d", "Section CLmax", "Cl_max", CL_MAX_2D,
          "engineering default: NACA 4-series section at Re ~ 7e5")
    const("cl_max_3d_factor", "Finite-wing knockdown", "k_3D", CL_MAX_3D_FACTOR,
          "Raymer: CLmax ≈ 0.9·Clmax·cos(Λc/4)")
    const("static_margin_target", "Static-margin target", "SM", STATIC_MARGIN_TARGET,
          "engineering default: 8% MAC for a tailless BWB")

    # ---------------- planform ----------------------------------------------
    c_root, c_mid, c_tip = params.chords()
    b1, b2 = params.half_widths()
    s = params.planform_area_m2()
    mac = params.mean_aero_chord_m()
    ar = params.aspect_ratio()
    planform_children = ["span_m", "root_chord_m", "centre_body_fraction",
                         "mid_chord_frac", "taper_outer"]
    add(DerivationNode(
        id="s_ref", label="Reference (planform) area", symbol="S", value=s, unit="m²",
        formula="S = 2·[b₁(c_r+c_m)/2 + b₂(c_m+c_t)/2]",
        substitution=f"2·[{_g(b1)}·({_g(c_root)}+{_g(c_mid)})/2 + "
                     f"{_g(b2)}·({_g(c_mid)}+{_g(c_tip)})/2] = {_g(s)} m²",
        children=planform_children,
        source="two-trapezoid planform geometry (exact)"))
    add(DerivationNode(
        id="mac_m", label="Mean aerodynamic chord", symbol="c̄", value=mac, unit="m",
        formula="c̄ = area-weighted trapezoid MAC of the two panels",
        substitution=f"c̄ = {_g(mac)} m",
        children=planform_children,
        source="standard trapezoid MAC formula (exact)"))
    add(DerivationNode(
        id="aspect_ratio", label="Aspect ratio", symbol="AR", value=ar,
        formula="AR = b²/S",
        substitution=f"{_g(params.span_m)}²/{_g(s)} = {_g(ar)}",
        children=["span_m", "s_ref"],
        source="definition"))
    add(DerivationNode(
        id="wing_loading_kgm2", label="Wing loading", symbol="W/S",
        value=mass_kg / s, unit="kg/m²",
        formula="W/S = m/S",
        substitution=f"{_g(mass_kg)}/{_g(s)} = {_g(mass_kg / s)} kg/m²",
        children=["mtow_kg", "s_ref"],
        source="definition"))

    # ---------------- flight condition --------------------------------------
    q = 0.5 * rho_kgm3 * v_ms ** 2
    add(DerivationNode(
        id="weight_n", label="Weight", symbol="W", value=weight_n, unit="N",
        formula="W = m·g",
        substitution=f"{_g(mass_kg)}·9.81 = {_g(weight_n)} N",
        children=["mtow_kg"], source="definition"))
    add(DerivationNode(
        id="q_pa", label="Dynamic pressure", symbol="q", value=q, unit="Pa",
        formula="q = ½·ρ·V²",
        substitution=f"0.5·{_g(rho_kgm3)}·{_g(v_ms)}² = {_g(q)} Pa",
        children=["rho", "v_ms"], source="definition"))
    re_mac = rho_kgm3 * v_ms * mac / MU_AIR
    add(DerivationNode(
        id="reynolds_mac", label="Reynolds number (MAC)", symbol="Re",
        value=re_mac,
        formula="Re = ρ·V·c̄/μ",
        substitution=f"{_g(rho_kgm3)}·{_g(v_ms)}·{_g(mac)}/{_g(MU_AIR, 3)} = {_g(re_mac, 3)}",
        children=["rho", "v_ms", "mac_m", "mu_air"], source="definition"))

    # ---------------- parasite drag buildup ---------------------------------
    cf = skin_friction_coeff(re_mac)
    re_c = max(re_mac, 1e4)
    cf_turb = 0.455 / (math.log10(re_c) ** 2.58)
    cf_lam = 1.328 / math.sqrt(re_c)
    add(DerivationNode(
        id="cf", label="Skin-friction coefficient", symbol="C_f", value=cf,
        formula="C_f = f_lam·(1.328/√Re) + (1−f_lam)·(0.455/log₁₀(Re)^2.58)",
        substitution=f"{LAMINAR_FRACTION:.2f}·{_g(cf_lam, 3)} + "
                     f"{1 - LAMINAR_FRACTION:.2f}·{_g(cf_turb, 3)} = {_g(cf, 3)}",
        children=["reynolds_mac", "laminar_fraction"],
        source="Blasius laminar + Schlichting turbulent flat plate, area-fraction blend"))
    tc = mean_tc(params)
    add(DerivationNode(
        id="mean_tc", label="Mean thickness ratio", symbol="t/c", value=tc,
        formula="t/c = area-weighted mean of root and tip sections",
        substitution=f"t/c = {_g(tc, 3)}",
        children=["root_thickness_pct", "tip_thickness_pct"],
        source="area-weighted panel average (exact)"))
    ff = form_factor(tc)
    add(DerivationNode(
        id="form_factor", label="Form factor", symbol="FF", value=ff,
        formula="FF = 1 + 2(t/c) + 60(t/c)⁴",
        substitution=f"1 + 2·{_g(tc, 3)} + 60·{_g(tc, 3)}⁴ = {_g(ff, 4)}",
        children=["mean_tc"],
        source="Hoerner wing form factor"))
    swet_ratio = 1.977 + 0.52 * tc
    add(DerivationNode(
        id="swet_ratio", label="Wetted-area ratio", symbol="S_wet/S", value=swet_ratio,
        formula="S_wet/S = 1.977 + 0.52(t/c)",
        substitution=f"1.977 + 0.52·{_g(tc, 3)} = {_g(swet_ratio, 4)}",
        children=["mean_tc"],
        source="Raymer wetted-area fit for wings"))
    cd0 = cf * ff * swet_ratio * INTERFERENCE_MARGIN
    add(DerivationNode(
        id="cd0", label="Parasite drag coefficient", symbol="CD₀", value=cd0,
        formula="CD₀ = C_f · FF · (S_wet/S) · k_int",
        substitution=f"{_g(cf, 3)}·{_g(ff, 4)}·{_g(swet_ratio, 4)}·{INTERFERENCE_MARGIN} "
                     f"= {_g(cd0, 4)}",
        children=["cf", "form_factor", "swet_ratio", "interference_margin"],
        source="Raymer wetted-area drag buildup (all-wing: exposed area = reference area)"))

    # ---------------- induced drag ------------------------------------------
    le_sweep = effective_le_sweep_deg(params)
    add(DerivationNode(
        id="le_sweep_eff", label="Effective LE sweep", symbol="Λ_LE", value=le_sweep,
        unit="deg",
        formula="Λ_LE = half-width-weighted mean of the two panel sweeps",
        substitution=f"({_g(b1)}·{_g(params.sweep_inner_deg)} + {_g(b2)}·"
                     f"{_g(params.sweep_outer_deg)})/{_g(b1 + b2)} = {_g(le_sweep)}°",
        children=["sweep_inner_deg", "sweep_outer_deg", "centre_body_fraction"],
        source="half-width-weighted average (exact)"))
    e = oswald_efficiency(ar, le_sweep)
    add(DerivationNode(
        id="oswald_e", label="Oswald span efficiency", symbol="e", value=e,
        formula="e = Raymer straight/swept-wing fits, blended 0–30° LE sweep, clamped [0.5, 0.95]",
        substitution=f"e(AR={_g(ar, 3)}, Λ={_g(le_sweep, 3)}°) = {_g(e, 3)}",
        children=["aspect_ratio", "le_sweep_eff"],
        source="Raymer Oswald-efficiency fits"))
    k = 1.0 / (math.pi * e * ar)
    add(DerivationNode(
        id="k_induced", label="Induced-drag factor", symbol="k", value=k,
        formula="k = 1/(π·e·AR)",
        substitution=f"1/(π·{_g(e, 3)}·{_g(ar, 3)}) = {_g(k, 4)}",
        children=["oswald_e", "aspect_ratio"],
        source="lifting-line induced drag"))

    # ---------------- lift, drag, L/D ---------------------------------------
    cl = weight_n / (q * s)
    add(DerivationNode(
        id="cl_cruise", label="Cruise lift coefficient", symbol="CL", value=cl,
        formula="CL = W/(q·S)   (lift = weight in steady cruise)",
        substitution=f"{_g(weight_n)}/({_g(q)}·{_g(s)}) = {_g(cl, 3)}",
        children=["weight_n", "q_pa", "s_ref"],
        source="steady-level-flight equilibrium"))
    cd = cd0 + k * cl ** 2
    add(DerivationNode(
        id="cd_cruise", label="Cruise drag coefficient", symbol="CD", value=cd,
        formula="CD = CD₀ + k·CL²",
        substitution=f"{_g(cd0, 4)} + {_g(k, 4)}·{_g(cl, 3)}² = {_g(cd, 4)}",
        children=["cd0", "k_induced", "cl_cruise"],
        source="parabolic drag polar"))
    add(DerivationNode(
        id="ld_cruise", label="Cruise lift-to-drag ratio", symbol="L/D", value=cl / cd,
        formula="L/D = CL/CD",
        substitution=f"{_g(cl, 3)}/{_g(cd, 4)} = {_g(cl / cd, 3)}",
        children=["cl_cruise", "cd_cruise"],
        source="definition"))
    cl_star = math.sqrt(cd0 / k)
    add(DerivationNode(
        id="cl_star", label="Best-L/D lift coefficient", symbol="CL*", value=cl_star,
        formula="CL* = √(CD₀/k)   (where induced drag equals parasite drag)",
        substitution=f"√({_g(cd0, 4)}/{_g(k, 4)}) = {_g(cl_star, 3)}",
        children=["cd0", "k_induced"],
        source="drag-polar minimum of CD/CL"))
    add(DerivationNode(
        id="ld_max", label="Best achievable L/D", symbol="(L/D)max",
        value=cl_star / (2.0 * cd0),
        formula="(L/D)max = CL*/(2·CD₀)",
        substitution=f"{_g(cl_star, 3)}/(2·{_g(cd0, 4)}) = {_g(cl_star / (2 * cd0), 3)}",
        children=["cl_star", "cd0"],
        source="drag-polar identity"))

    # ---------------- stall --------------------------------------------------
    c4_sweep = effective_c4_sweep_deg(params)
    add(DerivationNode(
        id="c4_sweep_eff", label="Effective quarter-chord sweep", symbol="Λ_c/4",
        value=c4_sweep, unit="deg",
        formula="tan(Λ_c/4) = tan(Λ_LE) − 0.25(c_r−c_t)/half-width, per panel",
        substitution=f"Λ_c/4 = {_g(c4_sweep, 3)}°",
        children=["sweep_inner_deg", "sweep_outer_deg", "root_chord_m",
                  "mid_chord_frac", "taper_outer"],
        source="quarter-chord sweep geometry (exact)"))
    clmax = CL_MAX_2D * CL_MAX_3D_FACTOR * math.cos(math.radians(c4_sweep))
    add(DerivationNode(
        id="cl_max_3d", label="Max lift coefficient (3D)", symbol="CLmax", value=clmax,
        formula="CLmax = Cl_max · k_3D · cos(Λ_c/4)",
        substitution=f"{CL_MAX_2D}·{CL_MAX_3D_FACTOR}·cos({_g(c4_sweep, 3)}°) = {_g(clmax, 3)}",
        children=["cl_max_2d", "cl_max_3d_factor", "c4_sweep_eff"],
        source="Raymer finite-wing CLmax knockdown"))
    v_stall = math.sqrt(2.0 * weight_n / (rho_kgm3 * s * clmax))
    stall_children = ["weight_n", "rho", "s_ref", "cl_max_3d"]
    add(DerivationNode(
        id="v_stall_ms", label="Stall speed", symbol="V_stall", value=v_stall, unit="m/s",
        formula="V_stall = √(2W/(ρ·S·CLmax))",
        substitution=f"√(2·{_g(weight_n)}/({_g(rho_kgm3)}·{_g(s)}·{_g(clmax, 3)})) "
                     f"= {_g(v_stall, 3)} m/s",
        children=stall_children,
        source="1-g stall from lift equation"))
    if launch:
        cap = LAUNCH_VSTALL_MAX[launch]
        const("launch_limit", f"Stall tolerance ({launch} launch)", "V_cap", cap,
              f"engineering default: '{launch}' launch tolerates stall ≤ {cap} m/s",
              "m/s")
        nodes["v_stall_ms"].children = stall_children + ["launch_limit"]

    # ---------------- stability ---------------------------------------------
    x_np = neutral_point_m(params)
    add(DerivationNode(
        id="x_np_m", label="Neutral point (aft of nose apex)", symbol="x_np",
        value=x_np, unit="m",
        formula="x_np = area-weighted quarter-MAC of the two trapezoid panels",
        substitution=f"x_np = {_g(x_np, 4)} m",
        children=["sweep_inner_deg", "sweep_outer_deg", "root_chord_m",
                  "mid_chord_frac", "taper_outer", "centre_body_fraction", "span_m"],
        source="planform aerodynamic-centre estimate — VSPAERO x_np supersedes this"))
    add(DerivationNode(
        id="x_cg_target", label="CG for target static margin", symbol="x_cg",
        value=x_np - STATIC_MARGIN_TARGET * mac, unit="m",
        formula="x_cg = x_np − SM·c̄",
        substitution=f"{_g(x_np, 4)} − {STATIC_MARGIN_TARGET}·{_g(mac)} "
                     f"= {_g(x_np - STATIC_MARGIN_TARGET * mac, 4)} m",
        children=["x_np_m", "static_margin_target", "mac_m"],
        source="static-margin definition"))

    return DerivationGraph(roots=list(ROOTS), nodes=nodes)
