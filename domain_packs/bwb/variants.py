"""BWB domain pack — lens-based design candidate generation (tier 0).

Instead of one fixed starting geometry, generate several named candidates,
each solving the same sizing equations under a different objective ("lens"):
max range, span efficiency, easy launch, control margin. Every candidate
carries its derivation as plain-language provenance, an equation-level metric
panel (aero_estimates), the constraints that bound it, and mechanical
trade-off sentences against the baseline.

Deterministic and closed-form throughout — no LLM, no solver. Candidates are
*hypotheses*: VSPAERO screening (tier 1) and OpenFOAM (tier 2) judge them.
"""
import math
from typing import Callable, List, Optional

from pydantic import BaseModel, Field

from .aero_estimates import (AeroEstimate, LAUNCH_VSTALL_MAX, cl_max_3d,
                             estimate)
from .defaults import MAX_CRUISE_CL
from .schema import BWBParams, MissionDerived

_MIN_TIP_CHORD = 0.042  # m; geometry gate is 0.040 — keep a hair of margin
_TIP_RE_WARN = 1.5e5


class DesignCandidate(BaseModel):
    key: str
    title: str
    objective: str
    params: BWBParams
    panel: AeroEstimate
    provenance: List[str] = Field(default_factory=list,
                                  description="How the numbers led to this shape")
    constraint_notes: List[str] = Field(default_factory=list,
                                        description="Which limits bound the design")
    tradeoffs: List[str] = Field(default_factory=list,
                                 description="Mechanical comparison vs baseline")
    is_baseline: bool = False


class _Lens(BaseModel):
    """Shape recipe + sizing behaviour for one objective."""
    key: str
    title: str
    objective: str
    ar: float
    centre_body_fraction: float
    mid_chord_frac: float
    taper_outer: float
    sweep_inner_deg: float
    sweep_outer_deg: float
    root_thickness_pct: float
    tip_thickness_pct: float
    root_twist_deg: float
    tip_twist_deg: float
    dihedral_deg: float = 2.0
    size_to_cl_star: bool = False   # else keep the baseline wing loading


LENSES: List[_Lens] = [
    _Lens(key="max_range", title="Max range",
          objective="Cruise at the best-L/D lift coefficient: shrink the wing "
                    "toward CL* and thin the sections to cut parasite drag.",
          ar=6.5, centre_body_fraction=0.28, mid_chord_frac=0.55,
          taper_outer=0.50, sweep_inner_deg=34, sweep_outer_deg=20,
          root_thickness_pct=12, tip_thickness_pct=8,
          root_twist_deg=2, tip_twist_deg=-2, size_to_cl_star=True),
    _Lens(key="span_efficiency", title="Span efficiency",
          objective="Cut induced drag with a higher aspect ratio at the same "
                    "wing loading; pay for it in root bending and span.",
          ar=8.5, centre_body_fraction=0.24, mid_chord_frac=0.55,
          taper_outer=0.45, sweep_inner_deg=32, sweep_outer_deg=18,
          root_thickness_pct=13, tip_thickness_pct=8.5,
          root_twist_deg=2, tip_twist_deg=-3),
    _Lens(key="easy_launch", title="Easy launch",
          objective="Lowest stall speed: bigger wing, modest aspect ratio, "
                    "extra washout for a docile departure.",
          ar=5.0, centre_body_fraction=0.32, mid_chord_frac=0.58,
          taper_outer=0.55, sweep_inner_deg=34, sweep_outer_deg=18,
          root_thickness_pct=14, tip_thickness_pct=9,
          root_twist_deg=2, tip_twist_deg=-4, size_to_cl_star=False),
    _Lens(key="control_margin", title="Control margin",
          objective="Move the neutral point aft with sweep and add washout "
                    "for pitch authority; accept the L/D cost.",
          ar=5.5, centre_body_fraction=0.30, mid_chord_frac=0.55,
          taper_outer=0.50, sweep_inner_deg=44, sweep_outer_deg=28,
          root_thickness_pct=15, tip_thickness_pct=9,
          root_twist_deg=3, tip_twist_deg=-5),
]


# ---------------------------------------------------------------------------
# planform construction under gates
# ---------------------------------------------------------------------------

def _solve_planform(s_ref: float, lens: _Lens,
                    notes: Optional[List[str]] = None) -> BWBParams:
    """Build a valid BWBParams with lens proportions hitting s_ref exactly,
    clamping span/AR/tip-chord into the geometry gates."""
    ar = lens.ar
    span = math.sqrt(ar * s_ref)
    if span > 3.0:
        span = 3.0
        ar = span ** 2 / s_ref
        if notes is not None:
            notes.append(f"Span capped at 3.0 m (schema): AR falls to {ar:.2f}")
    if span < 0.8:
        span = 0.8
        ar = span ** 2 / s_ref

    cbf, mf = lens.centre_body_fraction, lens.mid_chord_frac
    taper = lens.taper_outer
    for _ in range(20):
        b1 = cbf * span / 2.0
        b2 = span / 2.0 - b1
        k = b1 * (1 + mf) + b2 * (mf + mf * taper)
        root_chord = s_ref / k
        if root_chord * mf * taper >= _MIN_TIP_CHORD or taper >= 0.7:
            break
        taper = min(0.7, taper + 0.05)  # widen the tip until manufacturable
    if taper != lens.taper_outer and notes is not None:
        notes.append(f"Taper relaxed to {taper:.2f} to keep tip chord above 42 mm")

    return BWBParams(
        span_m=span, root_chord_m=root_chord, centre_body_fraction=cbf,
        mid_chord_frac=mf, taper_outer=taper,
        sweep_inner_deg=lens.sweep_inner_deg, sweep_outer_deg=lens.sweep_outer_deg,
        root_thickness_pct=lens.root_thickness_pct,
        tip_thickness_pct=lens.tip_thickness_pct,
        root_twist_deg=lens.root_twist_deg, tip_twist_deg=lens.tip_twist_deg,
        dihedral_deg=lens.dihedral_deg)


def _size_wing(lens: _Lens, derived: MissionDerived,
               provenance: List[str], notes: List[str]) -> BWBParams:
    """Choose S for the lens: either baseline wing loading, or iterate S so
    cruise CL approaches CL*, bounded by stall-at-launch and MAX_CRUISE_CL."""
    rho = derived.air_density_kgm3
    v = derived.mission.cruise_speed_ms
    w = derived.weight_n
    q = 0.5 * rho * v ** 2
    launch = derived.mission.launch

    s = derived.s_ref_target_m2
    if not lens.size_to_cl_star:
        if lens.key == "easy_launch":
            # deliberately lighter loading than the baseline table value
            wl = max(5.5, 0.8 * derived.wing_loading_kgm2)
            s = derived.mtow_kg / wl
            provenance.append(
                f"Wing loading set to {wl:.1f} kg/m^2 (below baseline "
                f"{derived.wing_loading_kgm2:.1f}) -> S = {s:.3f} m^2")
        else:
            provenance.append(
                f"Baseline wing loading kept: {derived.wing_loading_kgm2:.1f} kg/m^2 "
                f"-> S = {s:.3f} m^2")
        return _solve_planform(s, lens, notes)

    # --- size toward CL*: S and CL* are coupled through CD0, so iterate ---
    params = _solve_planform(s, lens, None)
    cl_star = 0.0
    for _ in range(8):
        est = estimate(params, rho, v, w, launch)
        cl_star = est.cl_star
        s_new = w / (q * cl_star)
        if abs(s_new - s) / s < 1e-3:
            s = s_new
            break
        s = s_new
        params = _solve_planform(s, lens, None)
    s_unconstrained = s
    provenance.append(
        f"Best-L/D lift coefficient CL* = sqrt(pi*e*AR*CD0) = {cl_star:.3f}")
    provenance.append(
        f"Wing sized so cruise CL = CL*: S = W/(q*CL*) = {s_unconstrained:.3f} m^2 "
        f"(wing loading {derived.mtow_kg / s_unconstrained:.1f} kg/m^2)")

    # constraints: cruise CL cap and stall speed at launch
    s_min_cl = w / (q * MAX_CRUISE_CL)
    clmax = cl_max_3d(params)
    v_cap = LAUNCH_VSTALL_MAX[launch]
    s_min_stall = 2.0 * w / (rho * v_cap ** 2 * clmax)
    s_bound = max(s_min_cl, s_min_stall)
    if s_bound > s:
        binder = "stall-at-launch" if s_min_stall >= s_min_cl else "cruise-CL cap"
        s = s_bound
        notes.append(
            f"Range-optimal wing loading {derived.mtow_kg / s_unconstrained:.1f} kg/m^2 "
            f"is unreachable: {binder} constraint ({launch} launch tolerates stall "
            f"<= {v_cap} m/s) caps wing loading at {derived.mtow_kg / s:.1f} kg/m^2. "
            f"A faster launch method would unlock the rest.")
    return _solve_planform(s, lens, notes)


# ---------------------------------------------------------------------------
# trade-off sentences (mechanical panel comparison, no LLM)
# ---------------------------------------------------------------------------

def _pct(new: float, old: float) -> float:
    return 100.0 * (new - old) / old if old else 0.0


def tradeoff_sentences(panel: AeroEstimate, base: AeroEstimate) -> List[str]:
    out: List[str] = []
    d_ld = _pct(panel.ld_cruise, base.ld_cruise)
    if abs(d_ld) >= 2:
        out.append(f"{'+' if d_ld > 0 else ''}{d_ld:.0f}% cruise L/D vs baseline "
                   f"({panel.ld_cruise:.1f} vs {base.ld_cruise:.1f})")
    d_stall = panel.v_stall_ms - base.v_stall_ms
    if abs(d_stall) >= 0.3:
        harder = "harder" if d_stall > 0 else "easier"
        out.append(f"stall {'+' if d_stall > 0 else ''}{d_stall:.1f} m/s "
                   f"({panel.v_stall_ms:.1f} m/s — {harder} launch)")
    if panel.launch_ok is False:
        out.append(f"stall exceeds the {panel.launch_limit_ms:.0f} m/s launch limit "
                   f"— needs a faster launch method")
    d_bend = _pct(panel.root_bending_index_nm, base.root_bending_index_nm)
    if abs(d_bend) >= 5:
        out.append(f"root bending {'+' if d_bend > 0 else ''}{d_bend:.0f}% "
                   f"({'heavier' if d_bend > 0 else 'lighter'} spar)")
    d_np = panel.x_np_m - base.x_np_m
    if abs(d_np) >= 0.01:
        out.append(f"neutral point {'aft' if d_np > 0 else 'forward'} "
                   f"{abs(d_np) * 1000:.0f} mm ({'more' if d_np > 0 else 'less'} "
                   f"pitch-stability room)")
    d_wash = panel.washout_deg - base.washout_deg
    if abs(d_wash) >= 0.5:
        out.append(f"washout {'+' if d_wash > 0 else ''}{d_wash:.1f} deg "
                   f"({'more docile stall, some lift lost at the tip' if d_wash > 0 else 'less twist drag, sharper stall'})")
    if panel.tip_re < _TIP_RE_WARN:
        out.append(f"tip Reynolds {panel.tip_re:.2g} below {_TIP_RE_WARN:.1e} "
                   f"— laminar-separation risk at the tip")
    d_span = panel.span_m - base.span_m
    if abs(d_span) >= 0.1:
        out.append(f"span {'+' if d_span > 0 else ''}{d_span:.2f} m "
                   f"({panel.span_m:.2f} m — transport/handling)")
    if not out:
        out.append("within a few percent of the baseline on every tracked metric")
    return out


# ---------------------------------------------------------------------------
# candidate generation
# ---------------------------------------------------------------------------

def _drag_provenance(panel: AeroEstimate) -> List[str]:
    return [
        f"Parasite drag CD0 = {panel.cd0:.4f} (wetted-area buildup at "
        f"Re = {panel.reynolds_mac:.3g})",
        f"Induced drag k = 1/(pi*e*AR), e = {panel.oswald_e:.3f}, AR = {panel.aspect_ratio:.2f}",
        f"Cruise CL = W/(q*S) = {panel.cl_cruise:.3f} -> L/D = {panel.ld_cruise:.1f} "
        f"(best possible L/D for this shape: {panel.ld_max:.1f} at CL* = {panel.cl_star:.3f})",
        f"Stall {panel.v_stall_ms:.1f} m/s at CLmax(3D) = {panel.cl_max_3d:.2f}",
        f"Neutral point estimate x_np = {panel.x_np_m:.3f} m; CG for "
        f"8% static margin at x = {panel.x_cg_for_target_sm_m:.3f} m",
    ]


def generate_candidates(derived: MissionDerived) -> List[DesignCandidate]:
    """Baseline + one candidate per lens, each scored on the same panel."""
    rho = derived.air_density_kgm3
    v = derived.mission.cruise_speed_ms
    w = derived.weight_n
    launch = derived.mission.launch

    base_panel = estimate(derived.params, rho, v, w, launch)
    baseline = DesignCandidate(
        key="baseline", title="Baseline",
        objective="The deterministic sizing default: a physically reasonable "
                  "starting geometry, not an optimum.",
        params=derived.params, panel=base_panel,
        provenance=list(derived.assumptions) + _drag_provenance(base_panel),
        tradeoffs=["reference design — all comparisons are against this"],
        is_baseline=True)

    out = [baseline]
    for lens in LENSES:
        provenance: List[str] = [lens.objective]
        notes: List[str] = []
        params = _size_wing(lens, derived, provenance, notes)
        panel = estimate(params, rho, v, w, launch)
        provenance += _drag_provenance(panel)
        out.append(DesignCandidate(
            key=lens.key, title=lens.title, objective=lens.objective,
            params=params, panel=panel, provenance=provenance,
            constraint_notes=notes,
            tradeoffs=tradeoff_sentences(panel, base_panel)))
    return out
