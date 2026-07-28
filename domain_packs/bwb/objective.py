"""BWB domain pack — multidisciplinary objective (MDO score).

Deterministic scoring function modelled on the Neuronautics "organic flying wing"
workflow:

    score = aero_efficiency - Σ wᵢ · penaltyᵢ

The full video-style term structure is preserved. The five structural terms
(w1..w5: root bending moment, deflection, peak stress, stress uniformity, 3D
shaping) are *present but dormant* — their weights default to 0.0 and their raw
values are placeholders until the equivalent-beam surrogate (block B,
`beam_structure.py`) lands in a later phase. Enabling structures then is a matter
of raising the weights; no term needs to be added.

No LLM anywhere in this path: the score and every penalty are closed-form and
returned as an inspectable breakdown. Feasibility reuses the geometry gate
(`BWBParams` validators) plus the hard stability floors the search must respect.

This module imports only pydantic/math and the light result/param types — it does
NOT import aerosandbox — so `objective.py` unit tests run without the heavy
aero stack. The caller supplies an already-computed `AsbAeroResult`.
"""
from __future__ import annotations

import math
from typing import Dict, Optional

from pydantic import BaseModel, Field

from .asb_aero import AsbAeroResult
from .schema import BWBParams

# Structural terms are dormant in v1: their beam surrogate (block B) is not built,
# so their raw value is a placeholder and their default weight is 0.
_STRUCTURAL_TERMS = ("m_root", "u_structure", "sigma_peak", "sigma_cv", "shape_3d")


class ObjectiveWeights(BaseModel):
    """Penalty weights wᵢ. Structural weights default to 0 (dormant in v1).

    Weights are applied to normalised, non-negative penalty terms so that a
    weight is a dimensionless "how much do I care" knob, comparable across terms.
    """
    # --- structural block (dormant: build block B before raising these) ---
    w1_m_root: float = Field(default=0.0, ge=0, description="root bending moment |M_root|")
    w2_u_structure: float = Field(default=0.0, ge=0, description="structural deflection / compliance")
    w3_sigma_peak: float = Field(default=0.0, ge=0, description="peak stress (failure proxy)")
    w4_sigma_cv: float = Field(default=0.0, ge=0, description="stress non-uniformity (CoV)")
    w5_shape_3d: float = Field(default=0.0, ge=0, description="3D-shaping / dihedral material penalty")
    # --- aero / stability / trim / geometry block (active) ---
    w6_latdir: float = Field(default=1.0, ge=0, description="lateral-directional stability")
    w7_pitch: float = Field(default=1.0, ge=0, description="pitch static-margin adherence")
    w8_trim: float = Field(default=0.5, ge=0, description="trim effort (control deflection proxy)")
    w9_geometry: float = Field(default=2.0, ge=0, description="geometry constraints (area, gates)")
    w10_stall_station: float = Field(
        default=1.0, ge=0,
        description="stall beginning outboard (pitch-up departure risk)")
    """[D] ENGINEERING JUDGMENT on the WEIGHT, not on the physics.

    The physics is not judgment: on a swept wing, lift lost outboard is lift lost AFT
    of the centre of gravity, so the centre of lift moves forward and the aircraft
    pitches nose-UP — further into the stall. A conventional aircraft recovers with the
    tail. A tailless BWB has none, which is why this configuration's characteristic
    accident is a departure at low speed, and why the maintainer asked for it to be
    penalised rather than merely reported.

    The 1.0 is the judgment. Its units are L/D, because the reward term is L/D: a wing
    that stalls at the very tip loses 1.0 from a score of ~15, so it must buy back ~7%
    in cruise efficiency to break even. SENSITIVITY: at 0.5 the term rarely changes a
    ranking; at 5.0 it dominates and the search will trade real cruise performance for
    stall station. FALSIFIED IF a tier-1 VLM run with a stalled-section model shows the
    pitch-up increment is small for these planforms, or if it shows the opposite — that
    the departure is abrupt enough that a soft penalty is the wrong instrument and this
    should be a hard rejection."""


class ObjectiveConfig(BaseModel):
    """Mission-derived targets and constraint floors. Populated from MissionDerived
    so the objective is self-contained and testable."""
    required_cl: float = Field(description="cruise CL the design must trim at")
    static_margin_target: float = Field(default=0.08, description="desired (x_np - x_cg)/MAC")
    s_ref_target_m2: float = Field(gt=0, description="reference-area constraint target")
    # hard feasibility floors (design rejected, not merely penalised)
    cn_beta_floor: float = Field(
        default=0.0,
        description="minimum weathercock stability Cn_beta [1/rad]; set to the "
                    "reference wing's value so no design is less stable than it")
    enforce_latdir_floor: bool = Field(
        default=False,
        description="hard-reject designs below cn_beta_floor. Off by default: the "
                    "floor is reference-relative and only meaningful once the "
                    "reference wing's Cn_beta has been measured (set during search "
                    "setup). The w6 penalty rewards directional stability regardless.")
    min_static_margin: float = Field(
        default=0.0, description="pitch must be at least neutrally stable (SM > this)")
    area_tol_frac: float = Field(
        default=0.30, description="soft band for the area penalty (fraction of target)")


class PenaltyTerm(BaseModel):
    weight: float
    raw: float = Field(description="normalised, non-negative penalty magnitude")
    contribution: float = Field(description="weight * raw, subtracted from the score")
    note: str = ""


class ScoreBreakdown(BaseModel):
    score: float = Field(description="aero_efficiency - total_penalty (feasible designs)")
    aero_efficiency: float = Field(description="reward term: L/D at trim")
    total_penalty: float
    penalties: Dict[str, PenaltyTerm]
    feasible: bool
    reject_reason: Optional[str] = None
    static_margin: float = Field(description="realised (x_np - x_cg)/MAC at the CG used")
    # S1-C2. Carried on the breakdown as well as inside the penalty note, because a
    # card that wants to say "stalls outboard" should not have to parse prose.
    stall_eta: float = Field(
        default=0.0,
        description="semi-span fraction where stall begins (0 root, 1 tip)")
    stall_is_outboard: bool = Field(
        default=False,
        description="stall onset outboard of spanwise.OUTBOARD_STALL_FRAC — pitch-up "
                    "departure risk on a tailless swept wing")

    @property
    def structural_active(self) -> bool:
        return any(self.penalties[t].weight > 0 for t in _STRUCTURAL_TERMS
                   if t in self.penalties)


_INFEASIBLE_SCORE = -1.0e6


def stall_station_at_stall_reynolds(params: BWBParams, aero: AsbAeroResult,
                                    max_iter: int = 6, tol: float = 1e-4):
    """The Schrenk stall solution, evaluated at the STALL Reynolds (S1-C2).

    The objective is handed a cruise-point `AsbAeroResult`, and a wing does not stall
    at cruise. Rather than plumb atmosphere and weight in here, use the fact that
    Reynolds is proportional to speed in the same air, and that lift equals weight at
    both points:

        L = q S CL  is the same at both  =>  V_stall / V_cruise = sqrt(CL_cruise / CLmax)
        Re_stall = Re_cruise * sqrt(CL_cruise / CLmax)

    CLmax depends on Reynolds, so this is the same fixed point `aero_estimates.
    stall_solution` runs, written in Reynolds ratio instead of absolute speed. It
    converges in 3-4 passes and needs nothing the objective does not already hold.

    NOTE the sweep patch is deliberately NOT applied: `cos(Lc/4)` scales the wing's
    total lift capability, not the station at which the first section runs out of it.
    """
    from .spanwise import solve_stall
    re = max(float(aero.reynolds_mac), 1.0)
    cl_cruise = max(float(aero.cl_target), 1e-6)
    sol = solve_stall(params, re)
    for _ in range(max_iter):
        re_new = max(float(aero.reynolds_mac) * math.sqrt(
            cl_cruise / max(sol.cl_max_wing, 1e-6)), 1.0)
        if abs(re_new - re) <= tol * max(re, 1.0):
            re = re_new
            break
        re = re_new
        sol = solve_stall(params, re)
    return sol


def _stall_station_penalty(stall_eta: float, outboard_frac: float) -> float:
    """Zero inboard of the flag, ramping linearly to 1 at the tip.

    A step at `outboard_frac` would give the optimiser a cliff with zero gradient on
    both sides — it would either ignore the term or sit exactly on the edge. A ramp
    means "further outboard is worse", which is what the physics says: the further out
    the first stall, the longer the moment arm of the lift that disappears."""
    span = max(1.0 - outboard_frac, 1e-6)
    return min(1.0, max(0.0, (stall_eta - outboard_frac) / span))


def _static_margin(aero: AsbAeroResult, config: ObjectiveConfig) -> float:
    """(x_np - x_cg)/MAC. aero already carries x_np and the CG/MAC it used."""
    if aero.mac_m <= 0:
        return 0.0
    return (aero.x_np_m - aero.x_cg_m) / aero.mac_m


def score(
    params: BWBParams,
    aero: AsbAeroResult,
    weights: ObjectiveWeights,
    config: ObjectiveConfig,
) -> ScoreBreakdown:
    """Grade one design. `params` is an already-validated BWBParams (its
    constructor is the geometry gate); `aero` is its AsbAeroResult from block A."""
    penalties: Dict[str, PenaltyTerm] = {}

    def add(name: str, weight: float, raw: float, note: str = "") -> None:
        raw = max(0.0, float(raw))
        penalties[name] = PenaltyTerm(
            weight=weight, raw=raw, contribution=weight * raw, note=note)

    sm = _static_margin(aero, config)

    # --- structural block (dormant): raw placeholders of 0 until block B ---
    add("m_root", weights.w1_m_root, 0.0, "dormant - needs beam surrogate (block B)")
    add("u_structure", weights.w2_u_structure, 0.0, "dormant - needs beam surrogate (block B)")
    add("sigma_peak", weights.w3_sigma_peak, 0.0, "dormant - needs beam surrogate (block B)")
    add("sigma_cv", weights.w4_sigma_cv, 0.0, "dormant - needs beam surrogate (block B)")
    add("shape_3d", weights.w5_shape_3d, 0.0, "dormant - needs beam surrogate (block B)")

    # --- lateral-directional stability (w6): reward margin above the floor ---
    # Penalise how far Cn_beta sits below a comfortable target band above the floor.
    latdir_target = config.cn_beta_floor + 0.02  # ~a small positive margin, 1/rad
    latdir_raw = max(0.0, latdir_target - aero.cn_beta_per_rad) / max(latdir_target, 1e-6)
    add("latdir", weights.w6_latdir, latdir_raw,
        f"Cn_beta={aero.cn_beta_per_rad:.4f}/rad vs floor {config.cn_beta_floor:.4f}")

    # --- pitch stability (w7): adhere to the target static margin ---
    pitch_raw = abs(sm - config.static_margin_target) / max(config.static_margin_target, 1e-3)
    add("pitch", weights.w7_pitch, pitch_raw,
        f"static_margin={sm:.3f} vs target {config.static_margin_target:.3f}")

    # --- trim (w8): control deflection to fly level ~ residual pitching moment ---
    # A design naturally near-trimmed at cruise CL needs little elevon => less drag.
    trim_raw = abs(aero.cm_at_trim)
    add("trim", weights.w8_trim, trim_raw, f"Cm_at_trim={aero.cm_at_trim:.4f}")

    # --- geometry constraints (w9): hold the reference area at target ---
    area = params.planform_area_m2()
    area_err = abs(area - config.s_ref_target_m2) / config.s_ref_target_m2
    geom_raw = max(0.0, area_err - config.area_tol_frac) + area_err  # soft band + gentle pull
    add("geometry", weights.w9_geometry, geom_raw,
        f"area={area:.4f} m^2 vs target {config.s_ref_target_m2:.4f} m^2")

    # --- stall station (w10): penalise stall beginning outboard (S1-C2) ---
    from .spanwise import OUTBOARD_STALL_FRAC
    stall = stall_station_at_stall_reynolds(params, aero)
    add("stall_station", weights.w10_stall_station,
        _stall_station_penalty(stall.stall_eta, OUTBOARD_STALL_FRAC),
        f"stall begins at eta={stall.stall_eta:.3f} "
        f"({'OUTBOARD' if stall.stall_is_outboard else 'inboard'} of "
        f"{OUTBOARD_STALL_FRAC:.2f}) — Schrenk, NACA TM-948")

    total_penalty = sum(p.contribution for p in penalties.values())
    aero_efficiency = float(aero.l_over_d)

    # --- hard feasibility floors: reject, don't merely penalise ---
    reject: Optional[str] = None
    if config.enforce_latdir_floor and aero.cn_beta_per_rad < config.cn_beta_floor:
        reject = (f"lateral-directional unstable: Cn_beta {aero.cn_beta_per_rad:.4f} "
                  f"< floor {config.cn_beta_floor:.4f} /rad")
    elif sm <= config.min_static_margin:
        reject = (f"pitch unstable: static margin {sm:.3f} <= "
                  f"{config.min_static_margin:.3f}")
    elif not math.isfinite(aero_efficiency) or aero_efficiency <= 0:
        reject = f"non-physical L/D: {aero_efficiency}"

    if reject is not None:
        return ScoreBreakdown(
            score=_INFEASIBLE_SCORE, aero_efficiency=aero_efficiency,
            total_penalty=total_penalty, penalties=penalties,
            feasible=False, reject_reason=reject, static_margin=sm,
            stall_eta=stall.stall_eta, stall_is_outboard=stall.stall_is_outboard)

    return ScoreBreakdown(
        score=aero_efficiency - total_penalty, aero_efficiency=aero_efficiency,
        total_penalty=total_penalty, penalties=penalties,
        feasible=True, reject_reason=None, static_margin=sm,
        stall_eta=stall.stall_eta, stall_is_outboard=stall.stall_is_outboard)


def config_from_derived(derived, cn_beta_floor: float = 0.0) -> ObjectiveConfig:
    """Build an ObjectiveConfig from a MissionDerived (keeps the MDO mission-aware
    without importing the heavy plan stage here)."""
    return ObjectiveConfig(
        required_cl=derived.required_cl,
        static_margin_target=derived.static_margin_target,
        s_ref_target_m2=derived.s_ref_target_m2,
        cn_beta_floor=cn_beta_floor,
    )
