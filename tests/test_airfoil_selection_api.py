"""Dedicated Airfoil Sets router, status semantics, and Concepts gate."""
from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from domain_packs.bwb.defaults import derive
from domain_packs.bwb.schema import MissionSpec


@pytest.fixture()
def api(sessions_root, monkeypatch):
    try:
        from cockpit.airfoil_router import router
        from services import candidates, design_session as ds
    except ImportError as exc:
        pytest.fail(f"airfoil API router is missing: {exc}")

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    sid = ds.create_session("airfoil-api")
    derived = derive(MissionSpec(
        payload_kg=1.0, mtow_kg=3.5, cruise_speed_ms=18.0,
        cruise_altitude_m=0.0, launch="hand"))
    with ds.session_lock(sid):
        vdir = ds.new_version_dir(sid, "plan")
        ds.atomic_write(os.path.join(vdir, "mission_derived.json"),
                        derived.model_dump_json(indent=2))
        ds.record_version(sid, "plan", {"source": "api-test"})
    ds.save_requirements(sid, {
        "payload_kg": 1.0, "mtow_kg": 3.5, "cruise_speed_ms": 18.0,
        "cruise_altitude_m": 0.0, "launch": "hand",
    })
    return client, sid, candidates, ds


def test_four_endpoints_recommend_list_detail_and_select(api):
    client, sid, _candidates, _ds = api
    made = client.post("/api/stage1/airfoils/recommend", json={"session_id": sid})
    assert made.status_code == 200, made.text
    body = made.json()
    assert len(body["strategies"]) == 3

    listed = client.get(f"/api/stage1/airfoils?session={sid}")
    assert listed.status_code == 200
    assert listed.json()["recommendation_hash"] == body["recommendation_hash"]

    strategy = body["strategies"][0]
    detail = client.get(
        f"/api/stage1/airfoils/detail?session={sid}"
        f"&strategy={strategy['strategy']}&station=root"
    )
    assert detail.status_code == 200, detail.text
    section = detail.json()
    assert section["name"] == strategy["schedule"]["root"]
    assert section["coordinates"]
    assert section["coordinate_sha256"] == strategy["coordinate_hashes"]["root"]
    assert section["prestall_lift"][0][0] == 0.0
    assert section["clmax"]["value"] > section["prestall_lift"][0][1]
    assert len(section["cd_vs_cl"]) >= 2
    assert {"reynolds", "thickness_ratio", "cm0", "confidence", "validity",
            "evidence", "references"} <= set(section)

    selected = client.post("/api/stage1/airfoils/select", json={
        "session_id": sid,
        "schedule_hash": strategy["schedule_hash"],
        "recommendation_version": body["version"],
    })
    assert selected.status_code == 200, selected.text
    assert selected.json()["schedule"] == strategy["schedule"]

    restored = client.get(f"/api/stage1/airfoils?session={sid}")
    assert restored.status_code == 200
    assert restored.json()["selection"]["selection_hash"] == (
        selected.json()["selection_hash"]
    )


def test_status_semantics_are_404_409_and_422(api):
    client, sid, _candidates, ds = api
    assert client.get("/api/stage1/airfoils?session=missing").status_code == 404
    assert client.get(f"/api/stage1/airfoils?session={sid}").status_code == 404
    assert client.post("/api/stage1/airfoils/select", json={
        "session_id": sid, "schedule_hash": "not-a-schedule"}).status_code == 409

    rec = client.post("/api/stage1/airfoils/recommend", json={"session_id": sid}).json()
    invalid = client.post("/api/stage1/airfoils/select", json={
        "session_id": sid, "schedule_hash": "not-a-schedule"})
    assert invalid.status_code == 422
    bad_detail = client.get(
        f"/api/stage1/airfoils/detail?session={sid}&strategy=balanced&station=tail"
    )
    assert bad_detail.status_code == 422

    ds.save_requirements(sid, {
        "payload_kg": 1.0, "mtow_kg": 3.5, "cruise_speed_ms": 18.0,
        "cruise_altitude_m": 0.0, "launch": "hand", "span_max_m": 2.0,
    })
    stale = client.post("/api/stage1/airfoils/select", json={
        "session_id": sid, "schedule_hash": rec["strategies"][0]["schedule_hash"]})
    assert stale.status_code == 409


def test_generate_requires_current_approval_and_passes_it_explicitly(api, monkeypatch):
    client, sid, candidates, _ds = api
    missing = client.post("/api/stage1/generate", json={"session_id": sid})
    assert missing.status_code == 409

    rec = client.post("/api/stage1/airfoils/recommend", json={"session_id": sid}).json()
    chosen = rec["strategies"][0]
    approval = client.post("/api/stage1/airfoils/select", json={
        "session_id": sid, "schedule_hash": chosen["schedule_hash"]}).json()
    captured = {}

    def fake_generate(session_id, *, approval, regenerate=False):
        captured.update(session_id=session_id, approval=approval,
                        regenerate=regenerate)
        return {"stale": False, "candidates": [{"key": "baseline"}]}

    monkeypatch.setattr(candidates, "generate", fake_generate)
    generated = client.post("/api/stage1/generate", json={"session_id": sid})
    assert generated.status_code == 200, generated.text
    assert generated.json()["candidates"] == [{"key": "baseline"}]
    assert generated.json()["plan_version"] == approval["sources"]["plan_version"]
    assert generated.json()["intake_version"] == approval["sources"]["intake_version"]
    assert captured["session_id"] == sid
    assert captured["approval"]["selection_hash"] == approval["selection_hash"]
    assert captured["regenerate"] is True


def test_registered_router_precedes_the_legacy_generate_route(cockpit_client):
    bootstrap = cockpit_client.get("/api/bootstrap")
    sid = bootstrap.json()["session_id"]

    response = cockpit_client.post(
        "/api/stage1/generate", json={"session_id": sid}
    )

    assert response.status_code == 409
    assert "airfoil" in response.json()["detail"].lower()


def test_malformed_bodies_use_fastapi_422(api):
    client, sid, _candidates, _ds = api
    assert client.post("/api/stage1/airfoils/recommend", json={}).status_code == 422
    assert client.post("/api/stage1/airfoils/select", json={
        "session_id": sid, "schedule_hash": 42}).status_code == 422


def test_stage2_handoff_requires_and_carries_current_selection(api):
    client, sid, _candidates, ds = api
    assert client.get(f"/api/handoff/{sid}").status_code == 409

    rec = client.post(
        "/api/stage1/airfoils/recommend", json={"session_id": sid}
    ).json()
    approval = client.post("/api/stage1/airfoils/select", json={
        "session_id": sid,
        "schedule_hash": rec["strategies"][0]["schedule_hash"],
    }).json()
    response = client.get(f"/api/handoff/{sid}")

    assert response.status_code == 200, response.text
    identity = response.json()["handoff"]["airfoil_selection"]
    assert identity["version"] == approval["version"]
    assert identity["selection_hash"] == approval["selection_hash"]
    assert [row["name"] for row in identity["sections"]] == [
        approval["schedule"][station] for station in ("root", "mid", "tip")
    ]
    assert all(row["openvsp_type"] == "XS_FILE_AIRFOIL"
               for row in identity["sections"])

    saved = ds.latest_requirements(sid)
    ds.save_requirements(sid, {
        **{key: value for key, value in saved.items()
           if key not in ("created", "version")},
        "span_max_m": 2.0,
    })
    assert client.get(f"/api/handoff/{sid}").status_code == 409
