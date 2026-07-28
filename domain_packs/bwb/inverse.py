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
                             reynolds_at,
                             estimate)
from .atmosphere import atmosphere_at
from .constants import VSTALL_TOL_MS
from .defaults import DEFAULT_AR, MAX_CRUISE_CL
from .requirements import DesignRequirements, RequirementStatus
from .schema import (DEFAULT_MID_AIRFOIL, DEFAULT_ROOT_AIRFOIL,
                     DEFAULT_TIP_AIRFOIL, BWBParams)

MIN_TIP_CHORD = 0.042   # m; geometry gate is 0.040 — keep a hair of margin
_AR_GATE = (2.5, 10.0)  # BWBParams cross-check range
_LD_TOL = 0.05          # L/D target considered met within this
_CL_REL_TOL = 0.02      # CL equality target considered met within 2%
_VSTALL_TOL = VSTALL_TOL_MS   # shared with AeroEstimate.launch_ok (constants.py)
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


class CapViolation(BaseModel):
    """A hard requirement the geometry could not satisfy (S4-C1/C4).

    The point of this type is that there is NO THIRD OUTCOME. A design either meets
    the user's cap or carries one of these saying, in numbers, by how much it does not
    and what stopped it. Before Stage 4 the third outcome was the normal one: the wing
    was grown to honour a stall cap, the AR gate shrank it back, and the candidate
    shipped with `passed: True` and two adjacent notes contradicting each other.

    Some caps are simply unreachable. Measured at a 0.9 m span cap and 20 kg MTOW, the
    stall cap needs 2.569 m^2 of wing while the largest wing the schema permits at that
    span is 0.324 m^2 — a factor of eight. No repair can fix that; only saying so can.
    """
    cap: str                     # "stall" | "span"
    required: float
    achieved: float
    unit: str
    blocked_by: str              # the gate or limit that made it impossible
    message: str


class LockedValues(BaseModel):
    """Quantities the solver may not move (S1-F).

    A CAP and a LOCK are different instructions and were previously conflated. The
    intake's `span_max` row is a cap: "no wider than this", and the solver is free to
    come in under it — which it usually does. A LOCK says "this is the number", because
    the operator has a fuselage, a print bed, or a wing already built.

    Before S1-F the lock toggles were display-only, and said so: the intake's own field
    description recorded that "the solver produces byte-identical derived values with
    every row 'fixed' and with every row 'variable'". They now constrain the solve.

    THE RULE THAT MATTERS: a lock that cannot be honoured produces a `CapViolation`, it
    never silently unlocks. Locks conflict easily — pinning both span and area pins
    aspect ratio too, and the schema's AR gate [2.5, 10] and root-chord range then have
    nowhere to go. That conflict is information the operator needs, not something to
    resolve on their behalf.
    """
    span_m: Optional[float] = Field(
        default=None, description="Exact span, m. Frees area and aspect ratio.")
    s_ref_m2: Optional[float] = Field(
        default=None, description="Exact reference area, m^2. Frees span and AR.")
    mtow_kg: Optional[float] = Field(
        default=None,
        description="Exact takeoff mass, kg. Stops the assumed MTOW following the "
                    "payload row, which is the only thing it silently does today.")

    @property
    def any_locked(self) -> bool:
        return any(v is not None for v in
                   (self.span_m, self.s_ref_m2, self.mtow_kg))


LOCK_TOL = {"span_m": 1e-3, "s_ref_m2": 1e-4}
"""How close counts as honoured. 1 mm of span and 1 cm^2 of area — finer than either
can be built to, and coarser than the solver's own convergence."""


class ShapeFractions(BaseModel):
    """The 10 non-sizing parameters of a BWBParams, held fixed while the
    solver chooses area and span."""
    centre_body_fraction: float
    mid_chord_frac: float
    taper_outer: float
    sweep_inner_deg: float
    sweep_outer_deg: float
    # S1-C: the thickness pair became the section triple. Thickness is no longer a
    # free number the solver could carry independently of the aerofoil.
    root_airfoil: str
    mid_airfoil: str
    tip_airfoil: str
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
    root_airfoil=DEFAULT_ROOT_AIRFOIL, mid_airfoil=DEFAULT_MID_AIRFOIL,
    tip_airfoil=DEFAULT_TIP_AIRFOIL,
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
    lock_violations: List[CapViolation] = Field(
        default_factory=list,
        description="Locks the geometry could not honour (S1-F). Empty is the normal "
                    "case; a non-empty list means the operator asked for something "
                    "this planform family cannot deliver, stated in numbers.")


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
        # The 0.8 m schema minimum overrides even a tighter user cap, and used to do it
        # in silence — the one span change with no note, right beside a branch that
        # notes every other one (S4-C1b).
        span = 0.8
        ar = span ** 2 / s_ref
        if notes is not None:
            notes.append(f"Span raised to the 0.8 m schema minimum: AR rises to "
                         f"{ar:.2f}"
                         + (f" — this is WIDER than your {span_cap:.2f} m span cap"
                            if span_cap < 0.8 - 1e-9 else ""))

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
        root_airfoil=shape.root_airfoil,
        mid_airfoil=shape.mid_airfoil,
        tip_airfoil=shape.tip_airfoil,
        root_twist_deg=shape.root_twist_deg, tip_twist_deg=shape.tip_twist_deg,
        dihedral_deg=shape.dihedral_deg)


def _feasible_planform(s: float, ar: float, shape: ShapeFractions,
                       span_cap: float, notes: Optional[List[str]],
                       s_floor: Optional[float] = None,
                       floor_reason: str = "your stall cap",
                       violations: Optional[List[CapViolation]] = None) -> BWBParams:
    """solve_planform with defensive schema-gate repairs for envelope corners
    (tiny or heavy-and-slow aircraft): the AR gate [2.5, 10] and the root-chord
    schema range [0.25, 1.2] m. Repairs trade AR first; area moves only when
    span is already pinned at a cap, and every area move is noted.

    `s_floor` is the area a HARD user requirement demands — normally the stall floor.
    The repairs may not silently go below it: they are schema conveniences, and a
    user's stall requirement outranks a cosmetic AR preference (S4-C1). Where the two
    genuinely conflict, this reports the conflict through `violations` and returns the
    closest legal planform, because `inverse` never raises and a quietly violating
    design is the one outcome that must not exist.
    """
    span_cap = min(span_cap, 3.0)
    ar = min(max(ar, _AR_GATE[0]), _AR_GATE[1])  # requested AR must respect the gate
    cbf, mf, taper = (shape.centre_body_fraction, shape.mid_chord_frac,
                      shape.taper_outer)
    blocked_by: Optional[str] = None

    def geometry_at(s_: float, ar_: float) -> tuple:
        span = min(max(math.sqrt(ar_ * s_), 0.8), span_cap)
        b1 = cbf * span / 2.0
        b2 = span / 2.0 - b1
        k = b1 * (1 + mf) + b2 * (mf + mf * taper)
        return span, k, s_ / k, span ** 2 / s_

    def shrink_to(new_s: float, gate: str) -> float:
        """Apply an area-reducing repair, recording it when it breaks the floor."""
        nonlocal blocked_by
        if s_floor is not None and new_s < s_floor - 1e-12 and blocked_by is None:
            blocked_by = gate
        return new_s

    for _ in range(80):
        span, k, root, ar_actual = geometry_at(s, ar)
        if ar_actual > _AR_GATE[1]:           # minimum span met a tiny wing
            s = span ** 2 / (_AR_GATE[1] * 0.999)
            if notes is not None:
                notes.append(f"Wing grown to {s:.3f} m^2: AR gate (10) at the "
                             f"{span:.2f} m minimum span")
            continue
        if ar_actual < _AR_GATE[0]:           # span pinned, area too large
            s = shrink_to(span ** 2 / (_AR_GATE[0] * 1.001),
                          f"the AR gate (2.5) at the {span:.2f} m span cap")
            if notes is not None:
                notes.append(f"Wing area capped at {s:.3f} m^2 by the AR gate "
                             f"(2.5) at the {span:.2f} m span cap — wing "
                             f"loading rises accordingly")
            continue
        if root > 1.2:                        # fat chord: raise AR, else cap area
            if span < span_cap - 1e-9 and ar < _AR_GATE[1] - 1e-9:
                ar = min(_AR_GATE[1], ar + 0.25)
                continue
            s = shrink_to(1.2 * k * 0.999,
                          f"the 1.2 m root-chord schema limit at {span:.2f} m span")
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

    if blocked_by is not None and violations is not None and s_floor is not None:
        violations.append(CapViolation(
            cap="stall", required=s_floor, achieved=s, unit="m^2",
            blocked_by=blocked_by,
            message=(f"{floor_reason} needs {s_floor:.3f} m^2 of wing; the largest "
                     f"this geometry allows is {s:.3f} m^2, blocked by "
                     f"{blocked_by}.")))
        if notes is not None:
            notes.append(
                f"CANNOT MEET {floor_reason}: it needs {s_floor:.3f} m^2 of wing and "
                f"{blocked_by} limits this design to {s:.3f} m^2.")

    # The contract is that this NEVER raises (module docstring): infeasibility is a
    # reported binding constraint, not an exception. The old loop only ever nudged the
    # area UP, which makes an AR-too-low failure worse, and a hypothesis sweep found a
    # legal requirement set that escaped it — mtow 0.5, v 10.5, alt 1305, ld_min 14 —
    # so the search now widens in BOTH directions around the requested area.
    base_s = s
    for factor in (1.0, 1.02, 0.98, 1.05, 0.95, 1.1, 0.9, 1.25, 0.8, 1.5, 0.7, 2.0):
        try:
            return solve_planform(base_s * factor, ar, shape, span_cap, notes)
        except ValueError:
            continue

    # Last resort: a geometry that is legal by construction. Put the span at the cap
    # (or the schema minimum) and choose the area that lands mid-gate on AR, so the
    # caller gets the closest feasible planform rather than an exception.
    span = min(max(0.8, span_cap), 3.0)
    mid_ar = 0.5 * (_AR_GATE[0] + _AR_GATE[1])
    if notes is not None:
        notes.append(
            f"No planform matched the requested area within the schema gates; fell "
            f"back to the closest legal shape at {span:.2f} m span and AR "
            f"{mid_ar:.1f}.")
    return solve_planform(span ** 2 / mid_ar, mid_ar, shape, span_cap, notes)


def effective_stall_cap(req: DesignRequirements) -> tuple:
    """(cap, label) — the stall speed actually in force: the tighter of the user's
    requirement and the launch method's tolerance (S6-J2)."""
    launch_cap = LAUNCH_VSTALL_MAX[req.launch]
    if req.v_stall_max_ms is not None and req.v_stall_max_ms < launch_cap:
        return req.v_stall_max_ms, "your requirement"
    return launch_cap, f"{req.launch} launch"


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
    # The EFFECTIVE cap, not just the user's row (S6-J2). With no stall row the target
    # was None, so `met` was None, so it could never be unmet, so no binding constraint
    # was ever set — and a design needing a 29.6 m/s hand throw was reported as "all
    # requirements met". The launch cap was applied as an area floor and then never
    # checked. It is a real limit whether or not the user typed one.
    t, cap_label = effective_stall_cap(req)
    out.append(RequirementStatus(
        name="v_stall_max_ms", label=f"Stall speed ({cap_label})", unit="m/s",
        target=t, achieved=est.v_stall_ms,
        met=est.v_stall_ms <= t + _VSTALL_TOL,
        margin=t - est.v_stall_ms))
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
    atmosphere = atmosphere_at(req.cruise_altitude_m)
    est = estimate(params, atmosphere, req.cruise_speed_ms,
                   req.mtow_kg * 9.81, req.launch)
    return _statuses(req, est)


def solve_requirements(req: DesignRequirements,
                       shape_from: Optional[BWBParams] = None,
                       locked: Optional[LockedValues] = None) -> InverseSolveResult:
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
      6. S1-F: honour any LOCKED values, or report a CapViolation saying by how much
         they could not be honoured. A lock is applied as a constraint and then
         VERIFIED against the geometry that came back, because the schema repairs in
         `_feasible_planform` are free to move area and span for their own reasons.
         Checking the result rather than trusting the request is what makes "never
         silently unlock" true regardless of what the repair machinery does.
    """
    locked = locked or LockedValues()
    shape = ShapeFractions.from_params(shape_from) if shape_from else BASELINE_SHAPE
    atmosphere = atmosphere_at(req.cruise_altitude_m)
    rho = atmosphere.density_kgm3
    v = req.cruise_speed_ms
    w = req.mtow_kg * 9.81
    q = 0.5 * rho * v ** 2
    launch_cap = LAUNCH_VSTALL_MAX[req.launch]
    stall_cap = min(req.v_stall_max_ms, launch_cap) if req.v_stall_max_ms else launch_cap
    span_cap = min(req.span_max_m, 3.0) if req.span_max_m else 3.0
    # A locked span is also a cap — the tightest one — so the sizing loop below cannot
    # spend area on span it is not allowed to have.
    if locked.span_m is not None:
        span_cap = min(span_cap, locked.span_m)

    notes: List[str] = []
    lock_violations: List[CapViolation] = []
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
                est_i = estimate(params_i, atmosphere, v, w, req.launch)
                s_new = w / (q * est_i.cl_star)
                if abs(s_new - s) / s < 1e-3:
                    s = s_new
                    break
                s = s_new
                params_i = _feasible_planform(s, ar_try, shape, span_cap, None)
        s_min_cl = w / (q * MAX_CRUISE_CL)
        # S1-C2: CLmax is Reynolds-dependent now, and this floor is about the stall
        # cap — so it is evaluated at that speed, not at cruise.
        _p = _feasible_planform(max(s, s_min_cl), ar_try, shape, span_cap, None)
        clmax = cl_max_3d(_p, reynolds_at(_p, atmosphere, stall_cap))
        s_min_stall = 2.0 * w / (rho * stall_cap ** 2 * clmax)
        floor = None
        if max(s_min_cl, s_min_stall) > s:
            floor = "stall" if s_min_stall >= s_min_cl else "cl_cap"
            s = max(s_min_cl, s_min_stall)
        return s, floor

    s, floor = area_at(ar)

    # A locked AREA replaces the sizing result outright: the operator has the wing, and
    # what the requirements would have asked for is no longer the question. The floors
    # are still computed, because the gap between the locked area and the area the
    # stall cap demands is exactly what the operator needs to be told.
    if locked.s_ref_m2 is not None:
        s_wanted = s
        s = locked.s_ref_m2
        if s_wanted > s * (1.0 + 1e-9):
            lock_violations.append(CapViolation(
                cap="s_ref lock", required=s_wanted, achieved=s, unit="m^2",
                blocked_by=(floor or "sizing target"),
                message=(f"Locked wing area {s:.4f} m^2 is smaller than the "
                         f"{s_wanted:.4f} m^2 this mission needs"
                         + (" to hold the stall cap" if floor == "stall"
                            else " to stay under the cruise-CL cap" if floor == "cl_cap"
                            else " to hit the cruise-CL target")
                         + ". The lock was honoured; the requirement was not.")))

    # A locked SPAN fixes span, which with the area fixes aspect ratio. Ask for exactly
    # that AR so the solver aims at the lock rather than being clipped to it.
    if locked.span_m is not None and s > 0:
        ar = locked.span_m ** 2 / s

    params = _feasible_planform(s, ar, shape, span_cap, notes)

    est = estimate(params, atmosphere, v, w, req.launch)

    # step 4 — spend span budget on AR for the L/D objective.
    #
    # SKIPPED UNDER A GEOMETRY LOCK. The loop works by trading area against aspect
    # ratio, and both locks remove exactly that freedom: with area pinned, raising AR
    # moves span; with span pinned, it moves area. Running it anyway would quietly
    # break the lock the operator just set in order to chase an L/D target, which is
    # the "silent unlock" this item exists to make impossible.
    if locked.span_m is not None or locked.s_ref_m2 is not None:
        if req.ld_min is not None and est.ld_cruise < req.ld_min - _LD_TOL:
            notes.append(
                f"L/D {est.ld_cruise:.1f} is below your {req.ld_min:.1f} target and "
                f"the solver did not chase it: the geometry lock fixes the aspect "
                f"ratio, which is the only lever it has. Unlock span or area, or "
                f"relax the L/D target.")
    elif req.ld_min is not None:
        for _ in range(40):
            if est.ld_cruise >= req.ld_min - _LD_TOL:
                break
            if ar >= _AR_GATE[1] - 1e-9 or params.span_m >= span_cap - 1e-3:
                break
            ar = min(_AR_GATE[1], ar + 0.25)  # never overshoot the AR gate
            s, floor = area_at(ar)
            params = _feasible_planform(s, ar, shape, span_cap, None)
            est = estimate(params, atmosphere, v, w, req.launch)

    # VERIFY, do not assume — and do it on the FINAL geometry, after any AR loop.
    # `_feasible_planform` repairs AR-gate and root-chord corners by moving area and
    # span, so a lock can survive the request and die in the repair. Measuring what was
    # delivered is the only check that a future change to those repairs cannot bypass.
    if locked.span_m is not None and abs(params.span_m - locked.span_m) > LOCK_TOL["span_m"]:
        lock_violations.append(CapViolation(
            cap="span lock", required=locked.span_m, achieved=params.span_m,
            unit="m", blocked_by="schema gates (aspect ratio / root chord)",
            message=(f"Span locked at {locked.span_m:.3f} m but the geometry that "
                     f"satisfies the schema is {params.span_m:.3f} m. No planform in "
                     f"this family holds both the lock and the gates.")))
    if locked.s_ref_m2 is not None:
        achieved = params.planform_area_m2()
        if abs(achieved - locked.s_ref_m2) > LOCK_TOL["s_ref_m2"]:
            lock_violations.append(CapViolation(
                cap="s_ref lock", required=locked.s_ref_m2, achieved=achieved,
                unit="m^2", blocked_by="schema gates (aspect ratio / root chord)",
                message=(f"Wing area locked at {locked.s_ref_m2:.4f} m^2 but the "
                         f"geometry that satisfies the schema has {achieved:.4f} m^2.")))

    statuses = _statuses(req, est)
    unmet = [st for st in statuses if st.met is False]

    binding: Optional[str] = None
    explanation = "all requirements met" if not unmet else ""
    if unmet:
        # A breached stall cap is diagnosed FIRST, whatever the area floor said (S6-J2).
        # In the minimal reproducer — stall 5.24 against a 5.00 cap — the old order
        # blamed the span cap and talked about aspect ratio and L/D while the actual
        # failure was the user's stall requirement, missed by 0.24 m/s.
        stall_unmet = next((st for st in unmet if st.name == "v_stall_max_ms"), None)
        if stall_unmet is not None:
            cap, cap_label = effective_stall_cap(req)
            if cap_label == "your requirement":
                binding = "stall_requirement"
                explanation = (
                    f"stall {est.v_stall_ms:.2f} m/s exceeds your {cap:.2f} m/s "
                    f"requirement — no wing this schema allows at "
                    f"{params.span_m:.2f} m span is large enough")
            else:
                binding = "stall_at_launch"
                explanation = (
                    f"stall {est.v_stall_ms:.2f} m/s exceeds what a {req.launch} "
                    f"launch tolerates ({cap:g} m/s) — a faster launch method, more "
                    f"span, or less mass would be needed")
        elif floor == "stall":
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
        binding_constraint=binding, binding_explanation=explanation, notes=notes,
        lock_violations=lock_violations)
