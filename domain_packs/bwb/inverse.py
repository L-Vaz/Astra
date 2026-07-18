"""BWB domain pack — requirements-first inverse sizing (tier 0).

Given DesignRequirements, deterministically choose the sizing parameters
(wing area via root chord, span via aspect ratio) while HOLDING the current
design's shape fractions, so a requirements change morphs the existing
design instead of resetting it. Closed-form + bounded fixed-point loops
throughout — microseconds, no optimizer, no LLM.

Infeasibility is never an error: the solver always returns the closest
feasible design plus the machine-readable binding constraint.

solve_planform() is the lens-independent generalization of the sizing
previously private to variants.py; variants._solve_planform now wraps it.
"""
import math
from typing import List, Optional

from pydantic import BaseModel, Field

from .aero_estimates import (LAUNCH_VSTALL_MAX, AeroEstimate, cl_max_3d,
                             estimate)
from .defaults import DEFAULT_AR, MAX_CRUISE_CL, isa_density
from .requirements import DesignRequirements, RequirementStatus
from .schema import BWBParams

MIN_TIP_CHORD = 0.042   # m; geometry gate is 0.040 — keep a hair of margin
_AR_GATE = (2.5, 10.0)  # BWBParams cross-check range
_LD_TOL = 0.05          # L/D target considered met within this
_CL_REL_TOL = 0.02      # CL equality target considered met within 2%
_VSTALL_TOL = 0.05      # m/s of slack before a stall cap counts as missed
_SPAN_TOL = 1e-6        # span cap is geometric — no engineering slack

# Served to the cockpit (bootstrap.requirements_meta) so the client-side
# "met" mirror can never disagree with _statuses(). Keys are the
# DesignRequirements field names; cl target is a RELATIVE tolerance.
MET_TOLERANCES = {"cl_cruise_target_rel": _CL_REL_TOL, "ld_min": _LD_TOL,
                  "v_stall_max_ms": _VSTALL_TOL, "span_max_m": _SPAN_TOL}

BINDING_LABELS = {
    "stall_at_launch": "launch-method stall tolerance",
    "stall_requirement": "your stall-speed requirement",
    "span_cap": "span cap",
    "ar_gate": "aspect-ratio gate (10)",
    "cruise_cl_cap": f"cruise-CL cap ({MAX_CRUISE_CL})",
    "physics_limit": "physics limit for this planform family",
}


class ShapeFractions(BaseModel):
    """The 10 non-sizing parameters of a BWBParams, held fixed while the
    solver chooses area and span."""
    centre_body_fraction: float
    mid_chord_frac: float
    taper_outer: float
    sweep_inner_deg: float
    sweep_outer_deg: float
    root_thickness_pct: float
    tip_thickness_pct: float
    root_twist_deg: float
    tip_twist_deg: float
    dihedral_deg: float

    @classmethod
    def from_params(cls, p: BWBParams) -> "ShapeFractions":
        return cls(**{k: getattr(p, k) for k in cls.model_fields})


# Mirrors the fixed defaults of defaults._planform_from (the mission baseline).
BASELINE_SHAPE = ShapeFractions(
    centre_body_fraction=0.30, mid_chord_frac=0.55, taper_outer=0.45,
    sweep_inner_deg=38.0, sweep_outer_deg=22.0,
    root_thickness_pct=14.0, tip_thickness_pct=9.0,
    root_twist_deg=2.0, tip_twist_deg=-3.0, dihedral_deg=2.0)


class InverseSolveResult(BaseModel):
    params: BWBParams
    panel: AeroEstimate
    requirements: List[RequirementStatus]
    binding_constraint: Optional[str] = Field(
        default=None, description="Machine key of the limit that stopped the solver, "
                                  "or None when every target was met")
    binding_explanation: str = ""
    notes: List[str] = Field(default_factory=list)


def solve_planform(s_ref: float, ar: float, shape: ShapeFractions,
                   span_cap: float = 3.0,
                   notes: Optional[List[str]] = None) -> BWBParams:
    """Build a valid BWBParams with `shape` proportions hitting s_ref exactly,
    clamping span/AR/tip-chord into the geometry gates.

    Lens-independent generalization of the former variants._solve_planform:
    identical clamps, taper relaxation, and note wording.
    """
    span_cap = min(span_cap, 3.0)
    span = math.sqrt(ar * s_ref)
    if span > span_cap:
        span = span_cap
        ar = span ** 2 / s_ref
        if notes is not None:
            reason = "schema" if span_cap >= 3.0 - 1e-9 else "span requirement"
            notes.append(f"Span capped at {span_cap:.1f} m ({reason}): AR falls to {ar:.2f}")
    if span < 0.8:
        span = 0.8
        ar = span ** 2 / s_ref

    cbf, mf = shape.centre_body_fraction, shape.mid_chord_frac
    taper = shape.taper_outer
    root_chord = s_ref  # overwritten in the loop
    for _ in range(20):
        b1 = cbf * span / 2.0
        b2 = span / 2.0 - b1
        k = b1 * (1 + mf) + b2 * (mf + mf * taper)
        root_chord = s_ref / k
        if root_chord * mf * taper >= MIN_TIP_CHORD or taper >= 0.7:
            break
        taper = min(0.7, taper + 0.05)  # widen the tip until manufacturable
    if taper != shape.taper_outer and notes is not None:
        notes.append(f"Taper relaxed to {taper:.2f} to keep tip chord above 42 mm")

    return BWBParams(
        span_m=span, root_chord_m=root_chord, centre_body_fraction=cbf,
        mid_chord_frac=mf, taper_outer=taper,
        sweep_inner_deg=shape.sweep_inner_deg, sweep_outer_deg=shape.sweep_outer_deg,
        root_thickness_pct=shape.root_thickness_pct,
        tip_thickness_pct=shape.tip_thickness_pct,
        root_twist_deg=shape.root_twist_deg, tip_twist_deg=shape.tip_twist_deg,
        dihedral_deg=shape.dihedral_deg)


def _feasible_planform(s: float, ar: float, shape: ShapeFractions,
                       span_cap: float, notes: Optional[List[str]]) -> BWBParams:
    """solve_planform with defensive schema-gate repairs for envelope corners
    (tiny or heavy-and-slow aircraft): the AR gate [2.5, 10] and the root-chord
    schema range [0.25, 1.2] m. Repairs trade AR first; area moves only when
    span is already pinned at a cap, and every area move is noted."""
    span_cap = min(span_cap, 3.0)
    ar = min(max(ar, _AR_GATE[0]), _AR_GATE[1])  # requested AR must respect the gate
    cbf, mf, taper = (shape.centre_body_fraction, shape.mid_chord_frac,
                      shape.taper_outer)

    def geometry_at(s_: float, ar_: float) -> tuple:
        span = min(max(math.sqrt(ar_ * s_), 0.8), span_cap)
        b1 = cbf * span / 2.0
        b2 = span / 2.0 - b1
        k = b1 * (1 + mf) + b2 * (mf + mf * taper)
        return span, k, s_ / k, span ** 2 / s_

    for _ in range(80):
        span, k, root, ar_actual = geometry_at(s, ar)
        if ar_actual > _AR_GATE[1]:           # minimum span met a tiny wing
            s = span ** 2 / (_AR_GATE[1] * 0.999)
            if notes is not None:
                notes.append(f"Wing grown to {s:.3f} m^2: AR gate (10) at the "
                             f"{span:.2f} m minimum span")
            continue
        if root > 1.2:                        # fat chord: raise AR, else cap area
            if span < span_cap - 1e-9 and ar < _AR_GATE[1] - 1e-9:
                ar = min(_AR_GATE[1], ar + 0.25)
                continue
            s = 1.2 * k * 0.999
            if notes is not None:
                notes.append(f"Wing area capped at {s:.3f} m^2 by the 1.2 m "
                             f"root-chord schema limit at {span:.2f} m span")
            continue
        if root < 0.25:                       # skinny chord: drop AR, else grow area
            if ar > _AR_GATE[0] + 1e-9:
                ar = max(_AR_GATE[0], ar - 0.25)
                continue
            s = 0.25 * k * 1.001
            if notes is not None:
                notes.append("Wing grown to keep the root chord above the "
                             "0.25 m schema minimum")
            continue
        break
    for _ in range(10):
        try:
            return solve_planform(s, ar, shape, span_cap, notes)
        except ValueError:
            # taper widening inside solve_planform shifted the root chord past
            # a bound by a hair — nudge area up and retry
            s *= 1.02
    return solve_planform(s, ar, shape, span_cap, notes)


def _statuses(req: DesignRequirements, est: AeroEstimate) -> List[RequirementStatus]:
    out: List[RequirementStatus] = []
    t = req.cl_cruise_target
    out.append(RequirementStatus(
        name="cl_cruise_target", label="Cruise CL", target=t,
        achieved=est.cl_cruise,
        met=None if t is None else abs(est.cl_cruise - t) <= _CL_REL_TOL * t,
        margin=None if t is None else -(abs(est.cl_cruise - t))))
    t = req.ld_min
    out.append(RequirementStatus(
        name="ld_min", label="Cruise L/D", target=t, achieved=est.ld_cruise,
        met=None if t is None else est.ld_cruise >= t - _LD_TOL,
        margin=None if t is None else est.ld_cruise - t))
    t = req.v_stall_max_ms
    out.append(RequirementStatus(
        name="v_stall_max_ms", label="Stall speed", unit="m/s", target=t,
        achieved=est.v_stall_ms,
        met=None if t is None else est.v_stall_ms <= t + _VSTALL_TOL,
        margin=None if t is None else t - est.v_stall_ms))
    t = req.span_max_m
    out.append(RequirementStatus(
        name="span_max_m", label="Span", unit="m", target=t, achieved=est.span_m,
        met=None if t is None else est.span_m <= t + _SPAN_TOL,
        margin=None if t is None else t - est.span_m))
    return out


def check_requirements(req: DesignRequirements,
                       params: BWBParams) -> List[RequirementStatus]:
    """Forward check only — no solving. Used when the user drags a geometry
    slider and the requirement chips must update live."""
    rho = isa_density(req.cruise_altitude_m)
    est = estimate(params, rho, req.cruise_speed_ms, req.mtow_kg * 9.81, req.launch)
    return _statuses(req, est)


def solve_requirements(req: DesignRequirements,
                       shape_from: Optional[BWBParams] = None) -> InverseSolveResult:
    """Deterministic inverse solve: requirements -> BWBParams + metric panel.

    Algorithm (each reused piece cited):
      1. Flight condition: rho from ISA, q, W; effective stall cap
         min(requirement, launch tolerance).
      2. Area at the working AR: S = W/(q*CL_target) when a CL target is set,
         else the CL* fixed point (the variants._size_wing loop).
      3. Area floors: stall 2W/(rho*v_cap^2*CLmax3d) and cruise-CL cap
         W/(q*MAX_CRUISE_CL) — both from variants._size_wing.
      4. If ld_min is unmet, raise AR in 0.25 steps while span stays under the
         cap and AR under the gate, re-running 2-3 (the cockpit goal-box loop,
         moved server-side to be the single source of truth).
      5. Name the binding constraint.
    """
    shape = ShapeFractions.from_params(shape_from) if shape_from else BASELINE_SHAPE
    rho = isa_density(req.cruise_altitude_m)
    v = req.cruise_speed_ms
    w = req.mtow_kg * 9.81
    q = 0.5 * rho * v ** 2
    launch_cap = LAUNCH_VSTALL_MAX[req.launch]
    stall_cap = min(req.v_stall_max_ms, launch_cap) if req.v_stall_max_ms else launch_cap
    span_cap = min(req.span_max_m, 3.0) if req.span_max_m else 3.0

    notes: List[str] = []
    ar = shape_from.aspect_ratio() if shape_from else DEFAULT_AR
    ar = min(max(ar, _AR_GATE[0]), _AR_GATE[1])

    def area_at(ar_try: float) -> tuple:
        """(S, floor_key) at one AR: target/CL* sizing, then hard floors."""
        if req.cl_cruise_target is not None:
            s = w / (q * req.cl_cruise_target)
        else:
            s = w / (q * 0.5)  # neutral start; fixed point converges in <=8
            params_i = _feasible_planform(s, ar_try, shape, span_cap, None)
            for _ in range(8):
                est_i = estimate(params_i, rho, v, w, req.launch)
                s_new = w / (q * est_i.cl_star)
                if abs(s_new - s) / s < 1e-3:
                    s = s_new
                    break
                s = s_new
                params_i = _feasible_planform(s, ar_try, shape, span_cap, None)
        s_min_cl = w / (q * MAX_CRUISE_CL)
        clmax = cl_max_3d(_feasible_planform(max(s, s_min_cl), ar_try, shape,
                                             span_cap, None))
        s_min_stall = 2.0 * w / (rho * stall_cap ** 2 * clmax)
        floor = None
        if max(s_min_cl, s_min_stall) > s:
            floor = "stall" if s_min_stall >= s_min_cl else "cl_cap"
            s = max(s_min_cl, s_min_stall)
        return s, floor

    s, floor = area_at(ar)
    params = _feasible_planform(s, ar, shape, span_cap, notes)
    est = estimate(params, rho, v, w, req.launch)

    # step 4 — spend span budget on AR for the L/D objective
    if req.ld_min is not None:
        for _ in range(40):
            if est.ld_cruise >= req.ld_min - _LD_TOL:
                break
            if ar >= _AR_GATE[1] - 1e-9 or params.span_m >= span_cap - 1e-3:
                break
            ar = min(_AR_GATE[1], ar + 0.25)  # never overshoot the AR gate
            s, floor = area_at(ar)
            params = _feasible_planform(s, ar, shape, span_cap, None)
            est = estimate(params, rho, v, w, req.launch)

    statuses = _statuses(req, est)
    unmet = [st for st in statuses if st.met is False]

    binding: Optional[str] = None
    explanation = "all requirements met" if not unmet else ""
    if unmet:
        if floor == "stall":
            if stall_cap == launch_cap and (req.v_stall_max_ms is None
                                            or req.v_stall_max_ms >= launch_cap):
                binding = "stall_at_launch"
                explanation = (f"{req.launch} launch tolerates stall <= {launch_cap} m/s "
                               f"— this floors the wing area and caps wing loading; "
                               f"a faster launch method would unlock more")
            else:
                binding = "stall_requirement"
                explanation = (f"your stall <= {stall_cap:.1f} m/s requirement floors "
                               f"the wing area — relax it for more L/D")
        elif params.span_m >= span_cap - 1e-3:
            binding = "span_cap"
            explanation = (f"span cap {span_cap:.2f} m binds — allowing more span "
                           f"would raise aspect ratio and L/D")
        elif ar >= _AR_GATE[1] - 1e-9:
            binding = "ar_gate"
            explanation = "the aspect-ratio gate (10) is reached — a schema limit for sane BWBs"
        elif floor == "cl_cap":
            binding = "cruise_cl_cap"
            explanation = (f"cruise CL is capped at {MAX_CRUISE_CL} to keep cruise away "
                           f"from stall — the wing cannot shrink further")
        else:
            binding = "physics_limit"
            explanation = ("no geometric limit binds — the targets exceed what this "
                           "planform family's drag physics can deliver")

    return InverseSolveResult(
        params=params, panel=est, requirements=statuses,
        binding_constraint=binding, binding_explanation=explanation, notes=notes)
