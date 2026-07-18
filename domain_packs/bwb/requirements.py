"""BWB domain pack — first-class engineering requirements.

The requirements-first explorer treats these as the ONLY primary inputs:
flight condition plus aero targets. Geometry is a consequence solved by
domain_packs/bwb/inverse.py, never a prerequisite. A requirement of None
means "no constraint — just report the achieved value".

Pydantic-only; safe to import from both the FoamAgent conda env (py3.12)
and the OpenVSP venv (py3.10).
"""
from typing import Literal, Optional

from pydantic import BaseModel, Field


class DesignRequirements(BaseModel):
    """The user's engineering requirements — editable at any time.

    Semantics (deterministic, enforced by inverse.solve_requirements):
      cl_cruise_target  equality: wing area solves S = W/(q*CL_target);
                        when absent the wing is sized to CL* (best L/D)
      ld_min            objective: aspect ratio rises (within gates and the
                        span cap) until met, else the binding limit is named
      v_stall_max_ms    HARD floor on wing area; the effective cap is
                        min(this, launch-method tolerance)
      span_max_m        HARD cap on span (transport/handling)
    """
    # -- flight condition (seeded from the mission when one exists) ----------
    mtow_kg: float = Field(gt=0, le=25, description="Max takeoff mass, kg")
    cruise_speed_ms: float = Field(ge=8, le=60, description="Cruise speed, m/s")
    cruise_altitude_m: float = Field(default=100.0, ge=0, le=4000,
                                     description="Cruise altitude AMSL, m")
    launch: Literal["hand", "catapult", "runway"] = "hand"

    # -- targets (None = no requirement) --------------------------------------
    cl_cruise_target: Optional[float] = Field(
        default=None, gt=0.05, le=0.75,
        description="Equality target for cruise CL; None -> size to CL* (best L/D)")
    ld_min: Optional[float] = Field(
        default=None, gt=0, le=40,
        description="Minimum cruise L/D; solver raises AR until met or a limit binds")
    v_stall_max_ms: Optional[float] = Field(
        default=None, ge=5, le=20,
        description="Hard stall-speed cap, m/s; effective cap also honours the launch method")
    span_max_m: Optional[float] = Field(
        default=None, ge=0.8, le=3.0,
        description="Hard span cap, m")


class RequirementStatus(BaseModel):
    """One requirement's target vs achieved, after a solve or a forward check."""
    name: str                              # "ld_min", "v_stall_max_ms", ...
    label: str                             # human label for the UI
    unit: str = ""
    target: Optional[float] = None
    achieved: float
    met: Optional[bool] = None             # None when no target was set
    margin: Optional[float] = Field(
        default=None, description="Signed toward 'good': positive = margin in hand")
