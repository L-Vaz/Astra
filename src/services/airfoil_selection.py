"""Immutable service boundary for Airfoil Sets recommendations and approvals."""
from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any, Dict, Optional

from domain_packs.bwb.airfoil_selection import recommend_airfoil_sets
from domain_packs.bwb.atmosphere import atmosphere_provenance
from domain_packs.bwb.schema import MissionDerived

from . import design_session as ds
from .intent import build_enriched_requirement


class AirfoilConflict(RuntimeError):
    """A prerequisite is missing or the requested artifact is stale (HTTP 409)."""


class AirfoilInvalid(ValueError):
    """The request names no valid recommendation or schedule (HTTP 422)."""


def _json_hash(payload: Any) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True,
                     ensure_ascii=True).encode("ascii")
    return hashlib.sha256(raw).hexdigest()


def _file_hash(path: str) -> Optional[str]:
    if not os.path.isfile(path):
        return None
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _index(root: str) -> int:
    path = os.path.join(root, "index.json")
    if not os.path.isfile(path):
        return 0
    with open(path) as stream:
        return int(json.load(stream).get("latest", 0))


def _recommendation_root(session_id: str) -> str:
    return os.path.join(ds.session_dir(session_id), "airfoil_recommendations")


def _selection_root(session_id: str) -> str:
    return os.path.join(ds.session_dir(session_id), "airfoil_selections")


def _intake_source(session_id: str) -> tuple[int, Optional[str], Optional[dict]]:
    root = os.path.join(ds.session_dir(session_id), "intake")
    version = _index(root)
    if version == 0:
        return 0, None, None
    path = os.path.join(root, f"v{version}", "intake.json")
    if not os.path.isfile(path):
        return version, None, None
    with open(path) as stream:
        return version, _file_hash(path), json.load(stream)


def _requirements_source(session_id: str) -> tuple[int, Optional[str]]:
    version = ds._latest_requirements_version(session_id)
    if version == 0:
        return 0, None
    path = os.path.join(ds.session_dir(session_id), "requirements", f"v{version}",
                        "requirements.json")
    return version, _file_hash(path)


def _plan_source(session_id: str) -> tuple[int, str, str]:
    state = ds.get_state(session_id)
    info = state["stages"]["plan"]
    if not info.get("valid") or not info.get("latest"):
        raise AirfoilConflict("session has no current plan")
    version = int(info["latest"])
    path = os.path.join(ds.session_dir(session_id), "plan", f"v{version}",
                        "mission_derived.json")
    digest = _file_hash(path)
    if digest is None:
        raise AirfoilConflict("current plan has no mission_derived.json")
    return version, digest, path


def current_sources(session_id: str) -> Dict[str, Any]:
    """Current identities used for read-time staleness checks."""
    plan_version, plan_hash, _path = _plan_source(session_id)
    requirements_version, requirements_hash = _requirements_source(session_id)
    intake_version, intake_hash, _payload = _intake_source(session_id)
    # Table/archive hashes live in the domain recommendation provenance. Importing
    # here avoids duplicating file locations in the store.
    from domain_packs.bwb import airfoil_coordinates, airfoils
    return {
        "plan_version": plan_version,
        "plan_hash": plan_hash,
        "requirements_version": requirements_version,
        "requirements_hash": requirements_hash,
        "intake_version": intake_version,
        "intake_hash": intake_hash,
        "polar_table_hash": airfoils.table_sha256(),
        "coordinate_archive_hash": airfoil_coordinates.archive_sha256(),
    }


def _read_latest(root: str, filename: str, label: str) -> Dict[str, Any]:
    version = _index(root)
    if version == 0:
        raise FileNotFoundError(f"no airfoil {label} exists")
    path = os.path.join(root, f"v{version}", filename)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"airfoil {label} v{version} is missing")
    with open(path) as stream:
        return json.load(stream)


def _is_stale(payload: Dict[str, Any], session_id: str) -> bool:
    try:
        return payload.get("sources") != current_sources(session_id)
    except (FileNotFoundError, AirfoilConflict):
        return True


def _selection_bridges_created_plan(recommendation: Dict[str, Any],
                                    session_id: str) -> bool:
    """A selection-created plan is an output of this recommendation, not a new input.

    Without this explicit provenance bridge, selecting a set advances the plan and
    immediately makes the cards that produced it stale.  Any later plan write still
    breaks the bridge because its version/hash no longer match ``plan_*_created``.
    """
    try:
        selection = _read_latest(
            _selection_root(session_id), "selection.json", "selection"
        )
        current = current_sources(session_id)
    except (FileNotFoundError, AirfoilConflict):
        return False
    return (
        selection.get("recommendation_version") == recommendation.get("version")
        and selection.get("recommendation_hash")
        == recommendation.get("recommendation_hash")
        and selection.get("source_plan_version")
        == recommendation.get("sources", {}).get("plan_version")
        and selection.get("source_plan_hash")
        == recommendation.get("sources", {}).get("plan_hash")
        and selection.get("plan_version_created") == current.get("plan_version")
        and selection.get("plan_hash_created") == current.get("plan_hash")
        and selection.get("sources") == current
    )


def get_recommendations(session_id: str) -> Dict[str, Any]:
    payload = _read_latest(
        _recommendation_root(session_id), "recommendations.json", "recommendation"
    )
    stale = _is_stale(payload, session_id)
    if stale and _selection_bridges_created_plan(payload, session_id):
        stale = False
    payload["stale"] = stale
    return payload


def _write_selected_plan(session_id: str, recommendation: Dict[str, Any],
                         chosen: Dict[str, Any]) -> tuple[int, str]:
    """Persist the converged schedule geometry as the next immutable plan version."""
    source_version = int(recommendation["sources"]["plan_version"])
    source_path = os.path.join(
        ds.session_dir(session_id), "plan", f"v{source_version}",
        "mission_derived.json",
    )
    if _file_hash(source_path) != recommendation["sources"]["plan_hash"]:
        raise AirfoilConflict("the recommendation source plan no longer matches disk")
    with open(source_path) as stream:
        source = MissionDerived.model_validate_json(stream.read())
    params = source.params.model_validate(chosen["params"])
    area = params.planform_area_m2()
    speed = source.mission.cruise_speed_ms
    density = source.atmosphere.density_kgm3
    derived = source.model_copy(update={
        "params": params,
        "s_ref_target_m2": area,
        "wing_loading_kgm2": source.mtow_kg / area,
        "required_cl": source.weight_n / (0.5 * density * speed ** 2 * area),
        "reynolds_mac": (
            speed * params.mean_aero_chord_m()
            / source.atmosphere.kinematic_viscosity_m2s
        ),
        "assumptions": list(source.assumptions) + [
            "Root/mid/tip airfoil schedule approved in the Stage 1 Airfoil Sets gate"
        ],
    })
    vdir = ds.new_version_dir(session_id, "plan")
    plan_path = os.path.join(vdir, "mission_derived.json")
    ds.atomic_write(plan_path, derived.model_dump_json(indent=2))
    ds.atomic_write(
        os.path.join(vdir, "enriched_requirement.txt"),
        build_enriched_requirement(derived),
    )
    version = ds.record_version(session_id, "plan", {
        "source": "airfoil_selection",
        "source_plan_version": source_version,
        "recommendation_version": recommendation["version"],
        "recommendation_hash": recommendation["recommendation_hash"],
        "schedule_hash": chosen["schedule_hash"],
        **atmosphere_provenance(derived.atmosphere),
    })
    digest = _file_hash(plan_path)
    if digest is None:
        raise AirfoilConflict("selected airfoil plan was not written")
    return version, digest


def recommend(session_id: str, *, brief: Optional[str] = None,
              candidate_limit: int = 6, max_passes: int = 5,
              preparation: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Compute or return the immutable recommendation for current upstream state."""
    ds.get_state(session_id)  # distinguish unknown session before checking artifacts
    with ds.session_lock(session_id):
        sources = current_sources(session_id)
        try:
            existing = get_recommendations(session_id)
        except FileNotFoundError:
            existing = None
        if (existing and not existing["stale"]
                and existing.get("search", {}).get("candidate_limit_per_station")
                == candidate_limit
                and existing.get("search", {}).get("max_coupled_passes") == max_passes):
            return existing

        _plan_version, _plan_hash, plan_path = _plan_source(session_id)
        with open(plan_path) as stream:
            derived = MissionDerived.model_validate_json(stream.read())
        _intake_version, _intake_hash, intake = _intake_source(session_id)
        resolved_brief = brief if brief is not None else (
            str((intake or {}).get("brief") or "")
        )
        try:
            domain = recommend_airfoil_sets(
                params=derived.params,
                cruise_speed_ms=derived.mission.cruise_speed_ms,
                altitude_m=derived.mission.cruise_altitude_m,
                weight_n=derived.weight_n,
                launch=derived.mission.launch,
                brief=resolved_brief,
                candidate_limit=candidate_limit,
                max_passes=max_passes,
            )
        except ValueError as exc:
            raise AirfoilConflict(
                f"no feasible three-set airfoil recommendation: {exc}"
            ) from exc
        root = _recommendation_root(session_id)
        version = _index(root) + 1
        content = {
            **domain,
            "version": version,
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "sources": sources,
            "preparation": dict(preparation or {}),
        }
        content["recommendation_hash"] = _json_hash(content)
        content["stale"] = False
        vdir = os.path.join(root, f"v{version}")
        ds.atomic_write_json(os.path.join(vdir, "recommendations.json"), content)
        ds.atomic_write_json(os.path.join(root, "index.json"), {"latest": version})
        return content


def get_selection(session_id: str, *, require_current: bool = True) -> Dict[str, Any]:
    payload = _read_latest(_selection_root(session_id), "selection.json", "selection")
    stale = _is_stale(payload, session_id)
    if not stale:
        try:
            recommendation = get_recommendations(session_id)
            stale = (
                recommendation["stale"]
                or payload.get("recommendation_version") != recommendation.get("version")
                or payload.get("recommendation_hash")
                != recommendation.get("recommendation_hash")
            )
        except FileNotFoundError:
            stale = True
    payload["stale"] = stale
    if stale and require_current:
        raise AirfoilConflict("airfoil selection is stale; recompute and approve again")
    return payload


def select(session_id: str, schedule_hash: str,
           *, recommendation_version: Optional[int] = None) -> Dict[str, Any]:
    """Approve exactly one recommended schedule; identical retries are idempotent."""
    ds.get_state(session_id)
    with ds.session_lock(session_id):
        recommendation = get_recommendations(session_id)
        if recommendation["stale"]:
            raise AirfoilConflict("airfoil recommendation is stale; recompute it")
        if (recommendation_version is not None
                and recommendation_version != recommendation["version"]):
            raise AirfoilConflict(
                f"recommendation v{recommendation_version} is not current "
                f"(current is v{recommendation['version']})"
            )
        chosen = next((item for item in recommendation["strategies"]
                       if item["schedule_hash"] == schedule_hash), None)
        if chosen is None:
            raise AirfoilInvalid(
                f"schedule {schedule_hash!r} is not in recommendation "
                f"v{recommendation['version']}"
            )

        try:
            current = get_selection(session_id, require_current=False)
        except FileNotFoundError:
            current = None
        if (current and not current["stale"]
                and current.get("recommendation_hash")
                == recommendation["recommendation_hash"]
                and current.get("schedule_hash") == schedule_hash):
            return current

        source_plan_version = recommendation["sources"]["plan_version"]
        source_plan_hash = recommendation["sources"]["plan_hash"]
        plan_version_created, plan_hash_created = _write_selected_plan(
            session_id, recommendation, chosen
        )
        selection_sources = current_sources(session_id)

        root = _selection_root(session_id)
        version = _index(root) + 1
        content = {
            "schema_version": 1,
            "version": version,
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "sources": selection_sources,
            "source_plan_version": source_plan_version,
            "source_plan_hash": source_plan_hash,
            "plan_version_created": plan_version_created,
            "plan_hash_created": plan_hash_created,
            "recommendation_version": recommendation["version"],
            "recommendation_hash": recommendation["recommendation_hash"],
            "strategy": chosen["strategy"],
            "schedule": dict(chosen["schedule"]),
            "schedule_hash": chosen["schedule_hash"],
            "coordinate_hashes": dict(chosen["coordinate_hashes"]),
            "ranking_provenance": {
                **dict(chosen["ranking"]),
                "leave_one_metric_out": dict(chosen["leave_one_metric_out"]),
            },
            "metrics": dict(chosen["metrics"]),
            "selected_params": dict(chosen["params"]),
            "considered_schedules": list(recommendation["search"]["considered"]),
            "rejected_schedules": list(recommendation["search"]["rejected"]),
            "bound_sensitivity": dict(recommendation["bound_sensitivity"]),
            "preparation": dict(recommendation.get("preparation") or {}),
        }
        content["selection_hash"] = _json_hash(content)
        content["stale"] = False
        vdir = os.path.join(root, f"v{version}")
        ds.atomic_write_json(os.path.join(vdir, "selection.json"), content)
        ds.atomic_write_json(os.path.join(root, "index.json"), {"latest": version})
        return content
