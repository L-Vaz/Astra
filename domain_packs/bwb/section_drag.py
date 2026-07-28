"""Profile drag from the section data, integrated across the span (S1-E).

WHAT THIS IS FOR
----------------
`aero_estimates.parasite_drag_coeff` builds CD0 from a flat-plate skin-friction
coefficient times a Hoerner form factor times a wetted-area fit times a lumped
interference margin. Every step is a correlation for conventional aircraft, and the
whole chain is blind to which aerofoil the wing actually uses: swapping the tip section
for one with twice the drag changes nothing but the thickness ratio.

Since S1-C the pack carries measured section drag — CD sampled on a CL ladder at five
Reynolds numbers for 2,171 aerofoils. This module uses it.

THE PHYSICS
-----------
Strip theory. Cut the wing into spanwise strips; each behaves like a 2D aerofoil at its
own local lift coefficient and its own chord Reynolds number. Its drag is that
aerofoil's measured CD times its area, and the wing's profile drag coefficient is the
area-weighted mean:

    CD_profile(CL) = (2 / S) * INTEGRAL_0^(b/2) cd( cl(y), Re(y) ) c(y) dy

with the local lift coefficient from Schrenk (`spanwise.py`),

    cl(y) = CL * cl_add(y) + cl_basic(y)

and Re(y) = Re_MAC * c(y) / MAC. The factor 2 is the other half-wing.

WHY THIS IS NOT "CD0"
---------------------
It is not the zero-lift parasite drag. A section's CD rises with its lift coefficient —
the boundary layer thickens and the pressure drag grows — so the strip integral at the
CRUISE CL already contains the viscous part of the lift-dependent drag. The classical
polar CD = CD0 + CL^2/(pi e AR) has no separate home for that, which is precisely why an
empirical "Oswald efficiency" comes out at 0.75-0.85 for real aircraft while the planar
inviscid span efficiency of the same wing is 0.98 (see `lifting_line.py`). Oswald e is
carrying viscous drag under an inviscid name.

So this module reports the profile drag AT A GIVEN CL, and separately at zero lift for
comparison with the existing CD0. Combining `cd_profile(CL)` with the planar induced
drag CL^2/(pi e_planar AR) is the physically clean decomposition; combining it with the
Raymer CD0 would double-count.

WHAT IT OMITS, AND WHY THE POLAR HAS NOT BEEN CHANGED
-----------------------------------------------------
A sum of 2D section drags is the profile drag of an INFINITE-SPAN wing built from those
sections. It contains no junction interference where the centre body meets the outer
panel, no surface roughness, no excrescences, no control-surface gaps. NeuralFoil is
trained on XFoil, a boundary-layer method, on smooth aerofoils.

Measured on the golden reference (commands in the tests):

    strip-integrated profile CD at zero lift        0.00990
    Raymer wetted-area CD0                          0.01360   (+37.4%)
    Raymer without its 1.10 interference margin     0.01237   (+25.0%)

so 1.10 of interference does not explain the gap; reconciling them needs a factor of
about 1.37. Using the section buildup with the planar induced term would take the
reference wing's cruise L/D from 15.93 to 20.16 — a 27% improvement arriving from a
model change rather than from a better aircraft.

Choosing the reconciling factor to close that gap would be fitting a judgment constant
to the answer, so this module does NOT change the drag polar. `parasite_drag_coeff`
still drives `cd_cruise`. Both numbers are reported, with their ratio, so that the
disagreement is visible and can be settled with evidence — a wind-tunnel or CFD point,
or the project's own OpenFOAM runs, which is exactly what tiers 2 and 3 exist for.
"""
from __future__ import annotations

import math
from functools import lru_cache
from typing import NamedTuple, Tuple

from .airfoils import cl_ladder, provenance, section_cd
from .schema import BWBParams
from .spanwise import _panel_interp, stations


class ProfileDrag(NamedTuple):
    """Strip-integrated profile drag, and everything needed to distrust it."""
    cd_profile: float
    """Profile drag coefficient at the requested wing CL. INCLUDES the viscous
    lift-dependent part, so it must not be added to a separate CD0."""

    cd_profile_at_zero_lift: float
    """The same integral at CL = 0 — the quantity comparable with the Raymer CD0."""

    cd0_wetted_area: float
    """The existing wetted-area buildup, for cross-check. Not used here."""

    disagreement_ratio: float
    """cd0_wetted_area / cd_profile_at_zero_lift. 1.0 would mean the two models agree.
    Measured 1.374 on the reference wing; the lumped interference margin the wetted-area
    buildup already carries is 1.10, so interference alone does not account for it.

    READ IT WITH `span_fraction_clamped_at_zero_lift`. At CL = 0 the washed-out tips
    carry NEGATIVE local lift, and the stored ladder starts at cl = 0, so on the
    reference wing 66.6% of the span is clamped to the ladder's floor. That floor is
    near the bottom of the low-Reynolds drag bucket and the bucket is roughly symmetric
    about it, so the clamp is not wild — but two thirds of this particular number is a
    boundary value rather than a measurement, and it must not be quoted as though it
    were the latter."""

    span_fraction_clamped_at_zero_lift: float

    span_fraction_below_ladder: float
    span_fraction_above_ladder: float
    """Fraction of the half-span where the local lift coefficient fell off the stored CL
    ladder and was CLAMPED to its end. Reported rather than hidden: a clamped drag value
    is indistinguishable from a measured one once it is inside an integral. The ladder
    runs 0.0 to 0.9, so washed-out tips at low wing CL fall off the bottom and the
    inboard stations fall off the top near stall."""

    ladder_interpolation_error_pct: float
    """The table's OWN measured error, from its provenance — median across 87 probes.
    Carried here so a consumer cannot quote this drag to more precision than the data
    supports."""


def _ladder_bounds() -> Tuple[float, float]:
    lad = cl_ladder()
    return lad[0], lad[-1]


@lru_cache(maxsize=1)
def _ladder_error_pct() -> float:
    err = provenance().get("cl_ladder_interpolation_error_pct") or {}
    return float(err.get("median", 0.0))


class _CdSurface(NamedTuple):
    """One section's CD sampled on the table's own (Reynolds x CL) mesh.

    PERFORMANCE, and the reason this exists: the naive version called `section_cd`
    three times per station for two integrations — 486 table walks, measured at
    7.85 ms per evaluation. `estimate()` is already ~6 ms and sits in the concept loop.

    The stored mesh is 5 Reynolds points by 7 ladder points, so each section is sampled
    once (35 walks total, cached across every call with the same section) and
    interpolated bilinearly here: linear in log(Re) and linear in CL, which is exactly
    what `section_cd` does internally, just hoisted out of the station loop."""
    log_re: Tuple[float, ...]
    ladder: Tuple[float, ...]
    cd: Tuple[Tuple[float, ...], ...]      # [re_index][ladder_index]

    def at(self, cl: float, reynolds: float) -> float:
        x = math.log(max(reynolds, 1.0))
        i = _bracket(self.log_re, x)
        j = _bracket(self.ladder, cl)
        tr = _frac(self.log_re, i, x)
        tc = _frac(self.ladder, j, cl)
        lo = self.cd[i][j] + tc * (self.cd[i][j + 1] - self.cd[i][j])
        hi = self.cd[i + 1][j] + tc * (self.cd[i + 1][j + 1] - self.cd[i + 1][j])
        return lo + tr * (hi - lo)


def _bracket(xs: Tuple[float, ...], x: float) -> int:
    """Index i with xs[i] <= x <= xs[i+1], clamped to the ends.

    Clamping is the same behaviour `section_cd` has, and it is why the caller tracks
    what fraction of the span left the ladder: outside the table the returned value is
    the boundary value, not an extrapolation, and nothing downstream can tell the two
    apart on its own."""
    if len(xs) < 2:
        return 0
    if x <= xs[0]:
        return 0
    if x >= xs[-1]:
        return len(xs) - 2
    for i in range(len(xs) - 1):
        if xs[i] <= x <= xs[i + 1]:
            return i
    return len(xs) - 2


def _frac(xs: Tuple[float, ...], i: int, x: float) -> float:
    lo, hi = xs[i], xs[i + 1]
    if hi <= lo:
        return 0.0
    return min(1.0, max(0.0, (x - lo) / (hi - lo)))


@lru_cache(maxsize=256)
def _surface(name: str) -> _CdSurface:
    grid = sorted(provenance()["re_grid"])
    lad = cl_ladder()
    rows = tuple(tuple(section_cd(name, cl, re)[0] for cl in lad) for re in grid)
    return _CdSurface(log_re=tuple(math.log(re) for re in grid),
                      ladder=tuple(lad), cd=rows)


def _cd_at(surfaces, y: float, b1: float, b2: float,
           cl: float, reynolds: float) -> float:
    """Local section CD, blended between the three stations.

    MODELLING CHOICE, the same one `spanwise.cl_max_at` makes and for the same reason:
    the drag VALUE is interpolated between the bracketing sections rather than the
    aerofoil shape. Blending coordinates and re-running NeuralFoil per station would be
    more faithful and far too slow to sit in a design loop."""
    return _panel_interp(y, b1, b2, *(s.at(cl, reynolds) for s in surfaces))


def profile_drag(params: BWBParams, cl_wing: float,
                 reynolds_mac: float) -> ProfileDrag:
    """Strip-integrated profile drag at one wing CL, with the wetted-area cross-check."""
    from .aero_estimates import parasite_drag_coeff

    sts = stations(params, reynolds_mac)
    s_ref = params.planform_area_m2()
    b1, b2 = params.half_widths()
    half = params.span_m / 2.0
    surfaces = tuple(_surface(n) for n in
                     (params.root_airfoil, params.mid_airfoil, params.tip_airfoil))
    cl_lo, cl_hi = _ladder_bounds()

    def integrate(cl_target: float) -> Tuple[float, float, float]:
        total = 0.0
        below = above = 0.0
        for a, b in zip(sts, sts[1:]):
            dy = b.y - a.y
            if dy <= 0:
                continue

            def strip(st) -> float:
                cl = cl_target * st.cl_add + st.cl_basic
                return _cd_at(surfaces, st.y, b1, b2, cl, st.reynolds) * st.chord

            total += 0.5 * (strip(a) + strip(b)) * dy
            # Attribute the out-of-ladder span to the interval, using its midpoint —
            # a station-count tally would depend on the (cosine) grid spacing and so
            # would over-report the tip, which is exactly where the clamping happens.
            cl_mid = cl_target * 0.5 * (a.cl_add + b.cl_add) + 0.5 * (a.cl_basic + b.cl_basic)
            if cl_mid < cl_lo:
                below += dy
            elif cl_mid > cl_hi:
                above += dy
        span = half if half > 0 else 1.0
        return (2.0 * total / s_ref if s_ref > 0 else 0.0), below / span, above / span

    cd_at_cl, frac_below, frac_above = integrate(cl_wing)
    cd_at_zero, zero_below, zero_above = integrate(0.0)
    cd0_raymer = parasite_drag_coeff(params, reynolds_mac)

    return ProfileDrag(
        cd_profile=cd_at_cl,
        cd_profile_at_zero_lift=cd_at_zero,
        cd0_wetted_area=cd0_raymer,
        disagreement_ratio=(cd0_raymer / cd_at_zero) if cd_at_zero > 0 else 0.0,
        span_fraction_clamped_at_zero_lift=zero_below + zero_above,
        span_fraction_below_ladder=frac_below,
        span_fraction_above_ladder=frac_above,
        ladder_interpolation_error_pct=_ladder_error_pct(),
    )
