"""Can this wing trim itself at cruise, and what does trimming cost it? (S1-D)

THE QUESTION
------------
A tailless aircraft has no tail to balance itself with. Everything that holds the nose
where it belongs is built into the wing: the camber of the sections, the sweep, and the
twist. Stage 1 could previously say nothing at all about this — `objective.w8_trim`
scored `cm_at_trim` from the tier-1 VLM, so a concept that could not trim was
indistinguishable from one that could until it had been through AeroSandbox.

THE PHYSICS, FROM THE DEFINITION OF A MOMENT
--------------------------------------------
Take x aft of the root leading-edge apex and call a nose-up moment positive.

At a spanwise station y the strip of wing of width dy carries two things:

  1. Its own pitching moment. Aerofoil data is quoted about the QUARTER CHORD because
     that is (near enough) the aerodynamic centre — the point about which the moment
     does not change with angle of attack. So the strip contributes

         dM_section = q * c(y)^2 * cm0(y) * dy

     `cm0` here is the section's Cm at zero incidence, measured for this project by
     NeuralFoil and stored in the committed screen. Camber makes it negative (nose
     down); reflex is the deliberate act of making it less negative or positive, and
     that is the entire reason a flying wing uses reflexed sections.

  2. Its lift, acting at that quarter-chord point. A lift force L sitting a distance
     (x_c4 - x_cg) AFT of the centre of gravity pushes the nose DOWN:

         dM_lift = -q * c(y) * cl(y) * (x_c4(y) - x_cg) * dy

     On a swept wing x_c4 grows with y, so the outboard strips have the long arms.
     This is why sweep substitutes for a tail, and why WASHOUT is a trim control: it
     moves load off the long-armed outboard strips and onto the short-armed inboard
     ones.

Both halves are symmetric, so integrating over the right half and doubling:

    Cm(x_cg) = (2 / (S * MAC)) * INTEGRAL_0^(b/2)
                 [ cm0(y) c(y)^2 - cl(y) c(y) (x_c4(y) - x_cg) ] dy

with the spanwise loading from Schrenk (`spanwise.py`):

    cl(y) = CL * cl_add(y) + cl_basic(y)

WHY THE SOLVE IS EXACT, NOT ITERATIVE
-------------------------------------
`cl_add`, `cm0`, `c` and `x_c4` do not depend on twist at all. `cl_basic` is
0.5 * a(y) * (aero_twist(y) - twist_bar), and both aero_twist and twist_bar are AFFINE
in `tip_twist_deg` (the blend twist is itself a linear interpolation between root and
tip). So Cm is an affine function of tip twist, and two evaluations locate its root
exactly. No iteration, no convergence criterion, no tolerance to tune.

WHAT THIS IS NOT
----------------
* NOT an elevon solve. The maintainer's ruling for Stage 1 is washout-only: no control
  surface, no schema change. The answer is "can this wing trim itself AS BUILT", which
  is a real yes/no, rather than "what deflection would be needed", which needs a
  control surface this schema does not have.
* NOT a substitute for the VLM. The quarter-chord aerodynamic centre is a thin-aerofoil
  result and the loading is Schrenk's approximation; TM-948 p.5 endorses this class of
  use ("static equilibrium problems") but the tier-1 AeroSandbox `cm_at_trim` supersedes
  it whenever it exists.
* NOT free of the sweep caveat. Schrenk has no sweep term (see `spanwise.py`), so the
  LOADING here is a straight-wing loading placed on a swept planform. The moment ARM is
  exact; the load distribution it multiplies is the approximation.
"""
from __future__ import annotations

from typing import NamedTuple, Optional, Tuple

from .aero_estimates import neutral_point_m
from .schema import BWBParams
from .spanwise import solve_stall, stations


class TrimSolution(NamedTuple):
    """Trim state at one flight condition, and what reaching it costs."""
    cm_cg: float
    """Residual pitching moment coefficient about the CG, as built. Nose-up positive.
    Zero means the wing flies itself at this CL with no control input."""

    trims_as_built: bool
    """|cm_cg| within CM_TRIM_TOLERANCE."""

    tip_twist_for_trim_deg: Optional[float]
    """Tip twist that would zero cm_cg, holding root twist and everything else fixed.
    None when the wing has no twist authority over pitch at all (dCm/dtwist ~ 0),
    which happens on an unswept wing — there the outboard arms are no longer longer."""

    tip_twist_in_range: bool
    """Whether that twist is inside the schema's declared tip-twist range. Outside it,
    washout alone cannot trim this wing and something else must give: sweep, CG, or
    reflexed sections."""

    dcm_dtwist_per_deg: float
    """Pitch authority per degree of tip twist. The sensitivity of the whole answer."""

    cl_max_as_built: float
    cl_max_at_trim_twist: Optional[float]
    """What trimming costs in maximum lift. Unloading the tips to trim removes exactly
    the lift the wing needs at low speed, which raises the stall speed, which raises the
    wing-area floor. This is the feedback that made S1-C2/D/E one batch of work."""

    cm0_contribution: float
    lift_contribution: float
    """The two halves of cm_cg, so a card can say WHICH one is out of balance. Section
    camber and load-times-arm pull in opposite directions on a well-designed BWB."""

    x_cg_for_trim_m: Optional[float]
    static_margin_at_trim: Optional[float]
    """THE OTHER WAY TO READ THE SAME EQUATION, and usually the more useful one.

    Trim and static margin are two separate demands on one CG position. Asking "what
    washout trims this wing at the CG that gives 8% margin" can answer "none that
    exists" while the wing is perfectly flyable — just at a different CG, with a
    different margin. So the solve is also inverted for x_cg, which is exact because
    Cm is linear in it (the lift term is a force times an arm measured from x_cg and
    nothing else depends on it), and the resulting static margin is reported so the
    cost of trimming that way is visible rather than hidden.

    A NEGATIVE static margin here means the wing can only be trimmed by moving the CG
    behind the neutral point — that is, it can be balanced or it can be stable, but not
    both, which is exactly the verdict Stage 1 should be able to deliver."""


CM_TRIM_TOLERANCE = 0.005
"""[D] ENGINEERING JUDGMENT. |Cm| below this counts as "trims itself".

Basis for the magnitude rather than a source: on the reference wing dCm/dtwist is about
0.004 per degree, so 0.005 is roughly one degree of tip twist — finer than this schema
can express, since the twist range is 9 degrees wide and no builder sets twist to better
than about half a degree. SENSITIVITY: it changes only the boolean `trims_as_built`,
never `cm_cg` or `tip_twist_for_trim_deg`, which are what the card should show.
FALSIFIED IF tier-1 trim runs show designs inside this band still needing meaningful
elevon to hold cruise."""

# The schema's declared tip-twist limits. Read from the model rather than restated, so
# a schema change cannot silently make this function lie about what is reachable.
def tip_twist_range() -> Tuple[float, float]:
    f = BWBParams.model_fields["tip_twist_deg"]
    lo = hi = None
    for m in f.metadata:
        lo = getattr(m, "ge", lo)
        hi = getattr(m, "le", hi)
    return (lo if lo is not None else -90.0, hi if hi is not None else 90.0)


class _MomentParts(NamedTuple):
    """The x_cg-independent pieces, so one integration serves every CG.

    Expanding the arm, (x_c4 - x_cg), splits the lift term exactly:

        Cm(x_cg) = scale * [ camber - about_apex + x_cg * lift ]

    which makes dCm/dx_cg = scale * lift ANALYTIC — total lift coefficient times the
    reference length. No finite-difference probe, and one `stations()` call instead of
    two for the CG inversion."""
    camber: float        # INTEGRAL cm0 * c^2 dy
    about_apex: float    # INTEGRAL cl * c * x_c4 dy
    lift: float          # INTEGRAL cl * c dy
    scale: float         # 2 / (S * MAC)

    def cm(self, x_cg_m: float) -> Tuple[float, float, float]:
        camber = self.scale * self.camber
        lift = self.scale * (x_cg_m * self.lift - self.about_apex)
        return camber + lift, camber, lift

    def dcm_dx_cg(self) -> float:
        return self.scale * self.lift


def _moment_parts(p: BWBParams, cl_wing: float, reynolds_mac: float) -> _MomentParts:
    sts = stations(p, reynolds_mac)
    s_ref = p.planform_area_m2()
    mac = p.mean_aero_chord_m()
    if s_ref <= 0 or mac <= 0:
        return _MomentParts(0.0, 0.0, 0.0, 0.0)

    camber = apex = lift = 0.0
    for a, b in zip(sts, sts[1:]):
        dy = b.y - a.y
        if dy <= 0:
            continue

        def strip(st):
            cl = cl_wing * st.cl_add + st.cl_basic
            return (st.cm0 * st.chord ** 2, cl * st.chord * st.x_c4, cl * st.chord)

        ca, aa, la = strip(a)
        cb, ab, lb = strip(b)
        camber += 0.5 * (ca + cb) * dy
        apex += 0.5 * (aa + ab) * dy
        lift += 0.5 * (la + lb) * dy

    return _MomentParts(camber, apex, lift, 2.0 / (s_ref * mac))


def pitching_moment(p: BWBParams, cl_wing: float, x_cg_m: float,
                    reynolds_mac: float) -> Tuple[float, float, float]:
    """(Cm about x_cg, section-camber part, lift-times-arm part). Nose-up positive."""
    return _moment_parts(p, cl_wing, reynolds_mac).cm(x_cg_m)


def solve_trim(p: BWBParams, cl_wing: float, x_cg_m: float,
               reynolds_mac: float) -> TrimSolution:
    """Trim residual as built, and the tip twist that would zero it.

    `cl_wing` is the lift coefficient to trim AT — normally the cruise CL, because
    that is where the aircraft spends its time and where residual trim drag is paid
    continuously.
    """
    parts = _moment_parts(p, cl_wing, reynolds_mac)
    cm, cm0_part, lift_part = parts.cm(x_cg_m)

    lo, hi = tip_twist_range()
    # Cm is affine in tip twist (see the module docstring), so one probe gives the exact
    # slope. Probe DOWNWARD from the current value so the point stays inside the
    # schema's range wherever the current setting sits; the constructor would reject an
    # out-of-range probe and the affine result is the same from either side.
    probe = max(lo, p.tip_twist_deg - 1.0)
    if probe == p.tip_twist_deg:
        probe = min(hi, p.tip_twist_deg + 1.0)
    d_twist = probe - p.tip_twist_deg

    tip_for_trim: Optional[float] = None
    slope = 0.0
    if d_twist != 0.0:
        probed = p.model_copy(update={"tip_twist_deg": probe})
        cm_probe = _moment_parts(probed, cl_wing, reynolds_mac).cm(x_cg_m)[0]
        slope = (cm_probe - cm) / d_twist
        # A near-zero slope is not a numerical accident: on an UNSWEPT wing every
        # station has the same moment arm, so moving load spanwise does not move the
        # centre of pressure fore or aft and twist has no pitch authority whatsoever.
        # Reporting None is the honest answer; dividing would produce a vast number.
        if abs(slope) > 1e-9:
            tip_for_trim = p.tip_twist_deg - cm / slope

    clmax_built = solve_stall(p, reynolds_mac).cl_max_wing
    clmax_trim: Optional[float] = None
    if tip_for_trim is not None and lo <= tip_for_trim <= hi:
        trimmed = p.model_copy(update={"tip_twist_deg": tip_for_trim})
        clmax_trim = solve_stall(trimmed, reynolds_mac).cl_max_wing

    # Cm is LINEAR in x_cg, and the slope is analytic — see `_MomentParts`.
    x_cg_trim: Optional[float] = None
    sm_trim: Optional[float] = None
    dcm_dx = parts.dcm_dx_cg()
    if abs(dcm_dx) > 1e-9:
        x_cg_trim = x_cg_m - cm / dcm_dx
        mac = p.mean_aero_chord_m()
        if mac > 0:
            sm_trim = (neutral_point_m(p) - x_cg_trim) / mac

    return TrimSolution(
        cm_cg=cm,
        trims_as_built=abs(cm) <= CM_TRIM_TOLERANCE,
        tip_twist_for_trim_deg=tip_for_trim,
        tip_twist_in_range=(tip_for_trim is not None and lo <= tip_for_trim <= hi),
        dcm_dtwist_per_deg=slope,
        cl_max_as_built=clmax_built,
        cl_max_at_trim_twist=clmax_trim,
        cm0_contribution=cm0_part,
        lift_contribution=lift_part,
        x_cg_for_trim_m=x_cg_trim,
        static_margin_at_trim=sm_trim,
    )
