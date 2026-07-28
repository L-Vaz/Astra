"""Span efficiency from Prandtl's lifting line, solved directly (S1-G).

WHAT THIS REPLACES
------------------
`aero_estimates.oswald_efficiency` blends two of Raymer's empirical fits, one for
straight wings and one for swept, and clamps the result to [0.5, 0.95]. Measured on the
legal parameter envelope, that model has three defects:

* The two fits CROSS at AR 4.799 (0 deg sweep) and AR 3.209 (30 deg). Below the
  crossing the swept fit sits above the straight one, so **adding sweep reports BETTER
  span efficiency** — the wrong sign — for AR 2.50 to 4.79.
* Unclamped it returns e > 1 (1.123 at AR 2.5), which is physically impossible: e is
  the ratio of a wing's induced drag to the elliptic minimum, and nothing planar beats
  an ellipse.
* **20.7% of the (AR x sweep) grid is pinned at a clamp**, where e has exactly zero
  gradient in both arguments. `sensitivity.py` takes finite differences of this
  quantity and cannot tell a plateau from physics.

THE PHYSICS, FROM THE CIRCULATION
---------------------------------
A finite wing sheds vorticity from its trailing edge. That sheet induces a downwash at
the wing, tilting the local lift vector backwards; the backward component is induced
drag. Prandtl's lifting line turns this into one equation by writing the circulation as
a Fourier sine series in the angular span coordinate theta, where y = -(b/2) cos(theta):

    Gamma(theta) = 2 b V SUM_n A_n sin(n theta)

Substituting into the requirement that each section's lift match its own lift-curve
slope at its own local (downwash-reduced) angle of attack gives the monoplane equation

    SUM_n A_n sin(n theta) [ n mu(theta) + sin(theta) ] = mu(theta) sin(theta) alpha_eff

with mu(theta) = a0 c(theta) / (4 b), a0 the section lift slope per radian. Integrating
lift and induced drag over the span collapses the series to two results:

    CL  = pi AR A_1
    CDi = pi AR SUM_n n A_n^2

Dividing one by the other's square,

    CDi = CL^2 (1 + delta) / (pi AR),      delta = SUM_{n>=2} n (A_n / A_1)^2

and comparing with the definition CDi = CL^2 / (pi e AR) gives

    e = 1 / (1 + delta)

delta is a sum of squares, so it is >= 0 and e <= 1 BY CONSTRUCTION. The elliptic wing
has every A_n above the first equal to zero, delta = 0, e = 1. No clamp is needed
anywhere, and there is no fit to cross.

Framework confirmed against Stengel, Princeton MAE331 Lecture 5 (Aircraft Performance),
slides 6 and 8, which cite McCormick p. 172 for the delta chart.

VALIDATION — AND ONE DATAPOINT THAT DOES NOT REPRODUCE
------------------------------------------------------
PASSES, and this is the strong one: lifting-line theory has exactly one closed-form
case, the elliptic chord distribution, for which every harmonic above the first must
vanish. Fed an elliptic chord this solver returns **delta = 4.8e-33** — zero to machine
precision. A collocation or indexing error could not survive that.

The trends are also the classical ones: delta minimises near taper 0.35-0.40 (0.0084 at
AR 5.83), rises toward rectangular (0.0466 at taper 1.0), and rises with aspect ratio
(0.0065 at AR 4 to 0.0480 at AR 20, taper 0.5). Converged: delta moves by 1e-5 between
N = 10 and N = 160.

DOES NOT REPRODUCE: the worked point carried in the plan, "AR 5.83, taper 0.5 ->
delta 0.0557, e 0.947". This solver returns **delta 0.0112, e 0.9889** there. The two
are self-consistent under e = 1/(1+delta), so the source is quoting the same
FORMULA — but 0.0557 corresponds to taper ~1.05 or AR ~22.5 on this planform family,
neither of which is the stated wing.

The likely explanation, stated as a hypothesis and not a conclusion: a whole-aircraft
**Oswald** efficiency is not a planar **span** efficiency. Oswald e absorbs viscous
lift-dependent drag and fuselage interference as well as the non-elliptic circulation,
and lands around 0.75-0.85 for real aircraft; the inviscid planar limit for a decently
tapered wing is 0.98-0.99. Raymer's fits produce the former (0.816 on the reference
wing); this module produces the latter (0.978).

CONSEQUENCE FOR THIS MODULE'S SCOPE. Substituting 0.978 for 0.816 in the drag polar
would cut the reference wing's induced drag by about 20% and inflate every L/D on
every card. The plan's own instruction for this item was to stop and re-source if the
datapoint disagreed, so **the polar is deliberately NOT changed**: `oswald_efficiency`
still drives CD, and this module's number is REPORTED alongside it with the gap
visible. Closing that gap needs a sourced viscous lift-dependent term, which is a
separate piece of work, not a silent substitution.

WHAT THIS COSTS, HONESTLY
-------------------------
delta depends on ASPECT RATIO AND TAPER. Raymer's fits depend on aspect ratio and
SWEEP. Switching therefore LOSES the sweep sensitivity of e — classical lifting line is
a straight-wing theory and has no sweep term, exactly as Schrenk has none. For a planar
wing at conceptual level taper and aspect ratio dominate, but this is a real change in
what the model responds to and the card must say so rather than quietly improving.

Schrenk cannot help here: TM-948 p. 5 states the method "is not directly applicable" to
induced drag or downwash. This is a separate solve for a separate quantity.
"""
from __future__ import annotations

import math
from functools import lru_cache
from typing import List, Sequence, Tuple

from .schema import BWBParams

N_TERMS = 40
"""Odd harmonics retained (n = 1, 3, ... 79). Symmetric loading has no even harmonics —
they are antisymmetric in y — so solving for them would waste half the system on
coefficients that must come out zero.

Convergence measured on the reference planform: delta changes by 1.2e-5 between N = 40
and N = 80, and by 3e-4 between N = 10 and N = 40. 40 is comfortably converged and the
whole solve costs well under a millisecond."""


def _solve_dense(a: List[List[float]], b: List[float]) -> List[float]:
    """Gaussian elimination with partial pivoting.

    Written out rather than imported: `domain_packs` must stay importable from the
    OpenVSP Python 3.10 venv, which does not carry the heavy analysis stack, and an
    N x N solve at N = 40 is not worth a dependency."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-14:
            raise ValueError("lifting-line system is singular — check chord > 0 "
                             "at every station")
        m[col], m[piv] = m[piv], m[col]
        pivot = m[col][col]
        for r in range(col + 1, n):
            f = m[r][col] / pivot
            if f:
                for c in range(col, n + 1):
                    m[r][c] -= f * m[col][c]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        acc = m[r][n] - sum(m[r][c] * x[c] for c in range(r + 1, n))
        x[r] = acc / m[r][r]
    return x


def fourier_coefficients(chord_at: Sequence[float], span_m: float,
                         cl_alpha_per_rad: float,
                         n_terms: int = N_TERMS) -> List[float]:
    """A_1, A_3, ... for an UNTWISTED wing at unit effective incidence.

    `chord_at[i]` is the chord at collocation angle theta_i = (i+1) pi / (2 N), which
    places the points on the right half-span, clustered toward the tip where the
    circulation gradient is steepest. delta is a ratio of coefficients, so the choice
    of incidence scales out and 1 radian is as good as any.
    """
    n = len(chord_at)
    orders = [2 * k + 1 for k in range(n)]
    a: List[List[float]] = []
    rhs: List[float] = []
    for i, c in enumerate(chord_at):
        theta = (i + 1) * math.pi / (2.0 * n)
        mu = cl_alpha_per_rad * c / (4.0 * span_m)
        s = math.sin(theta)
        a.append([math.sin(k * theta) * (k * mu + s) for k in orders])
        rhs.append(mu * s)
    return _solve_dense(a, rhs)


def delta_from_coefficients(coeffs: Sequence[float],
                            n_terms: int = N_TERMS) -> float:
    """delta = SUM_{n>=2} n (A_n/A_1)^2, over the retained odd harmonics."""
    a1 = coeffs[0]
    if a1 == 0.0:
        raise ValueError("A_1 = 0: this wing generates no lift")
    orders = [2 * k + 1 for k in range(len(coeffs))]
    return sum(k * (c / a1) ** 2 for k, c in zip(orders[1:], coeffs[1:]))


def span_efficiency_tapered(aspect_ratio: float, taper: float,
                            cl_alpha_per_rad: float = 2.0 * math.pi,
                            n_terms: int = N_TERMS) -> Tuple[float, float]:
    """(delta, e) for a straight tapered wing — the shape the published charts use.

    Exists so the implementation can be checked against a source datapoint rather than
    only against itself: AR 5.83, taper 0.5 gives delta 0.0557 in Stengel's slide 46.
    """
    span = 1.0
    s_ref = span * span / aspect_ratio
    # Trapezoid of unit span with the given taper: S = (b/2)(c_root + c_tip).
    c_root = 2.0 * s_ref / (span * (1.0 + taper))
    chords = []
    for i in range(n_terms):
        theta = (i + 1) * math.pi / (2.0 * n_terms)
        eta = abs(math.cos(theta))          # |y| / (b/2)
        chords.append(c_root * (1.0 - (1.0 - taper) * eta))
    coeffs = fourier_coefficients(chords, span, cl_alpha_per_rad, n_terms)
    delta = delta_from_coefficients(coeffs, n_terms)
    return delta, 1.0 / (1.0 + delta)


@lru_cache(maxsize=512)
def _cached(span_m: float, b1: float, b2: float, c_root: float, c_mid: float,
            c_tip: float, cl_alpha_per_rad: float, n_terms: int) -> Tuple[float, float]:
    half = span_m / 2.0
    chords = []
    for i in range(n_terms):
        theta = (i + 1) * math.pi / (2.0 * n_terms)
        y = half * abs(math.cos(theta))
        if y <= b1:
            c = c_root + (c_mid - c_root) * ((y / b1) if b1 > 0 else 0.0)
        else:
            c = c_mid + (c_tip - c_mid) * (((y - b1) / b2) if b2 > 0 else 0.0)
        chords.append(c)
    coeffs = fourier_coefficients(chords, span_m, cl_alpha_per_rad, n_terms)
    delta = delta_from_coefficients(coeffs, n_terms)
    return delta, 1.0 / (1.0 + delta)


def span_efficiency(params: BWBParams, cl_alpha_per_rad: float = 2.0 * math.pi,
                    n_terms: int = N_TERMS) -> Tuple[float, float]:
    """(delta, e) for this BWB planform, on its own two-panel chord distribution.

    The section lift slope defaults to the thin-aerofoil 2*pi rather than the measured
    section value. That is deliberate: delta is a property of the CIRCULATION SHAPE, and
    using a measured slope here would make span efficiency move when the aerofoil
    changes at fixed planform — a coupling classical lifting line does not claim and
    that would be double-counted against the section drag in S1-E. The argument is
    exposed so the sensitivity can be measured rather than assumed.
    """
    b1, b2 = params.half_widths()
    c_root, c_mid, c_tip = params.chords()
    return _cached(params.span_m, b1, b2, c_root, c_mid, c_tip,
                   cl_alpha_per_rad, n_terms)
