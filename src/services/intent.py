"""Intent service — the democratization layer.

Turns a casual natural-language request into (a) a structured MissionSpec,
(b) mission-derived physics with every assumption logged (defaults.derive),
and (c) an expert-grade user_requirement string the existing planner can
consume unchanged.
"""
import os
import sys
from typing import Optional

from pydantic import BaseModel

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from domain_packs.bwb.defaults import derive  # noqa: E402
from domain_packs.bwb.schema import MissionDerived, MissionSpec  # noqa: E402


class IntentResult(BaseModel):
    mission_derived: MissionDerived
    enriched_requirement: str


_PARSE_SYSTEM = """You extract UAV mission requirements from a user's casual request.
Rules:
- Only set a field when the user stated it or it is clearly implied. Convert units to SI.
- Leave mtow_kg and endurance_min unset unless the user gave them (do NOT invent them).
- launch is one of hand/catapult/runway; default hand only if nothing is implied.
- If the user gives a range ("20-30 m/s"), use the midpoint.
- payload_kg, cruise_speed_ms, cruise_altitude_m are required: if truly absent, use a
  sensible small-UAV value (payload 1 kg, speed 20 m/s, altitude 100 m) — these will be
  shown to the user as assumptions for review."""


def parse_mission(user_prompt: str, llm_service, revision_note: Optional[str] = None) -> MissionSpec:
    prompt = f"User request:\n{user_prompt}"
    if revision_note:
        prompt += f"\n\nRevision from user review (takes precedence):\n{revision_note}"
    return llm_service.invoke(prompt, _PARSE_SYSTEM, pydantic_obj=MissionSpec)


def build_enriched_requirement(d: MissionDerived) -> str:
    """Expert-grade requirement string for the downstream Foam-Agent planner.

    Foundation OpenFOAM v10 conventions (simpleFoam, momentumTransport).
    """
    m = d.mission
    p = d.params
    return f"""External aerodynamics of a blended-wing-body UAV (steady cruise).

Geometry: watertight STL of the full aircraft (both halves), provided by the geometry
stage. Reference quantities: reference area {d.s_ref_target_m2:.4f} m^2, reference chord
(MAC) {p.mean_aero_chord_m():.4f} m, span {p.span_m:.3f} m. Moment reference at the
estimated CG.

Flow conditions: freestream velocity {m.cruise_speed_ms:.1f} m/s along +x at geometric
altitude {d.atmosphere.geometric_altitude_m:.2f} m AMSL. Standard-atmosphere
temperature {d.atmosphere.temperature_k:.3f} K, pressure {d.atmosphere.pressure_pa:.2f} Pa,
air density {d.atmosphere.density_kgm3:.6f} kg/m^3,
dynamic viscosity {d.atmosphere.dynamic_viscosity_kgms:.9g} kg/(m*s),
kinematic viscosity {d.atmosphere.kinematic_viscosity_m2s:.9g} m^2/s, Reynolds number based on MAC
approximately {d.reynolds_mac:.3g}. Atmosphere source {d.atmosphere.source}, dataset
SHA-256 {d.atmosphere.dataset_sha256}.

Simulation: incompressible steady-state RANS with simpleFoam (Foundation OpenFOAM v10),
turbulence model {d.turbulence_model} with wall functions (target y+ ~ {d.y_plus_target:.0f}).
External-aero domain: far-field box at least 10 chord lengths upstream/lateral and 20
downstream, velocity inlet, pressure outlet, slip on far-field sides, no-slip wall on the
aircraft surface.

Post-processing: forceCoeffs function object computing CL, CD and CM about the moment
reference using the reference quantities above; residuals and yPlus function objects.
Success criteria: converged residuals and force coefficients; cruise target CL approximately
{d.required_cl:.3f} (lift = weight = {d.weight_n:.1f} N at {m.cruise_speed_ms:.1f} m/s).
"""


def run_intent(user_prompt: str, llm_service, revision_note: Optional[str] = None) -> IntentResult:
    mission = parse_mission(user_prompt, llm_service, revision_note)
    derived = derive(mission)
    return IntentResult(
        mission_derived=derived,
        enriched_requirement=build_enriched_requirement(derived),
    )
