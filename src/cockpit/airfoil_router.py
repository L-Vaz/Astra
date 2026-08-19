"""Dedicated HTTP surface for the Stage 1 Airfoil Sets approval gate."""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Literal, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, StrictStr

from domain_packs.bwb import airfoil_coordinates, airfoils, intake as intake_pack
from domain_packs.bwb.aero_estimates import estimate as aero_estimate
from domain_packs.bwb.atmosphere import atmosphere_at
from domain_packs.bwb.handoff import SCHEMA_VERSION, build_handoff
from domain_packs.bwb.inverse import LockedValues, check_requirements, solve_requirements
from domain_packs.bwb.requirements import DesignRequirements
from domain_packs.bwb.schema import BWBParams, MissionDerived, MissionSpec
from services import airfoil_selection as service
from services import candidates, design_session as ds


router = APIRouter()


class RecommendRequest(BaseModel):
    session_id: StrictStr = Field(min_length=1, max_length=128)


class SelectRequest(BaseModel):
    session_id: StrictStr = Field(min_length=1, max_length=128)
    schedule_hash: StrictStr = Field(min_length=1, max_length=128)
    recommendation_version: Optional[int] = Field(default=None, ge=1)


class GenerateRequest(BaseModel):
    session_id: Optional[StrictStr] = None


def _http_error(exc: Exception, *, missing_is_conflict: bool = False) -> HTTPException:
    if isinstance(exc, FileNotFoundError):
        return HTTPException(409 if missing_is_conflict else 404, str(exc))
    if isinstance(exc, (service.AirfoilConflict, ds.SessionBusy)):
        return HTTPException(409, str(exc))
    if isinstance(exc, (service.AirfoilInvalid, ds.SessionIdRejected, ValueError)):
        return HTTPException(422, str(exc))
    return HTTPException(500, str(exc))


def _prepare_plan_from_intake(session_id: str) -> Dict[str, Any]:
    """Synchronize the reviewed intake before computing recommendations.

    The Airfoil Sets gate moved ahead of Concepts, so the legacy intake-to-plan bridge
    now runs when recommendations are requested.  We reuse the shell's hardened store
    helpers (recovery, disclosures, idempotent versions) without modifying its DOM or
    route implementation.
    """
    from cockpit import server as shell  # lazy: server imports this router

    intake, intake_version, warnings = shell._load_intake(session_id)
    if intake_version == 0:
        intake_version = shell._save_intake(session_id, intake)
    if intake.feasibility and intake.feasibility.verdict == "error":
        raise service.AirfoilConflict(
            "requirements do not size; fix the intake before Airfoil Sets"
        )
    warnings = list(warnings) + shell._intake_warnings(intake)

    def row_number(row_id: str, fallback: float) -> float:
        value = intake_pack.usable_number(intake, row_id)
        return fallback if value is None else value

    launch_row = next((row for row in intake.rows if row.id == "launch"), None)
    launch = launch_row.value.strip().lower() if launch_row else "hand"
    if launch not in ("hand", "catapult", "runway"):
        launch = "hand"
    payload_kg = min(max(row_number("payload", 1.0), 0.01), 10.0)
    mtow_kg = min(row_number("mtow_cap", 0.0), 25.0) or None
    if mtow_kg is not None and mtow_kg < payload_kg:
        warnings.append(shell._warning(
            "clamped", row_id="mtow_cap", typed=f"{mtow_kg:g} kg",
            used=f"{payload_kg:g} kg", limit=f"payload {payload_kg:g} kg",
            message=(f"MTOW cap: {mtow_kg:g} kg is below the {payload_kg:g} kg "
                     f"payload; raised the cap to {payload_kg:g} kg"),
        ))
        mtow_kg = payload_kg

    mission = MissionSpec(
        payload_kg=payload_kg,
        cruise_speed_ms=min(max(row_number("cruise_speed", 18.0), 8.0), 60.0),
        cruise_altitude_m=min(max(row_number("altitude", 100.0), 0.0), 4000.0),
        mtow_kg=mtow_kg,
        launch=launch,
    )
    requirements = intake_pack.to_requirements(intake).model_copy(
        update={"cruise_speed_ms": mission.cruise_speed_ms}
    )
    solved = solve_requirements(requirements)
    atmosphere = atmosphere_at(mission.cruise_altitude_m)
    derived = MissionDerived(
        mission=mission,
        atmosphere=atmosphere,
        mtow_kg=requirements.mtow_kg,
        air_density_kgm3=atmosphere.density_kgm3,
        weight_n=requirements.mtow_kg * 9.81,
        wing_loading_kgm2=solved.panel.wing_loading_kgm2,
        s_ref_target_m2=solved.panel.s_ref_m2,
        required_cl=solved.panel.cl_cruise,
        reynolds_mac=(mission.cruise_speed_ms * solved.params.mean_aero_chord_m()
                      / atmosphere.kinematic_viscosity_m2s),
        params=solved.params,
        assumptions=(["Sized by the stage-1 intake (inverse solve at best-L/D "
                      "cruise)"] + solved.notes),
    )
    plan_version = shell._write_plan_version(
        session_id,
        derived,
        f"stage-1 intake v{intake_version} locked",
        invalidate_from="fast_aero",
    )
    synced, overrides = shell._synced_requirements(
        session_id, intake, mission, requirements
    )
    for override in overrides:
        warnings.append(shell._warning(
            "overridden",
            row_id=shell._REQUIREMENT_SOURCE_ROWS[override["field"]],
            typed=str(override["new"]),
            used=str(override["new"]),
            limit=str(override["previous"]),
            message=(f"{override['field']}: replaced your saved "
                     f"{override['previous']} with {override['new']} from the "
                     "Stage-1 intake"),
        ))
    requirements_version = ds.save_requirements(
        session_id, dict(synced, intake_overrides=overrides)
    )
    return {
        "plan_version": plan_version,
        "intake_version": intake_version,
        "requirements_version": requirements_version,
        "warnings": warnings,
    }


@router.post("/api/stage1/airfoils/recommend")
def recommend_airfoils(req: RecommendRequest) -> Dict[str, Any]:
    try:
        ds.get_state(req.session_id)
        with ds.session_lock(req.session_id):
            preparation = _prepare_plan_from_intake(req.session_id)
            return service.recommend(req.session_id, preparation=preparation)
    except Exception as exc:
        raise _http_error(exc) from exc


@router.get("/api/stage1/airfoils")
def list_airfoils(session: str = Query(min_length=1, max_length=128)) -> Dict[str, Any]:
    try:
        payload = service.get_recommendations(session)
        try:
            payload["selection"] = service.get_selection(
                session, require_current=False
            )
        except FileNotFoundError:
            payload["selection"] = None
        return payload
    except Exception as exc:
        raise _http_error(exc) from exc


@router.get("/api/stage1/airfoils/detail")
def airfoil_detail(
    session: str = Query(min_length=1, max_length=128),
    strategy: Literal["balanced", "efficiency", "low_speed_control"] = "balanced",
    station: Literal["root", "mid", "tip"] = "root",
) -> Dict[str, Any]:
    try:
        recommendation = service.get_recommendations(session)
        card = next((item for item in recommendation["strategies"]
                     if item["strategy"] == strategy), None)
        if card is None:
            raise service.AirfoilInvalid(f"unknown strategy {strategy!r}")
        name = card["schedule"][station]
        state = recommendation["station_state"][station]
        reynolds = float(state["reynolds_cruise"])
        aero = airfoils.section_at(name, reynolds)
        coordinates = airfoil_coordinates.section(name)
        ladder = []
        for cl in airfoils.cl_ladder():
            cd, flags = airfoils.section_cd(name, cl, reynolds)
            ladder.append({"cl": cl, "cd": cd, "valid": not bool(flags)})
        references = airfoil_coordinates.reference(name)
        return {
            "session_id": session,
            "recommendation_version": recommendation["version"],
            "strategy": strategy,
            "station": station,
            "name": name,
            "coordinates": coordinates["coordinates"],
            "coordinate_sha256": coordinates["sha256"],
            "prestall_lift": [
                [0.0, aero.cl0],
                [aero.alpha_clmax_deg, aero.clmax],
            ],
            "clmax": {
                "value": aero.clmax,
                "alpha_deg": aero.alpha_clmax_deg,
                "is_measured_peak": aero.stalled,
            },
            "cd_vs_cl": ladder,
            "reynolds": reynolds,
            "reynolds_low_speed": state["reynolds_low_speed"],
            "thickness_ratio": aero.tc,
            "cm0": aero.cm0,
            "confidence": aero.confidence,
            "validity": {
                "polar_in_envelope": not bool(aero.out_of_range),
                "polar_floor_bound": state["polar_floor_bound"],
                "stalled_inside_sweep": aero.stalled,
            },
            "evidence": {
                "polar": "NeuralFoil table [C]",
                "coordinates": "AeroSandbox library archive [C]",
                "experimental": (
                    "primary reference [A]" if references.get("experimental")
                    else "not located; evidence weakened"
                ),
            },
            "references": references,
        }
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/api/stage1/airfoils/select")
def select_airfoil(req: SelectRequest) -> Dict[str, Any]:
    try:
        return service.select(
            req.session_id,
            req.schedule_hash,
            recommendation_version=req.recommendation_version,
        )
    except Exception as exc:
        raise _http_error(exc, missing_is_conflict=True) from exc


@router.post("/api/stage1/generate")
def generate_concepts(req: GenerateRequest) -> Dict[str, Any]:
    """Concept generation is impossible without a current exact approval."""
    if not req.session_id:
        raise HTTPException(400, "session_id is required for this request")
    if not re.fullmatch(r"[0-9a-f]{8}", req.session_id):
        raise HTTPException(400, "session_id must be 8 lowercase hex characters")
    try:
        ds.get_state(req.session_id)
    except ds.SessionIdRejected as exc:
        raise HTTPException(400, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, f"unknown design session: {req.session_id}") from exc
    try:
        with ds.session_lock(req.session_id):
            approval = service.get_selection(req.session_id, require_current=True)
            batch = candidates.generate(
                req.session_id, approval=approval, regenerate=True
            )
        return {
            "session_id": req.session_id,
            "plan_version": approval["sources"]["plan_version"],
            "intake_version": approval["sources"]["intake_version"],
            "selection_version": approval["version"],
            "selection_hash": approval["selection_hash"],
            "candidates": batch["candidates"],
            "stale": batch.get("stale", False),
            "warnings": list(
                (approval.get("preparation") or {}).get("warnings") or []
            ),
        }
    except Exception as exc:
        raise _http_error(exc, missing_is_conflict=True) from exc


def _handoff_inputs(session_id: str) -> tuple[BWBParams, DesignRequirements]:
    plan_dir = ds.latest_dir(session_id, "plan")
    if not plan_dir:
        raise FileNotFoundError("no plan in this session")
    with open(os.path.join(plan_dir, "mission_derived.json")) as stream:
        derived = MissionDerived.model_validate_json(stream.read())

    params = derived.params
    geometry_dir = ds.latest_dir(session_id, "geometry")
    geometry_params = (
        os.path.join(geometry_dir, "params.json") if geometry_dir else None
    )
    if geometry_params and os.path.isfile(geometry_params):
        with open(geometry_params) as stream:
            params = BWBParams.model_validate(json.load(stream))

    saved = ds.latest_requirements(session_id)
    if saved:
        payload = {
            key: value for key, value in saved.items()
            if key in DesignRequirements.model_fields
        }
        requirements = DesignRequirements.model_validate(payload)
    else:
        requirements = DesignRequirements(
            mtow_kg=derived.mtow_kg,
            cruise_speed_ms=derived.mission.cruise_speed_ms,
            cruise_altitude_m=derived.mission.cruise_altitude_m,
            launch=derived.mission.launch,
        )
    return params, requirements


@router.get("/api/handoff/{session_id}")
def stage2_handoff(session_id: str) -> Dict[str, Any]:
    """Return a Stage 2 handoff only when its airfoil approval is current."""
    try:
        ds.get_state(session_id)
    except (FileNotFoundError, ds.SessionIdRejected) as exc:
        raise _http_error(exc) from exc
    try:
        approval = service.get_selection(session_id, require_current=True)
        params, requirements = _handoff_inputs(session_id)
        atmosphere = atmosphere_at(requirements.cruise_altitude_m)
        panel = aero_estimate(
            params,
            atmosphere,
            requirements.cruise_speed_ms,
            requirements.mtow_kg * 9.81,
            requirements.launch,
        )
        lock_path = os.path.join(ds.session_dir(session_id), "cockpit", "locks.json")
        locks = {}
        if os.path.isfile(lock_path):
            with open(lock_path) as stream:
                locks = json.load(stream)
        locked = LockedValues(
            span_m=params.span_m if locks.get("span_max") else None,
            s_ref_m2=params.planform_area_m2() if locks.get("s_ref") else None,
            mtow_kg=requirements.mtow_kg if locks.get("mtow_cap") else None,
        )
        handoff = build_handoff(
            params,
            panel,
            requirements,
            requirement_status=check_requirements(requirements, params),
            locked=locked,
            altitude_m=requirements.cruise_altitude_m,
            airfoil_selection=approval,
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "handoff": json.loads(handoff.model_dump_json()),
        }
    except Exception as exc:
        raise _http_error(exc, missing_is_conflict=True) from exc
