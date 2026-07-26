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
    duplicate_of: Optional[str] = Field(
        default=None,
        description=("Key of an earlier candidate this one is the SAME WING as — same "
                     "span and area to within 0.1 mm / 1% (S3-G5). None when this "
                     "candidate is a distinct design."))
    distinct_sizings: int = Field(
        default=0,
        description=("How many genuinely different wings this batch contains. Below "
                     "the number of cards, the constraints are doing the designing "
                     "and the gallery must say so."))


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
          objective="Cruise as close to the best-L/D lift coefficient as the stall "
                    "and cruise-CL limits allow: shrink the wing toward CL* and thin "
                    "the sections to cut parasite drag.",
          ar=6.5, centre_body_fraction=0.28, mid_chord_frac=0.55,
          taper_outer=0.50, sweep_inner_deg=34, sweep_outer_deg=20,
          root_thickness_pct=12, tip_thickness_pct=8,
          root_twist_deg=2, tip_twist_deg=-2, size_to_cl_star=True),
    _Lens(key="span_efficiency", title="Span efficiency",
          objective="Cut induced drag with a higher aspect ratio at the same "
                    "wing loading, where the span cap and the planform gates "
                    "allow; usually paid for in root bending and span.",
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
                    "for pitch authority; accept the L/D cost AND a higher stall "
                    "speed — this lens has the highest stall of the five in 65.9% "
                    "of conditions.",
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


class _Sizing(BaseModel):
    """What the sizing solve DECIDED, kept separate from what it will SAY.

    Provenance used to be written while sizing, from the values sizing had at that
    moment — before `_feasible_planform`'s schema repairs moved the area. The result
    was cards quoting numbers their own panel contradicted: "at the same wing loading"
    was false in 14.8% of conditions (50.0% at 10 kg), and "below baseline" was false
    in 56.0%. Recording the decision and rendering the sentence afterwards, from the
    FINAL panel, is what makes that class of untruth impossible (S3-G1h/G3h/G4h)."""
    strategy: str                      # baseline_loading | lighter_loading | cl_star
    requested_wing_loading: Optional[float] = None
    wing_loading_floor_applied: bool = False
    cl_star: Optional[float] = None
    s_unconstrained_m2: Optional[float] = None
    binder: Optional[str] = None       # what stopped it reaching the target, if any
    binder_reason: Optional[str] = None


def _size_wing(lens: _Lens, derived: MissionDerived,
               notes: List[str],
               span_cap: float = 3.0,
               stall_cap: Optional[float] = None) -> tuple:
    """(params, sizing record). Choose S for the lens: either baseline wing loading,
    or iterate S so cruise CL approaches CL*, bounded by the stall cap and
    MAX_CRUISE_CL.

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
            # deliberately lighter loading than the baseline table value — EXCEPT that
            # the 5.5 kg/m^2 floor raises it whenever the baseline is lighter than
            # 6.875, which is 56.0% of conditions. The floor itself is a defect and is
            # fixed in S6-G1; here the card simply stops claiming otherwise.
            target = 0.8 * derived.wing_loading_kgm2
            wl = max(5.5, target)
            s = derived.mtow_kg / wl
            sizing = _Sizing(strategy="lighter_loading",
                             requested_wing_loading=wl,
                             wing_loading_floor_applied=wl > target + 1e-12)
        else:
            sizing = _Sizing(strategy="baseline_loading",
                             requested_wing_loading=derived.wing_loading_kgm2)
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
        return _solve_planform(s, lens, notes, span_cap), sizing

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
    sizing = _Sizing(strategy="cl_star", cl_star=cl_star,
                     s_unconstrained_m2=s_unconstrained)

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
        sizing.binder, sizing.binder_reason = binder, reason
        notes.append(
            f"Range-optimal wing loading {derived.mtow_kg / s_unconstrained:.1f} kg/m^2 "
            f"is unreachable: {binder} constraint ({reason}) caps wing loading "
            f"at {derived.mtow_kg / s:.1f} kg/m^2.")
    return _solve_planform(s, lens, notes, span_cap), sizing


# how far cruise CL may sit from CL* before "sized for best L/D" is a false claim
CL_STAR_TOLERANCE = 0.02


def _sizing_provenance(lens: _Lens, sizing: _Sizing, panel: AeroEstimate,
                       base: AeroEstimate, derived: MissionDerived,
                       notes: List[str]) -> List[str]:
    """The sizing story, told from the FINAL panel (S3-G1h/G2h/G3h).

    Every comparison asserted here is evaluated against the numbers on the card, so a
    card cannot claim "below baseline" while its own wing loading is higher."""
    out: List[str] = []
    wing_loading = panel.wing_loading_kgm2

    if sizing.strategy == "lighter_loading":
        if wing_loading < base.wing_loading_kgm2 - 1e-9:
            out.append(
                f"Wing loading {wing_loading:.1f} kg/m^2, below the baseline's "
                f"{base.wing_loading_kgm2:.1f} -> S = {panel.s_ref_m2:.3f} m^2")
        elif sizing.wing_loading_floor_applied:
            # the 5.5 kg/m^2 floor is what raised it, not the lens's own intent
            out.append(
                f"Wing loading raised to the {sizing.requested_wing_loading:.1f} "
                f"kg/m^2 minimum practical value (the baseline's "
                f"{base.wing_loading_kgm2:.1f} kg/m^2 is lower); the design sits at "
                f"{wing_loading:.1f} kg/m^2 -> S = {panel.s_ref_m2:.3f} m^2")
            notes.append(
                "This lens cannot lower the stall speed here: the minimum practical "
                "wing loading is already above the baseline's, so the wing did not "
                "grow.")
        else:
            # asked for a lighter wing and did not get one — the planform gates moved
            # the area after sizing chose it
            out.append(
                f"Asked for {sizing.requested_wing_loading:.1f} kg/m^2, 20% under the "
                f"baseline's {base.wing_loading_kgm2:.1f}; after the planform gates "
                f"the design sits at {wing_loading:.1f} kg/m^2 -> S = "
                f"{panel.s_ref_m2:.3f} m^2")
            notes.append(
                f"This lens did not end up lighter than the baseline: the planform "
                f"gates moved the wing loading to {wing_loading:.1f} kg/m^2 against "
                f"the baseline's {base.wing_loading_kgm2:.1f}, so the stall speed is "
                f"not lower here.")
    elif sizing.strategy == "baseline_loading":
        if abs(wing_loading - base.wing_loading_kgm2) <= 0.05:
            out.append(
                f"Baseline wing loading kept: {wing_loading:.1f} kg/m^2 -> S = "
                f"{panel.s_ref_m2:.3f} m^2")
        else:
            # the schema-gate repairs moved the area after sizing chose it
            out.append(
                f"Started from the baseline wing loading "
                f"{base.wing_loading_kgm2:.1f} kg/m^2; after the planform gates the "
                f"design sits at {wing_loading:.1f} kg/m^2 -> S = "
                f"{panel.s_ref_m2:.3f} m^2")
            notes.append(
                f"Wing loading moved from the baseline's "
                f"{base.wing_loading_kgm2:.1f} to {wing_loading:.1f} kg/m^2 when the "
                f"planform was repaired to fit the schema gates.")
    else:  # cl_star
        out.append(
            f"Best-L/D lift coefficient CL* = sqrt(pi*e*AR*CD0) = "
            f"{panel.cl_star:.3f}")
        miss = (abs(panel.cl_cruise - panel.cl_star) / panel.cl_star
                if panel.cl_star else 1.0)
        if miss <= CL_STAR_TOLERANCE:
            out.append(
                f"Wing sized so cruise CL = CL*: S = {panel.s_ref_m2:.3f} m^2 "
                f"(wing loading {panel.wing_loading_kgm2:.1f} kg/m^2, achieved CL "
                f"{panel.cl_cruise:.3f})")
        else:
            # The method is right; the CLAIM was unconditional. Measured: the lens
            # reaches CL* within 2% in only 16.2% of conditions (S3-G2h).
            out.append(
                f"Target wing sized for CL = CL* ({panel.cl_star:.3f}); achieved CL "
                f"{panel.cl_cruise:.3f} at S = {panel.s_ref_m2:.3f} m^2 "
                f"(wing loading {panel.wing_loading_kgm2:.1f} kg/m^2)")
            binder = sizing.binder or "the planform gates"
            reason = sizing.binder_reason or "the wing could not be sized freely"
            # The retraction is MANDATORY, not hoped for: without it the card claims
            # an objective it did not meet.
            notes.append(
                f"Cruise CL {panel.cl_cruise:.3f} is {miss * 100:.0f}% away from CL* "
                f"{panel.cl_star:.3f} — {binder} bound the wing ({reason}), so this "
                f"design does not cruise at its best-L/D point.")
    return out


# ---------------------------------------------------------------------------
# trade-off sentences (mechanical panel comparison, no LLM)
# ---------------------------------------------------------------------------

def _pct(new: float, old: float) -> float:
    return 100.0 * (new - old) / old if old else 0.0


# Thresholds are compared with a tolerance so +x and -x behave identically. Exact-
# threshold behaviour used to be float-dependent and asymmetric: a -10 mm neutral-point
# shift and a -0.10 m span change were silently dropped while their positive twins were
# reported (S3-G6b).
_EPS = 1e-9


def _over(value: float, threshold: float) -> bool:
    return abs(value) >= threshold - _EPS


def tradeoff_sentences(panel: AeroEstimate, base: AeroEstimate) -> List[str]:
    out: List[str] = []
    d_ld = _pct(panel.ld_cruise, base.ld_cruise)
    if _over(d_ld, 2):
        out.append(f"{'+' if d_ld > 0 else ''}{d_ld:.0f}% cruise L/D vs baseline "
                   f"({panel.ld_cruise:.1f} vs {base.ld_cruise:.1f})")
    d_stall = panel.v_stall_ms - base.v_stall_ms
    if _over(d_stall, 0.3):
        harder = "harder" if d_stall > 0 else "easier"
        out.append(f"stall {'+' if d_stall > 0 else ''}{d_stall:.1f} m/s "
                   f"({panel.v_stall_ms:.1f} m/s — {harder} launch)")
    if panel.launch_ok is False:
        # :.1f, not :.0f — the hand limit is 10.5, and round-half-even rendered it as
        # "the 10 m/s launch limit" directly beneath a "stall 10.50 m/s" sentence on
        # the same card (S3-G6a). Catapult and runway are integers, so a test using
        # only those would never have caught it.
        out.append(f"stall exceeds the {panel.launch_limit_ms:.1f} m/s launch limit "
                   f"— needs a faster launch method")
    # Root bending is weight*span/8 and weight is identical across candidates, so
    # bending %-change IS span %-change exactly. Two sentences said the same thing on
    # two different thresholds; this is one sentence with both consequences (S3-G6e).
    d_span = panel.span_m - base.span_m
    d_bend = _pct(panel.root_bending_index_nm, base.root_bending_index_nm)
    if _over(d_bend, 5):
        out.append(f"span {'+' if d_span > 0 else ''}{d_span:.2f} m "
                   f"({panel.span_m:.2f} m — transport/handling), so root bending "
                   f"{'+' if d_bend > 0 else ''}{d_bend:.0f}% "
                   f"({'heavier' if d_bend > 0 else 'lighter'} spar)")
    d_np = panel.x_np_m - base.x_np_m
    if _over(d_np, 0.01):
        out.append(f"neutral point {'aft' if d_np > 0 else 'forward'} "
                   f"{abs(d_np) * 1000:.0f} mm ({'more' if d_np > 0 else 'less'} "
                   f"pitch-stability room)")
    d_wash = panel.washout_deg - base.washout_deg
    if _over(d_wash, 0.5):
        out.append(f"washout {'+' if d_wash > 0 else ''}{d_wash:.1f} deg "
                   f"({'more docile stall, some lift lost at the tip' if d_wash > 0 else 'less twist drag, sharper stall'})")
    # COMPARATIVE, not absolute (S3-G6c): the old test fired whenever tip Re was low,
    # which is nearly every small UAV — including when the baseline was equally low —
    # and that made the "within a few percent" fallback unreachable for them. The
    # absolute warning still exists, in constraint_notes, where an absolute warning
    # belongs (see absolute_notes()).
    if panel.tip_re < _TIP_RE_WARN <= base.tip_re:
        out.append(f"tip Reynolds {panel.tip_re:.3g} below {_TIP_RE_WARN:.1e} "
                   f"(baseline {base.tip_re:.3g}) — laminar-separation risk at the tip")
    if not out:
        out.append("within a few percent of the baseline on every tracked metric")
    return out


def absolute_notes(panel: AeroEstimate) -> List[str]:
    """Checks that are true of a design on its own terms, with no baseline involved.

    These run on EVERY candidate including the baseline, whose tradeoffs are a fixed
    placeholder — so a baseline that exceeds its own launch limit used to be flagged
    nowhere at all (S3-G6f)."""
    out: List[str] = []
    if panel.launch_ok is False:
        out.append(f"Stall {panel.v_stall_ms:.1f} m/s exceeds the "
                   f"{panel.launch_limit_ms:.1f} m/s limit for this launch method.")
    if panel.tip_re < _TIP_RE_WARN:
        # :.3g — at :.2g the value rounded UP to the threshold it claims to be below,
        # printing "tip Reynolds 1.5e+05 below 1.5e+05" (S3-G6d).
        out.append(f"Tip Reynolds {panel.tip_re:.3g} is below {_TIP_RE_WARN:.1e}: "
                   f"laminar-separation risk at the tip.")
    if panel.cl_cruise > panel.cl_max_3d:
        out.append(f"Cruise CL {panel.cl_cruise:.3f} exceeds CLmax "
                   f"{panel.cl_max_3d:.3f} — this design cannot fly at this speed.")
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
        # The placeholder stays — comparisons against itself are meaningless — but the
        # ABSOLUTE checks now run on it too (S3-G6f).
        constraint_notes=absolute_notes(base_panel),
        tradeoffs=["reference design — all comparisons are against this"],
        is_baseline=True)

    out = [baseline]
    for lens in LENSES:
        notes: List[str] = []
        params, sizing = _size_wing(lens, derived, notes, span_cap, stall_cap)
        panel = estimate(params, atmosphere, v, w, launch)
        # Provenance is composed AFTER the panel exists, so every number it quotes is
        # a number on this card (S3-G3h/G4h).
        provenance = ([lens.objective]
                      + _sizing_provenance(lens, sizing, panel, base_panel, derived,
                                           notes)
                      + _drag_provenance(panel))
        if lens.key == "span_efficiency" and panel.span_m <= base_panel.span_m + 1e-9:
            # "pay for it in span" is false in 33.8% of conditions — the span cap or
            # the AR gate can leave this lens SMALLER than the baseline (S3-G3h).
            notes.append(
                f"Span did not grow here: {panel.span_m:.2f} m against the baseline's "
                f"{base_panel.span_m:.2f} m, so this lens buys its span efficiency "
                f"from proportions rather than from extra span.")
        out.append(DesignCandidate(
            key=lens.key, title=lens.title, objective=lens.objective,
            params=params, panel=panel, provenance=provenance,
            constraint_notes=notes + absolute_notes(panel),
            tradeoffs=tradeoff_sentences(panel, base_panel)))
    return _report_degeneracy(out)


# Two candidates are "the same wing" when the sizing solve landed them in the same
# place: span within 0.1 mm and area within 1%. Deliberately NOT the 12-parameter
# vector — sweep, thickness and twist always differ between lenses, so the full-
# parameter distance never drops below 0.15 and cannot detect the collapse (S3-G5).
_SPAN_TOL_M = 1e-4
_AREA_TOL_FRAC = 0.01


def _same_sizing(a: DesignCandidate, b: DesignCandidate) -> bool:
    if abs(a.panel.span_m - b.panel.span_m) > _SPAN_TOL_M:
        return False
    return abs(a.panel.s_ref_m2 - b.panel.s_ref_m2) <= _AREA_TOL_FRAC * b.panel.s_ref_m2


def _report_degeneracy(candidates: List[DesignCandidate]) -> List[DesignCandidate]:
    """Mark candidates that are the same wing as an earlier one (S3-G5).

    Measured over 2880 conditions: 10.0% collapse to ONE distinct sizing (all at
    span_cap 1.2 m), 2.7% to two, 5.7% to three. Concretely, mtow 0.8 / alt 4000 /
    v 10 / span_cap 1.2 puts baseline, max_range, span_efficiency and control_margin
    at S = 0.5754, b = 1.200, AR = 2.50 — four cards, one design, presented as four
    choices with nothing saying otherwise."""
    for index, candidate in enumerate(candidates):
        for earlier in candidates[:index]:
            if earlier.duplicate_of is None and _same_sizing(candidate, earlier):
                candidate.duplicate_of = earlier.key
                candidate.constraint_notes.append(
                    f"Same wing as '{earlier.title}': span {candidate.panel.span_m:.3f} m "
                    f"and area {candidate.panel.s_ref_m2:.4f} m^2 match it. The "
                    f"constraints are doing the designing here, not this lens.")
                break
    distinct = sum(1 for c in candidates if c.duplicate_of is None)
    for candidate in candidates:
        candidate.distinct_sizings = distinct
    return candidates
