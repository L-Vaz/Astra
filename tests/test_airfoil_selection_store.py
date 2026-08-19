"""Immutable recommendation/approval storage and staleness contract."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os

import pytest

from domain_packs.bwb.defaults import derive
from domain_packs.bwb.schema import MissionSpec


@pytest.fixture()
def modules():
    try:
        from services import airfoil_selection, design_session
    except ImportError as exc:
        pytest.fail(f"airfoil selection service is missing: {exc}")
    return airfoil_selection, design_session


@pytest.fixture()
def planned_session(sessions_root, modules):
    _service, ds = modules
    sid = ds.create_session("airfoil-store")
    mission = MissionSpec(
        payload_kg=1.0,
        mtow_kg=3.5,
        cruise_speed_ms=18.0,
        cruise_altitude_m=0.0,
        launch="hand",
    )
    derived = derive(mission)
    with ds.session_lock(sid):
        vdir = ds.new_version_dir(sid, "plan")
        ds.atomic_write(os.path.join(vdir, "mission_derived.json"),
                        derived.model_dump_json(indent=2))
        ds.record_version(sid, "plan", {"source": "store-test"})
    ds.save_requirements(sid, {
        "payload_kg": 1.0,
        "mtow_kg": 3.5,
        "cruise_speed_ms": 18.0,
        "cruise_altitude_m": 0.0,
        "launch": "hand",
    })
    intake_dir = os.path.join(ds.session_dir(sid), "intake", "v1")
    os.makedirs(intake_dir, exist_ok=True)
    ds.atomic_write_json(os.path.join(intake_dir, "intake.json"), {
        "version": 1,
        "brief": "hand launch camera inspection",
        "rows": [],
    })
    ds.atomic_write_json(os.path.join(ds.session_dir(sid), "intake", "index.json"),
                         {"latest": 1})
    return sid


def test_recommendation_is_immutable_versioned_and_reloadable(planned_session, modules):
    service, ds = modules
    saved = service.recommend(planned_session)

    assert saved["version"] == 1
    assert saved["stale"] is False
    assert saved["sources"]["plan_version"] == 1
    assert saved["sources"]["requirements_version"] == 1
    assert saved["sources"]["intake_version"] == 1
    assert saved["recommendation_hash"]
    path = os.path.join(ds.session_dir(planned_session),
                        "airfoil_recommendations", "v1", "recommendations.json")
    assert os.path.isfile(path)
    assert service.get_recommendations(planned_session) == saved


def test_recommendation_retry_is_idempotent_for_unchanged_sources(planned_session, modules):
    service, _ds = modules
    first = service.recommend(planned_session)
    second = service.recommend(planned_session)

    assert second["version"] == first["version"]
    assert second["recommendation_hash"] == first["recommendation_hash"]


def test_approval_is_idempotent_but_a_different_schedule_creates_v2(planned_session,
                                                                    modules):
    service, ds = modules
    recommendation = service.recommend(planned_session)
    first_hash = recommendation["strategies"][0]["schedule_hash"]
    second_hash = recommendation["strategies"][1]["schedule_hash"]

    first = service.select(planned_session, first_hash)
    retry = service.select(planned_session, first_hash)
    changed = service.select(planned_session, second_hash)

    assert retry == first
    assert first["version"] == 1
    assert changed["version"] == 2
    assert changed["selection_hash"] != first["selection_hash"]
    assert os.path.isfile(os.path.join(ds.session_dir(planned_session),
                                       "airfoil_selections", "v1", "selection.json"))
    assert os.path.isfile(os.path.join(ds.session_dir(planned_session),
                                       "airfoil_selections", "v2", "selection.json"))


def test_parallel_repeated_approval_allocates_one_version(planned_session, modules):
    service, _ds = modules
    recommendation = service.recommend(planned_session)
    schedule_hash = recommendation["strategies"][0]["schedule_hash"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        approvals = list(pool.map(
            lambda _index: service.select(planned_session, schedule_hash), range(16)
        ))

    assert {item["version"] for item in approvals} == {1}
    assert len({item["selection_hash"] for item in approvals}) == 1


@pytest.mark.parametrize("source", ["requirements", "intake", "plan"])
def test_upstream_change_stales_recommendation_and_selection(planned_session, modules,
                                                            source):
    service, ds = modules
    recommendation = service.recommend(planned_session)
    service.select(planned_session, recommendation["strategies"][0]["schedule_hash"])

    if source == "requirements":
        current = ds.latest_requirements(planned_session)
        payload = {key: value for key, value in current.items()
                   if key not in ("created", "version")}
        ds.save_requirements(planned_session, {**payload, "span_max_m": 2.0})
    elif source == "intake":
        root = os.path.join(ds.session_dir(planned_session), "intake")
        os.makedirs(os.path.join(root, "v2"), exist_ok=True)
        ds.atomic_write_json(os.path.join(root, "v2", "intake.json"), {
            "version": 2, "brief": "changed", "rows": []})
        ds.atomic_write_json(os.path.join(root, "index.json"), {"latest": 2})
    else:
        old = ds.latest_dir(planned_session, "plan")
        with open(os.path.join(old, "mission_derived.json")) as stream:
            payload = stream.read()
        with ds.session_lock(planned_session):
            vdir = ds.new_version_dir(planned_session, "plan")
            ds.atomic_write(os.path.join(vdir, "mission_derived.json"), payload)
            ds.record_version(planned_session, "plan", {"source": "external-plan"})

    assert service.get_recommendations(planned_session)["stale"] is True
    with pytest.raises(service.AirfoilConflict, match="stale"):
        service.get_selection(planned_session, require_current=True)
    assert service.get_selection(planned_session, require_current=False)["stale"] is True


def test_unknown_session_and_missing_artifacts_are_not_found(sessions_root, modules):
    service, ds = modules
    with pytest.raises(FileNotFoundError, match="unknown"):
        service.recommend("does-not-exist")

    sid = ds.create_session("empty")
    with pytest.raises(FileNotFoundError, match="recommendation"):
        service.get_recommendations(sid)
    with pytest.raises(FileNotFoundError, match="selection"):
        service.get_selection(sid)


def test_store_rejects_session_path_traversal(sessions_root, modules):
    service, ds = modules
    with pytest.raises(ds.SessionIdRejected):
        service.get_recommendations("../escaped")


def test_selection_records_exact_names_hashes_and_ranking_provenance(planned_session,
                                                                     modules):
    service, _ds = modules
    recommendation = service.recommend(planned_session)
    chosen = recommendation["strategies"][0]
    selection = service.select(planned_session, chosen["schedule_hash"])

    assert selection["schedule"] == chosen["schedule"]
    assert selection["coordinate_hashes"] == chosen["coordinate_hashes"]
    assert selection["recommendation_version"] == recommendation["version"]
    assert selection["recommendation_hash"] == recommendation["recommendation_hash"]
    assert selection["ranking_provenance"]["method"]
    assert service.get_selection(planned_session)["selection_hash"] == selection[
        "selection_hash"]

