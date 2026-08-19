"""Dedicated HTTP surface for the Stage 1 Airfoil Sets approval gate."""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Literal, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, StrictStr

from domain_packs.bwb import airfoil_coordinates, airfoils
from domain_packs.bwb.aero_estimates import estimate as aero_estimate
from domain_packs.bwb.atmosphere import atmosphere_at
from domain_packs.bwb.handoff import SCHEMA_VERSION, build_handoff
from domain_packs.bwb.inverse import LockedValues, check_requirements
from domain_packs.bwb.requirements import DesignRequirements
from domain_packs.bwb.schema import BWBParams, MissionDerived
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
    session_id: StrictStr = Field(min_length=1, max_length=128)


def _http_error(exc: Exception, *, missing_is_conflict: bool = False) -> HTTPException:
    if isinstance(exc, FileNotFoundError):
        return HTTPException(409 if missing_is_conflict else 404, str(exc))
    if isinstance(exc, (service.AirfoilConflict, ds.SessionBusy)):
        return HTTPException(409, str(exc))
    if isinstance(exc, (service.AirfoilInvalid, ds.SessionIdRejected, ValueError)):
        return HTTPException(422, str(exc))
    return HTTPException(500, str(exc))


@router.post("/api/stage1/airfoils/recommend")
def recommend_airfoils(req: RecommendRequest) -> Dict[str, Any]:
    try:
        return service.recommend(req.session_id)
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
    try:
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
