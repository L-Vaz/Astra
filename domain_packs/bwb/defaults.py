"""BWB domain pack — mission-to-parameters engineering rules.

Deterministic sizing: every number either comes from the mission or is a
defensible small-UAV default, and every default is written into the
assumptions list so the user can audit (and override) it.
"""
import math
from typing import List, Optional

from .atmosphere import atmosphere_at
from .schema import (DEFAULT_MID_AIRFOIL, DEFAULT_ROOT_AIRFOIL,
                     DEFAULT_TIP_AIRFOIL, BWBParams, MissionDerived, MissionSpec)

# Typical wing loading for small fixed-wing UAVs by launch method, kg/m^2
WING_LOADING = {"hand": 7.0, "catapult": 10.0, "runway": 13.0}
DEFAULT_AR = 5.5           # BWBs sit lower than sailplanes, higher than flying plates
PAYLOAD_FRACTION = 0.30    # payload / MTOW for small UAVs when MTOW not given
MAX_CRUISE_CL = 0.75       # above this, cruise sits too close to stall


def isa_density(altitude_m: float) -> float:
    """Compatibility wrapper for callers that only need standard density."""
    return atmosphere_at(altitude_m).density_kgm3


def derive(mission: MissionSpec) -> MissionDerived:
    assumptions: List[str] = []

    mtow = mission.mtow_kg
    if mtow is None:
        mtow = mission.payload_kg / PAYLOAD_FRACTION
        assumptions.append(
            f"MTOW not given; assumed payload fraction {PAYLOAD_FRACTION:.0%} -> MTOW {mtow:.2f} kg")

    atmosphere = atmosphere_at(mission.cruise_altitude_m)
    rho = atmosphere.density_kgm3
    weight_n = mtow * 9.81

    wl = WING_LOADING[mission.launch]
    assumptions.append(
        f"Wing loading {wl} kg/m^2 assumed (typical {mission.launch}-launched small UAV)")

    s_ref = mtow / wl
    q = 0.5 * rho * mission.cruise_speed_ms ** 2
    required_cl = weight_n / (q * s_ref)

    # If cruise CL is uncomfortably high, grow the wing until it is not.
    if required_cl > MAX_CRUISE_CL:
        s_ref = weight_n / (q * MAX_CRUISE_CL)
        wl = mtow / s_ref
        required_cl = MAX_CRUISE_CL
        assumptions.append(
            f"Required cruise CL exceeded {MAX_CRUISE_CL}; wing grown to {s_ref:.3f} m^2 "
            f"(wing loading now {wl:.1f} kg/m^2)")

    ar = DEFAULT_AR
    assumptions.append(f"Aspect ratio {ar} assumed (typical BWB compromise)")
    span = math.sqrt(ar * s_ref)

    params = _planform_from(span, s_ref)
    mac = params.mean_aero_chord_m()
    re_mac = mission.cruise_speed_ms * mac / atmosphere.kinematic_viscosity_m2s

    assumptions.append("Turbulence model kOmegaSST with wall functions (external aero default)")
    assumptions.append("Static margin target 8% MAC (tailless BWB needs washout + swept tips)")

    return MissionDerived(
        mission=mission,
        atmosphere=atmosphere,
        mtow_kg=mtow,
        air_density_kgm3=rho,
        weight_n=weight_n,
        wing_loading_kgm2=wl,
        s_ref_target_m2=s_ref,
        required_cl=required_cl,
        reynolds_mac=re_mac,
        params=params,
        assumptions=assumptions,
    )


def _planform_from(span: float, s_ref: float,
                   centre_body_fraction: float = 0.30,
                   mid_chord_frac: float = 0.55,
                   taper_outer: float = 0.45) -> BWBParams:
    """Solve root chord so the two-trapezoid planform hits s_ref exactly."""
    b1 = centre_body_fraction * span / 2.0
    b2 = span / 2.0 - b1
    # S = 2*[b1*(cr + cr*m)/2 + b2*(cr*m + cr*m*t)/2] = cr * k
    m, t = mid_chord_frac, taper_outer
    k = b1 * (1 + m) + b2 * (m + m * t)
    root_chord = s_ref / k

    return BWBParams(
        span_m=span,
        root_chord_m=root_chord,
        centre_body_fraction=centre_body_fraction,
        mid_chord_frac=mid_chord_frac,
        taper_outer=taper_outer,
        sweep_inner_deg=38.0,
        sweep_outer_deg=22.0,
        # S1-C: the section owns thickness; these give 14.07 / 12.09 / 9.82 %,
        # the closest available to the 14 / 9 this used to hard-code.
        root_airfoil=DEFAULT_ROOT_AIRFOIL,
        mid_airfoil=DEFAULT_MID_AIRFOIL,
        tip_airfoil=DEFAULT_TIP_AIRFOIL,
        root_twist_deg=2.0,
        tip_twist_deg=-3.0,
        dihedral_deg=2.0,
    )
