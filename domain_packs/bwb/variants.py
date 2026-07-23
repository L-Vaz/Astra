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
from typing import Callable, List, Optional

from pydantic import BaseModel, Field

from .aero_estimates import (AeroEstimate, LAUNCH_VSTALL_MAX, cl_max_3d,
                             estimate)
from .defaults import MAX_CRUISE_CL
from .inverse import MIN_TIP_CHORD as _MIN_TIP_CHORD  # noqa: F401 (re-export)
from .inverse import ShapeFractions, _feasible_planform, solve_planform  # noqa: F401
from .requirements import DesignRequirements
from .schema import BWBParams, MissionDerived

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
                    notes: Optional[List[str]] = None,
                    span_cap: float = 3.0) -> BWBParams:
    """Build a valid BWBParams with lens proportions hitting s_ref exactly.
    Uses inverse._feasible_planform (solve_planform plus defensive schema-gate
    repairs) so envelope corners — tiny or heavy-and-slow aircraft — yield the
    closest valid planform with a note instead of a ValidationError."""
    shape = ShapeFractions(**{k: getattr(lens, k) for k in ShapeFractions.model_fields})
    return _feasible_planform(s_ref, lens.ar, shape, span_cap, notes)


def _size_wing(lens: _Lens, derived: MissionDerived,
               provenance: List[str], notes: List[str],
               span_cap: float = 3.0,
               stall_cap: Optional[float] = None) -> BWBParams:
    """Choose S for the lens: either baseline wing loading, or iterate S so
    cruise CL approaches CL*, bounded by the stall cap and MAX_CRUISE_CL.

    span_cap / stall_cap carry the user's vehicle constraints (stage-1 intake
    or requirements store); the effective stall cap is the tighter of the
    user's requirement and the launch-method tolerance."""
    atmosphere = derived.atmosphere
    rho = atmosphere.density_kgm3
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
        # wing-loading lenses must still honour the stall floor — a user's
        # stall requirement binds every candidate, not just CL*-sized ones
        params0 = _solve_planform(s, lens, None, span_cap)
        launch_cap = LAUNCH_VSTALL_MAX[launch]
        v_cap = min(launch_cap, stall_cap) if stall_cap else launch_cap
        s_floor = max(2.0 * w / (rho * v_cap ** 2 * cl_max_3d(params0)),
                      w / (q * MAX_CRUISE_CL))
        if s_floor > s:
            notes.append(f"Wing grown to {s_floor:.3f} m^2 to honour "
                         f"stall <= {v_cap:g} m/s")
            s = s_floor
        return _solve_planform(s, lens, notes, span_cap)

    # --- size toward CL*: S and CL* are coupled through CD0, so iterate ---
    params = _solve_planform(s, lens, None, span_cap)
    cl_star = 0.0
    for _ in range(8):
        est = estimate(params, atmosphere, v, w, launch)
        cl_star = est.cl_star
        s_new = w / (q * cl_star)
        if abs(s_new - s) / s < 1e-3:
            s = s_new
            break
        s = s_new
        params = _solve_planform(s, lens, None, span_cap)
    s_unconstrained = s
    provenance.append(
        f"Best-L/D lift coefficient CL* = sqrt(pi*e*AR*CD0) = {cl_star:.3f}")
    provenance.append(
        f"Wing sized so cruise CL = CL*: S = W/(q*CL*) = {s_unconstrained:.3f} m^2 "
        f"(wing loading {derived.mtow_kg / s_unconstrained:.1f} kg/m^2)")

    # constraints: cruise CL cap and the effective stall cap
    s_min_cl = w / (q * MAX_CRUISE_CL)
    clmax = cl_max_3d(params)
    launch_cap = LAUNCH_VSTALL_MAX[launch]
    v_cap = min(launch_cap, stall_cap) if stall_cap else launch_cap
    s_min_stall = 2.0 * w / (rho * v_cap ** 2 * clmax)
    s_bound = max(s_min_cl, s_min_stall)
    if s_bound > s:
        if s_min_stall >= s_min_cl:
            binder = ("stall-at-launch" if v_cap == launch_cap
                      else "your stall requirement")
            reason = (f"{launch} launch tolerates stall <= {launch_cap} m/s"
                      if v_cap == launch_cap
                      else f"you require stall <= {v_cap} m/s")
        else:
            binder, reason = "cruise-CL cap", f"cruise CL capped at {MAX_CRUISE_CL}"
        s = s_bound
        notes.append(
            f"Range-optimal wing loading {derived.mtow_kg / s_unconstrained:.1f} kg/m^2 "
            f"is unreachable: {binder} constraint ({reason}) caps wing loading "
            f"at {derived.mtow_kg / s:.1f} kg/m^2.")
    return _solve_planform(s, lens, notes, span_cap)


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


def generate_candidates(derived: MissionDerived,
                        requirements: Optional["DesignRequirements"] = None,
                        ) -> List[DesignCandidate]:
    """Baseline + one candidate per lens, each scored on the same panel.

    `requirements` carries the user's vehicle constraints (span/stall caps)
    so every lens is sized inside the same box the stage-1 feasibility check
    used — without it, lenses would silently fall back to schema-wide caps."""
    atmosphere = derived.atmosphere
    v = derived.mission.cruise_speed_ms
    w = derived.weight_n
    launch = derived.mission.launch
    span_cap = min(requirements.span_max_m, 3.0) \
        if requirements and requirements.span_max_m else 3.0
    stall_cap = requirements.v_stall_max_ms if requirements else None

    base_panel = estimate(derived.params, atmosphere, v, w, launch)
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
        params = _size_wing(lens, derived, provenance, notes, span_cap, stall_cap)
        panel = estimate(params, atmosphere, v, w, launch)
        provenance += _drag_provenance(panel)
        out.append(DesignCandidate(
            key=lens.key, title=lens.title, objective=lens.objective,
            params=params, panel=panel, provenance=provenance,
            constraint_notes=notes,
            tradeoffs=tradeoff_sentences(panel, base_panel)))
    return out
