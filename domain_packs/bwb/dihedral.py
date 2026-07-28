"""Effective dihedral: how much roll a swept wing gets from a sideslip (S1-H).

WHY A TAILLESS DESIGNER CARES
-----------------------------
"Dihedral effect" is the rolling moment an aircraft develops when it sideslips —
formally the derivative Cl_beta, roll due to sideslip. It is what rights an aeroplane
after a gust, and too much of it is as bad as too little: excess dihedral effect
combined with weak directional stability gives Dutch roll, and it eats aileron
authority exactly when you are slow and need it.

A swept wing produces dihedral effect WITHOUT any geometric dihedral. Sideslip makes
the windward panel present a smaller effective sweep angle to the oncoming flow, so it
sees a higher effective velocity component perpendicular to its own quarter-chord line
and lifts more; the leeward panel lifts less. The difference rolls the aircraft away
from the sideslip, which is what positive dihedral does. Because the mechanism works on
the LIFT the panels are already making, the effect scales with CL — it is strong when
slow and weak when fast, which is precisely the behaviour a thermal-soaring flying wing
wants and a conventional tailed aircraft has to engineer around.

THE EQUATION
------------
For a linearly tapered wing, the increment due to sweep is

    Cl_beta_sweep = -[(1 + 2*lam) / (3 * (1 + lam))] * CL * tan(Lambda_c/2)

    lam    = taper ratio
    CL     = wing lift coefficient
    Lambda = sweep measured AT THE HALF CHORD

Barnes W. McCormick, "Aerodynamics, Aeronautics, and Flight Mechanics", John Wiley &
Sons, 1979, p. 547 — reproduced with its derivation and two design charts by Bill and
Bunny Kuhlman, "Swept Wings and Effective Dihedral", RC Soaring Digest, January-March
2000, PDF pp. 15-18 (the accessible copy read for this implementation).

UNITS — THE PART THAT HAD TO BE RECONCILED BEFORE WRITING ANY CODE
------------------------------------------------------------------
The same article carries a conversion: "a Cl_beta value of -0.00021 is roughly
equivalent to one degree of positive dihedral". Those two numbers are in DIFFERENT
units, and mixing them is a factor-of-57 error that would look like a plausible answer.

Resolved numerically rather than assumed, by reproducing the article's own charts:

    lam = 0.6, Lambda = 30 deg, CL = 1.2
      treating the equation as per-RADIAN:   26.4 degrees of effective dihedral
      treating it as per-DEGREE:           1512 degrees of effective dihedral

Graph 3 (taper 0.6) and Graph 4 (taper 1.0) both run their effective-dihedral axis to
30 degrees, and both reach their top-right corner at Lambda = 30, CL = 1.2. The
per-radian reading lands at 26.4 and 28.8 degrees for the two tapers — just under the
axis maximum on both charts. The per-degree reading is off by a factor of 57.3.

Second, independent check. The article exists partly to dispute the folk rule that
"three degrees of sweep is equivalent to one degree of geometric dihedral". Under the
per-radian reading that rule comes out exact at CL = 0.50 (taper 0.6) or CL = 0.46
(taper 1.0) — ordinary cruise lift coefficients, which is exactly why a rule of thumb
that ignores CL appears to work at one speed and fails at every other. Under the
per-degree reading it would be exact at CL = 0.0087, which no aircraft flies at.

So: THE EQUATION RETURNS Cl_beta PER RADIAN. The -0.00021 conversion is PER DEGREE. The
57.2958 conversion between them is applied here, once, in `effective_dihedral_deg`.

SIGN
----
Cl_beta NEGATIVE is positive (stabilising) dihedral effect — that is the sign
convention the -0.00021 conversion is written in. Aft sweep gives tan(Lambda) > 0, so
Cl_beta comes out negative and the effective dihedral positive. A forward-swept half
chord flips it, and this schema reaches that: the quarter-chord sweep already goes
negative in 5.4% of the legal envelope. Nothing here takes an absolute value.

WHAT IS AND IS NOT INCLUDED
---------------------------
Included: the sweep contribution, and the geometric dihedral, which adds one for one
because "degrees of effective dihedral" is DEFINED as the geometric dihedral that would
produce the same Cl_beta.

NOT included, and each would add to the total on a real airframe: winglets (Kuhlman
gives EDA = 20*h_w/s from Nickel and Wohlfahrt p. 108 — this schema has no winglets),
wing vertical position on a fuselage (an all-wing aircraft has none), a vertical tail
(none), and swept flap hinge lines (none in Stage 1).

REPORTED, NOT OPTIMISED. `dihedral_deg` stays where the user set it. Stage 2 has a
measured Cl_beta from the VLM and can optimise against that; this exists so Stage 1 can
say what the planform is already doing, which is not small — and so that a concept
whose sweep is quietly supplying 10 degrees of dihedral effect at low speed does not
reach the MDO looking like a 2-degree aircraft.
"""
from __future__ import annotations

import math
from typing import NamedTuple

from .aero_estimates import effective_c2_sweep_deg
from .schema import BWBParams

CL_BETA_PER_DEGREE_OF_DIHEDRAL = -0.00021
"""Cl_beta, PER DEGREE of sideslip, equivalent to one degree of positive dihedral.

`[B]` Kuhlman, RC Soaring Digest Jan-Mar 2000, PDF p. 18: "a Cl_beta value of -0.00021
is roughly equivalent to one degree of positive dihedral". An independent NACA-era
statement of the same convention gives -0.0002 per degree, so the two agree to the
precision either is quoted at. The word "roughly" is the author's: this is a
conventional equivalence for a typical wing, not an identity, and it sets the accuracy
of every number in this module."""


class EffectiveDihedral(NamedTuple):
    """Dihedral effect, split into where it comes from."""
    total_deg: float
    geometric_deg: float
    sweep_deg: float
    cl_beta_per_rad: float
    """The underlying derivative, per RADIAN of sideslip, from the McCormick equation
    plus the geometric part converted back. Negative is stabilising."""
    half_chord_sweep_deg: float
    taper_ratio: float
    cl: float


def sweep_cl_beta_per_rad(taper_ratio: float, cl: float,
                          half_chord_sweep_deg: float) -> float:
    """McCormick p. 547: the sweep increment to Cl_beta, per radian of sideslip."""
    lam = taper_ratio
    return -((1.0 + 2.0 * lam) / (3.0 * (1.0 + lam))) * cl * math.tan(
        math.radians(half_chord_sweep_deg))


def sweep_contribution_deg(taper_ratio: float, cl: float,
                           half_chord_sweep_deg: float) -> float:
    """The same increment expressed in DEGREES of equivalent dihedral.

    The 57.2958 is the whole reconciliation: the equation above is per radian, the
    -0.00021 equivalence is per degree. See the module docstring for the two numerical
    checks that establish which is which."""
    per_deg = sweep_cl_beta_per_rad(taper_ratio, cl, half_chord_sweep_deg) / math.degrees(1.0)
    return per_deg / CL_BETA_PER_DEGREE_OF_DIHEDRAL


def effective_dihedral(params: BWBParams, cl: float) -> EffectiveDihedral:
    """Total effective dihedral at a lift coefficient, and its two sources.

    TAPER RATIO IS AN APPROXIMATION HERE, and it is the one modelling choice this
    function makes. McCormick's equation is for a LINEARLY tapered wing; a BWB has two
    panels with a chord kink at the blend, so there is no single taper ratio. The tip/root
    ratio is used, because that is what "taper ratio" means for the wing as a whole and
    because the equation's dependence on it is weak — the factor (1+2*lam)/(3(1+lam))
    moves only from 0.333 at lam = 0 to 0.500 at lam = 1, a 50% span across the entire
    possible range, while tan(sweep) and CL each vary by more than that within this
    schema alone.
    """
    c_root, _c_mid, c_tip = params.chords()
    lam = (c_tip / c_root) if c_root > 0 else 0.0
    sweep_c2 = effective_c2_sweep_deg(params)

    from_sweep = sweep_contribution_deg(lam, cl, sweep_c2)
    from_geometry = params.dihedral_deg
    total = from_sweep + from_geometry

    # Back out the underlying derivative so a consumer that wants Cl_beta itself does
    # not have to re-derive the unit conversion and get it wrong.
    cl_beta_per_deg = total * CL_BETA_PER_DEGREE_OF_DIHEDRAL
    return EffectiveDihedral(
        total_deg=total, geometric_deg=from_geometry, sweep_deg=from_sweep,
        cl_beta_per_rad=cl_beta_per_deg * math.degrees(1.0),
        half_chord_sweep_deg=sweep_c2, taper_ratio=lam, cl=cl)
