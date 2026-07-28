"""Spanwise lift distribution and the stall station (S1-C2).

WHAT THIS REPLACES
------------------
`aero_estimates.cl_max_3d` was `CL_MAX_2D * 0.90 * cos(sweep)` — one hard-coded section
value and one unsourced knockdown, identical for every aircraft this project has ever
produced. Feeding the new section table into that formula would only have changed WHICH
number was guessed.

A wing stalls LOCALLY. At each spanwise station there are two curves:

  demand      cl(y)      — the local lift coefficient, set by planform and twist
  capability  cl_max(y)  — set by the section there, at THAT station's Reynolds number

Stall begins where they first touch. The wing CLmax is the total CL at that moment, and
every other station is still below its own limit — which is exactly what the mystery
0.90 was standing in for. Computing it deletes the fudge factor.

Why this matters more than the number: if stall starts outboard on a swept wing the
centre of lift moves FORWARD, so the aircraft pitches UP and drives itself deeper into
the stall. A tailless BWB has no tail to recover with. That is the departure mode that
kills this configuration, and Stage 1 could not previously see it.

THE METHOD — Schrenk, NACA TM-948 (1940)
----------------------------------------
"A Simple Approximation Method for Obtaining the Spanwise Lift Distribution", O.
Schrenk, NACA Technical Memorandum No. 948, translated by S. Reiss. Public domain, from
NTRS accession 19930094469. Read for this implementation; equations cited to page.

The paper endorses this exact application (p. 5): "suitable for all problems where too
great accuracy is not required; that is, in general, for investigations with regard to
the maximum lift coefficient Ca_max, stalling, and static equilibrium problems."

Fundamental idea (p. 2): "The plausible assumption is made that the real lift
distribution lies between an ideal distribution independent of the wing shape and a
distribution determined in a simple manner by the wing shape." The ideal is the ellipse;
the shape-dependent one is proportional to chord.

Untwisted wing (p. 2): "There is drawn instead the semiellipse of equal area with the
chord-distribution curve, and the lift distribution is obtained by forming the
ARITHMETICAL MEAN between the two curves."

Twisted wing (p. 2) — the part that is easy to get wrong, and does not appear in any
summary of the method:

  1. The zero-lift reference is the CHORD-WEIGHTED mean twist,
     "delta_bar = mean[delta(x) t(x)] / mean[t(x)]", and "for all further computations,
     the angles of attack and twist are reckoned from the zero lift direction given by
     delta_bar". This is what makes the basic loading carry zero net lift.
  2. "the lift is decomposed into a component without twist and a twist component
     without lift. The second component is determined on the basis of a mean value which
     has THE ZERO LINE INSTEAD OF THE ELLIPSE as the ideal distribution". So the twist
     term is the mean of the local twist loading and ZERO — i.e. HALF of it. Using the
     ellipse here instead would double the effect of washout, which is precisely the
     lever that decides whether this wing stalls inboard or outboard.

Master formula (p. 3):

  dA/dx = 1/2 q (dc_a/dalpha) [ t(x) + (4F/pi b) sqrt(1 - (2x/b)^2) ]
        + 1/2 q (dc_a/ddelta) delta(x) t(x)

LIMITATIONS, AS THE PAPER STATES THEM
-------------------------------------
* NO SWEEP TREATMENT ANYWHERE. TM-948 is a straight monoplane-wing derivation. The
  cos(sweep) factor kept in `aero_estimates` is a separately labelled patch over this
  method, not part of it, and it is the weakest link in the result.
* p. 5: "For the computation of the induced drag and for the downwash computation, the
  method is not directly applicable." So this cannot be reused for span efficiency.
* p. 4: deviations concentrate at unrounded wing tips and at aileron/flap transitions.
* p. 3 offers a (1 + tau) lift-slope refinement, tau by taper ratio (fig. 1). NOT
  implemented here; `TAU_REFINEMENT_APPLIED` records that, so the omission is visible
  rather than forgotten.

NOT FROM THE PAPER
------------------
Schrenk assumes ONE aerofoil throughout. This wing has three, which forces two decisions
the paper does not make:

1. INTERPOLATING THE CAPABILITY VALUE. cl_max between bracketing stations is
   interpolated as a NUMBER, not by blending aerofoil coordinates and re-running
   NeuralFoil per station. The latter would be more faithful and far too slow for a
   function inside the sizing loop. See `cl_max_at`.

2. AERODYNAMIC TWIST. p.2 reckons all angles "from the zero lift direction", which for
   a single-section wing is one direction. With three sections each station has its own,
   so the twist that drives the basic loading is the GEOMETRIC twist minus the local
   zero-lift angle. This is the only reading consistent with the paper, and it is not
   cosmetic: measured on the golden reference, the cambered s5010 tip sits at
   alpha_L0 = -1.35 deg against 0.00 for the symmetric e474 root, so 1.35 deg of the
   4.0 deg geometric washout is cancelled by camber — 34%. Before this correction the
   reference appeared to reach an inboard stall at -5 deg of washout; it does not reach
   one anywhere in the schema's range.
"""
from __future__ import annotations

import math
from functools import lru_cache
from typing import List, NamedTuple, Tuple

from .airfoils import provenance, section_at
from .schema import BWBParams

# Integration stations per half-span. The 19-station table in planform.js is a PREVIEW
# MESH for drawing the aircraft, not an integration grid, and it is far too coarse near
# the tip where the elliptic ordinate has infinite slope. This grid is cosine-spaced,
# which clusters points at both ends for that reason, and the panel break is inserted
# explicitly so the chord kink is never straddled.
N_STATIONS = 80

# Fraction of semi-span beyond which a stall onset counts as "outboard". Outboard stall
# on a swept tailless wing moves the centre of lift forward and pitches the aircraft up,
# deeper into the stall.
#
# [D] ENGINEERING JUDGMENT, not from TM-948. 0.60 is chosen because outboard of ~60%
# semi-span the local lift acts aft of the centre of gravity on a swept wing, so losing
# it produces the nose-up increment. Sensitivity: moving this to 0.5 or 0.7 changes only
# which designs get FLAGGED, never a computed number. Falsified if a trim-and-departure
# analysis at tier 1 shows the pitch-up onset at a materially different station.
OUTBOARD_STALL_FRAC = 0.60

TAU_REFINEMENT_APPLIED = False
"""TM-948 p.3 offers a (1+tau) lift-slope refinement by taper ratio (fig. 1). Skipped:
the paper itself says "A further refinement in the value of K, nevertheless, seems
unnecessary." Recorded as a flag so its absence is visible in the provenance."""


class Station(NamedTuple):
    """One spanwise integration station on the right half-wing."""
    y: float                # m from centreline
    eta: float              # y / (b/2), 0 at root, 1 at tip
    chord: float            # m
    twist_deg: float        # local geometric twist
    reynolds: float         # chord Reynolds at this station
    cl_add: float           # additional loading: cl(y) per unit wing CL
    cl_basic: float         # basic (twist) loading, CL-independent
    cl_max: float           # local section capability at this station's Reynolds
    cm0: float = 0.0        # local section zero-lift pitching moment about its own
                            # quarter chord — the camber's trim burden (S1-D)
    x_c4: float = 0.0       # quarter-chord x aft of the root leading-edge apex, m.
                            # The moment ARM that turns local lift into pitch (S1-D)


class StallSolution(NamedTuple):
    """Where the wing stalls, and what it can lift when it does."""
    cl_max_wing: float
    stall_eta: float                # fraction of semi-span, 0 = root, 1 = tip
    stall_is_outboard: bool
    margin_at_root: float           # how close the root is to its own limit at stall
    stations: Tuple[Station, ...]


def _panel_interp(y: float, b1: float, b2: float,
                  root: float, mid: float, tip: float) -> float:
    """Piecewise-linear value at y, breaking at the centre-body/wing blend.

    This is the shared spanwise law: identical to the one planform.js uses to build the
    preview mesh, and to the OpenVSP builder's station interpolation. The BREAKPOINT
    matters — chord and twist both kink at y = b1, and an integration grid that straddles
    it silently smooths a real geometric feature."""
    if y <= b1:
        f = (y / b1) if b1 > 0 else 0.0
        return root + (mid - root) * f
    f = ((y - b1) / b2) if b2 > 0 else 0.0
    return mid + (tip - mid) * f


def _mid_twist(p: BWBParams) -> float:
    """Twist at the blend station. Matches planform.js and vsp_builder: the blend twist
    is interpolated by CENTRE_BODY_FRACTION, not by half-span position."""
    return p.root_twist_deg + (p.tip_twist_deg - p.root_twist_deg) * p.centre_body_fraction


def _grid(p: BWBParams) -> List[float]:
    """Cosine-spaced stations on the right half-span, with the panel break included.

    Cosine spacing (y = (b/2)(1 - cos(theta))/2) clusters points at root and tip. The
    tip is where it matters: the elliptic ordinate sqrt(1 - (2y/b)^2) has infinite slope
    there, and a uniform grid under-resolves exactly the region where a swept wing is
    most likely to stall first."""
    half = p.span_m / 2.0
    b1, _ = p.half_widths()
    ys = [half * (1.0 - math.cos(math.pi * i / (N_STATIONS - 1))) / 2.0
          for i in range(N_STATIONS)]
    ys.append(b1)                       # never straddle the chord/twist kink
    return sorted(set(ys))


def cl_max_at(p: BWBParams, y: float, reynolds: float) -> float:
    """Local section CLmax at station y, evaluated at THIS station's Reynolds number.

    Two separate effects, both of which the old single constant erased:

    * The section changes along the span (root -> blend -> tip), so capability changes.
    * Reynolds falls with chord toward the tip — measured on the reference wing, the tip
      runs at ~270k against ~643k at the MAC. CLmax falls with Reynolds (measured:
      naca0012 1.119 at Re 2e5 against ~1.35 at 1e6), so evaluating the tip at the MAC
      Reynolds credits it with a stall limit it never actually sees.

    MODELLING CHOICE, not from TM-948: the capability VALUE is interpolated between the
    bracketing stations' sections. The alternative — blending aerofoil coordinates and
    re-running NeuralFoil per station — would be more faithful and far too slow for a
    function that sits inside the sizing loop."""
    b1, b2 = p.half_widths()
    root = _curve(p.root_airfoil).at(reynolds)[0]
    mid = _curve(p.mid_airfoil).at(reynolds)[0]
    tip = _curve(p.tip_airfoil).at(reynolds)[0]
    return _panel_interp(y, b1, b2, root, mid, tip)


def stations(p: BWBParams, reynolds_mac: float) -> Tuple[Station, ...]:
    """Demand and capability at every integration station (TM-948 pp. 2-3)."""
    half = p.span_m / 2.0
    b1, b2 = p.half_widths()
    c_root, c_mid, c_tip = p.chords()
    s_ref = p.planform_area_m2()
    mac = p.mean_aero_chord_m()
    t_root, t_mid, t_tip = p.root_twist_deg, _mid_twist(p), p.tip_twist_deg

    # Leading-edge x at each station, aft of the root LE apex. Same two-panel sweep law
    # planform.js and vsp_builder use; needed here because S1-D turns local lift into a
    # pitching moment and the ARM is what makes a swept wing's trim different from a
    # straight one's.
    tan_i = math.tan(math.radians(p.sweep_inner_deg))
    tan_o = math.tan(math.radians(p.sweep_outer_deg))

    ys = _grid(p)
    chords = [_panel_interp(y, b1, b2, c_root, c_mid, c_tip) for y in ys]
    twists = [_panel_interp(y, b1, b2, t_root, t_mid, t_tip) for y in ys]
    x_les = [y * tan_i if y <= b1 else b1 * tan_i + (y - b1) * tan_o for y in ys]
    curves = {n: _curve(n) for n in
              (p.root_airfoil, p.mid_airfoil, p.tip_airfoil)}
    c_r, c_m, c_t = (curves[p.root_airfoil], curves[p.mid_airfoil],
                     curves[p.tip_airfoil])

    # PASS 1 — everything that depends only on the local chord Reynolds. The section
    # properties must be sampled before the zero-lift reference can be formed, because
    # that reference is now an integral over them (see below).
    re_locals = [reynolds_mac * (c / mac) if mac > 0 else reynolds_mac for c in chords]
    sampled = []
    for y, re_local in zip(ys, re_locals):
        r, m, t = c_r.at(re_local), c_m.at(re_local), c_t.at(re_local)
        sampled.append(tuple(_panel_interp(y, b1, b2, r[k], m[k], t[k])
                             for k in range(4)))     # clmax, cl_alpha, cm0, alpha_l0

    # AERODYNAMIC twist, not geometric. TM-948 p.2 is explicit that "the angles of
    # attack and twist are reckoned from the zero lift direction" — and when the SECTION
    # changes along the span, each station has its own zero-lift direction. A cambered
    # tip on a symmetric root is aerodynamic WASH-IN even at zero geometric twist.
    #
    # MEASURED on the golden reference, which is why this is a correction and not a
    # refinement: the tip section s5010 has alpha_L0 = -1.35 deg against 0.00 for the
    # symmetric e474 root, so of the 4.0 deg of geometric washout only 2.65 deg is
    # aerodynamically real. CAMBER CANCELS 34% OF THE WASHOUT. Reckoning from geometric
    # twist alone credited this wing with half again as much washout as it has, on the
    # exact lever that decides inboard versus outboard stall.
    aero_twists = [tw - s[3] for tw, s in zip(twists, sampled)]

    # Chord-weighted mean, per TM-948 p.2: the wing's overall zero-lift direction.
    # Referencing to it is what makes the basic loading carry zero NET lift rather than
    # quietly adding or removing lift from the whole wing.
    num = _trapz([t * c for t, c in zip(aero_twists, chords)], ys)
    den = _trapz(chords, ys)
    twist_bar = (num / den) if den > 0 else 0.0

    # PASS 2 — the loading itself.
    out: List[Station] = []
    for y, c, tw, x_le, re_local, s, atw in zip(
            ys, chords, twists, x_les, re_locals, sampled, aero_twists):
        # Semi-ellipse of EQUAL AREA to the chord distribution (TM-948 p.2). Integrating
        # (4S/pi b) sqrt(1-(2y/b)^2) over the span returns exactly S.
        ratio = min(1.0, max(0.0, (y / half) if half > 0 else 0.0))
        c_ell = (4.0 * s_ref / (math.pi * p.span_m)) * math.sqrt(max(0.0, 1.0 - ratio ** 2))

        # Additional loading: arithmetic mean of chord and ellipse, per unit wing CL.
        cl_add = (c + c_ell) / (2.0 * c) if c > 0 else 0.0

        # Basic loading: HALF the local twist loading, because the twist component takes
        # the zero line — not the ellipse — as its ideal distribution (TM-948 p.2). The
        # section lift-curve slope comes from the same committed screen as CLmax.
        cl_basic = 0.5 * s[1] * (atw - twist_bar)

        out.append(Station(y=y, eta=(y / half if half > 0 else 0.0), chord=c,
                           twist_deg=tw, reynolds=re_local, cl_add=cl_add,
                           cl_basic=cl_basic, cl_max=s[0],
                           cm0=s[2], x_c4=x_le + 0.25 * c))
    return tuple(out)


def _trapz(values: List[float], xs: List[float]) -> float:
    return sum((values[i] + values[i + 1]) * (xs[i + 1] - xs[i]) / 2.0
               for i in range(len(xs) - 1))


class _SectionCurve(NamedTuple):
    """One section's CLmax and lift slope sampled on the table's own Reynolds grid.

    PERFORMANCE, and the reason this exists: a naive implementation calls
    `airfoils.section_at` three times per station for CLmax plus once for the lift
    slope. At 80 stations that is ~324 table walks and measured 2.16 ms per solve —
    unacceptable for a function that sits inside the sizing loop at inverse.py:377,
    where the old formula was a cosine and two multiplies.

    The committed table holds only 5 Reynolds points, so each section is evaluated
    once per grid point (15 calls total) and interpolated locally in log(Re) — the
    same interpolation `section_at` performs, just hoisted out of the station loop."""
    log_re: Tuple[float, ...]
    clmax: Tuple[float, ...]
    cl_alpha: Tuple[float, ...]
    cm0: Tuple[float, ...]
    alpha_l0: Tuple[float, ...]

    def at(self, reynolds: float) -> Tuple[float, float, float, float]:
        """(CLmax, lift slope per degree, zero-lift Cm, zero-lift angle deg)."""
        x = math.log(max(reynolds, 1.0))
        if x <= self.log_re[0]:
            return (self.clmax[0], self.cl_alpha[0], self.cm0[0], self.alpha_l0[0])
        if x >= self.log_re[-1]:
            return (self.clmax[-1], self.cl_alpha[-1], self.cm0[-1], self.alpha_l0[-1])
        for i in range(len(self.log_re) - 1):
            lo, hi = self.log_re[i], self.log_re[i + 1]
            if lo <= x <= hi:
                t = (x - lo) / (hi - lo) if hi > lo else 0.0
                return (self.clmax[i] + t * (self.clmax[i + 1] - self.clmax[i]),
                        self.cl_alpha[i] + t * (self.cl_alpha[i + 1] - self.cl_alpha[i]),
                        self.cm0[i] + t * (self.cm0[i + 1] - self.cm0[i]),
                        self.alpha_l0[i] + t * (self.alpha_l0[i + 1] - self.alpha_l0[i]))
        return (self.clmax[-1], self.cl_alpha[-1], self.cm0[-1], self.alpha_l0[-1])


@lru_cache(maxsize=256)
def _curve(name: str) -> _SectionCurve:
    """Cached: the sizing loop re-solves the same three sections thousands of times
    while only the planform moves, so the Reynolds sampling is pure repeated work."""
    grid = sorted(provenance()["re_grid"])
    sampled = [section_at(name, re) for re in grid]
    return _SectionCurve(log_re=tuple(math.log(re) for re in grid),
                         clmax=tuple(s.clmax for s in sampled),
                         cl_alpha=tuple(s.cl_alpha_per_deg for s in sampled),
                         cm0=tuple(s.cm0 for s in sampled),
                         # Zero-lift angle from the measured pair (cl0, dcl/dalpha):
                         # cl(alpha) = cl0 + a*alpha = 0  =>  alpha_L0 = -cl0/a.
                         alpha_l0=tuple(
                             (-s.cl0 / s.cl_alpha_per_deg) if s.cl_alpha_per_deg else 0.0
                             for s in sampled))


def curve_payload(*names: str) -> dict:
    """The minimum a client needs to run this method itself, for the named sections.

    WHY THIS EXISTS. The cockpit recomputes the tier-0 panel in the browser so that
    dragging a slider is instant, which means the browser needs section CLmax and lift
    slope as functions of Reynolds. The committed table is 2.33 MB — shipping it to run
    a 30-number lookup would be absurd, and refusing to ship anything would leave the
    browser on the deleted `CL_MAX_2D` constant, which is how it came to read 2.86%
    optimistic against the server.

    So the server sends only the selected sections, sampled on the table's own Reynolds
    grid: 3 sections x 5 points x 2 quantities. `log_re` is pre-computed because the
    interpolation is linear in log(Re) and the client must not have to know that."""
    out = {}
    for name in names:
        if name in out:
            continue
        c = _curve(name)
        out[name] = {"log_re": list(c.log_re), "clmax": list(c.clmax),
                     "cl_alpha": list(c.cl_alpha), "cm0": list(c.cm0),
                     "alpha_l0": list(c.alpha_l0)}
    return out


def solve_stall(p: BWBParams, reynolds_mac: float) -> StallSolution:
    """Wing CLmax and the station where stall begins.

    Demand at wing lift coefficient CL is

        cl(y) = CL * cl_add(y) + cl_basic(y)

    and stall starts at the first station where that reaches cl_max(y). Solving each
    station for the CL that would stall it and taking the minimum gives both the wing
    CLmax and where it happens:

        CL_max_wing = min over y of [ (cl_max(y) - cl_basic(y)) / cl_add(y) ]
    """
    sts = stations(p, reynolds_mac)
    best_cl = math.inf
    best = sts[0]
    for st in sts:
        if st.cl_add <= 0:
            continue
        cl_stall = (st.cl_max - st.cl_basic) / st.cl_add
        if cl_stall < best_cl:
            best_cl, best = cl_stall, st
    if not math.isfinite(best_cl) or best_cl <= 0:
        raise ValueError(
            f"no station can carry positive lift before stalling (CLmax {best_cl!r}) — "
            f"twist {p.root_twist_deg}/{p.tip_twist_deg} deg may be non-physical")

    root = sts[0]
    margin_root = root.cl_max - (best_cl * root.cl_add + root.cl_basic)
    return StallSolution(cl_max_wing=best_cl, stall_eta=best.eta,
                         stall_is_outboard=best.eta >= OUTBOARD_STALL_FRAC,
                         margin_at_root=margin_root, stations=sts)
