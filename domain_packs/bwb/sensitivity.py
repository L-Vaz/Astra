"""BWB domain pack — finite-difference sensitivities on the tier-0 panel.

Answers "what does +1° of sweep do to my stall speed?" by perturbing each
geometry parameter (and each flight-condition requirement) by a human-scale
step and re-running the closed-form estimate. Central differences where both
perturbed points validate; one-sided (flagged `clamped`) at schema bounds.
Cost: tens of microsecond estimate() calls — safe to run on every edit.
"""
from typing import Dict, List, Optional, Sequence

from pydantic import BaseModel, Field

from .aero_estimates import AeroEstimate, estimate
from .atmosphere import AtmosphereState, atmosphere_at
from .requirements import DesignRequirements
from .schema import BWBParams

# Perturbation sizes users actually think in (one "notch" of the control).
HUMAN_STEPS: Dict[str, float] = {
    "span_m": 0.10, "root_chord_m": 0.02, "centre_body_fraction": 0.02,
    "mid_chord_frac": 0.02, "taper_outer": 0.05,
    "sweep_inner_deg": 1.0, "sweep_outer_deg": 1.0,
    # S1-C: no thickness entries. Thickness comes from the section, so "one notch of
    # the control" is meaningless — there is no control, and nudging t/c by 1% would
    # mean silently swapping the aerofoil for a different one.
    "root_twist_deg": 0.5, "tip_twist_deg": 0.5, "dihedral_deg": 1.0,
}
STEP_UNITS: Dict[str, str] = {
    "span_m": "m", "root_chord_m": "m", "centre_body_fraction": "",
    "mid_chord_frac": "", "taper_outer": "",
    "sweep_inner_deg": "deg", "sweep_outer_deg": "deg",
    "root_twist_deg": "deg", "tip_twist_deg": "deg", "dihedral_deg": "deg",
}
METRICS = ["ld_cruise", "cd_cruise", "cd0", "cl_cruise", "cl_max_3d",
           "v_stall_ms", "x_np_m", "oswald_e", "s_ref_m2", "aspect_ratio"]

# Flight-condition steps for the requirements-side table.
REQ_STEPS: Dict[str, float] = {
    "cruise_speed_ms": 1.0, "mtow_kg": 0.25, "cruise_altitude_m": 250.0,
}
REQ_STEP_UNITS: Dict[str, str] = {
    "cruise_speed_ms": "m/s", "mtow_kg": "kg", "cruise_altitude_m": "m",
}


class SensitivityRow(BaseModel):
    param: str
    label: str = ""
    step: float
    step_unit: str = ""
    deltas: Dict[str, float] = Field(
        description="metric -> change in that metric for +step of the param")
    clamped: bool = Field(
        default=False,
        description="True when a schema bound forced a one-sided difference")


def _panel(params: BWBParams, atmosphere: AtmosphereState, v: float, w: float,
           launch: Optional[str]) -> AeroEstimate:
    return estimate(params, atmosphere, v, w, launch)


def _try_params(base: BWBParams, key: str, value: float) -> Optional[BWBParams]:
    try:
        return BWBParams.model_validate({**base.model_dump(), key: value})
    except ValueError:  # pydantic ValidationError subclasses ValueError
        return None


def sensitivity_table(params: BWBParams, atmosphere: AtmosphereState,
                      v: float, weight_n: float,
                      launch: Optional[str] = None,
                      metrics: Sequence[str] = METRICS) -> List[SensitivityRow]:
    """d(metric) for +HUMAN_STEP of each geometry parameter.
    Central difference estimate(p ± step/2); degrades to one-sided at bounds."""
    rows: List[SensitivityRow] = []
    center = _panel(params, atmosphere, v, weight_n, launch)
    for key, step in HUMAN_STEPS.items():
        x0 = float(getattr(params, key))
        lo = _try_params(params, key, x0 - step / 2)
        hi = _try_params(params, key, x0 + step / 2)
        clamped = False
        if lo is not None and hi is not None:
            p_lo, p_hi, scale = lo, hi, 1.0
        elif hi is not None:
            p_lo, p_hi, scale, clamped = None, hi, 2.0, True
        elif lo is not None:
            p_lo, p_hi, scale, clamped = lo, None, 2.0, True
        else:
            full_hi = _try_params(params, key, x0 + step)
            full_lo = _try_params(params, key, x0 - step)
            if full_hi is not None:
                p_lo, p_hi, scale, clamped = None, full_hi, 1.0, True
            elif full_lo is not None:
                p_lo, p_hi, scale, clamped = full_lo, None, 1.0, True
            else:
                continue  # boxed in on both sides — no derivative available
        e_lo = _panel(p_lo, atmosphere, v, weight_n, launch) if p_lo else center
        e_hi = _panel(p_hi, atmosphere, v, weight_n, launch) if p_hi else center
        deltas = {m: (getattr(e_hi, m) - getattr(e_lo, m)) * scale for m in metrics}
        rows.append(SensitivityRow(param=key, step=step,
                                   step_unit=STEP_UNITS.get(key, ""),
                                   deltas=deltas, clamped=clamped))
    return rows


def requirement_sensitivity_table(
        req: DesignRequirements, params: BWBParams,
        metrics: Sequence[str] = METRICS) -> List[SensitivityRow]:
    """d(metric) for +REQ_STEP of each flight-condition input, geometry held
    fixed — answers 'what does flying 1 m/s faster do to this design'."""
    rows: List[SensitivityRow] = []

    def panel_at(speed: float, mtow: float, alt: float) -> AeroEstimate:
        return estimate(params, atmosphere_at(alt), speed, mtow * 9.81, req.launch)

    base = dict(speed=req.cruise_speed_ms, mtow=req.mtow_kg,
                alt=req.cruise_altitude_m)
    arg_of = {"cruise_speed_ms": "speed", "mtow_kg": "mtow",
              "cruise_altitude_m": "alt"}
    for key, step in REQ_STEPS.items():
        arg = arg_of[key]
        lo = {**base, arg: base[arg] - step / 2}
        hi = {**base, arg: base[arg] + step / 2}
        center = panel_at(**base)
        clamped = False
        try:
            e_lo = panel_at(**lo)
        except ValueError:
            e_lo = center
            clamped = True
        try:
            e_hi = panel_at(**hi)
        except ValueError:
            e_hi = center
            clamped = True
        scale = 2.0 if clamped else 1.0
        deltas = {
            m: (getattr(e_hi, m) - getattr(e_lo, m)) * scale for m in metrics
        }
        rows.append(SensitivityRow(param=key, step=step,
                                   step_unit=REQ_STEP_UNITS.get(key, ""),
                                   deltas=deltas, clamped=clamped))
    return rows
