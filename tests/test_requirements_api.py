"""Cockpit API tests for the requirements-first explorer (M3).

Runs the FastAPI app with TestClient against a temp session store — no WSL,
no OpenVSP (the /api/revision build path is not exercised here).
"""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRA_SESSIONS_DIR", str(tmp_path / "sessions"))
    # services.design_session reads the env var at call time — safe to import once
    from fastapi.testclient import TestClient

    from cockpit import server as cockpit_server
    return TestClient(cockpit_server.app)


def _bootstrap(client):
    r = client.get("/api/bootstrap")
    assert r.status_code == 200
    return r.json()


def test_bootstrap_includes_requirements(client):
    b = _bootstrap(client)
    assert "requirements" in b and "requirements_status" in b
    assert b["requirements"]["cruise_speed_ms"] == pytest.approx(25.0)
    assert b["requirements_schema"]["title"] == "DesignRequirements"


def test_requirements_solve_and_persist(client):
    b = _bootstrap(client)
    sid = b["session_id"]
    r = client.post("/api/requirements", json={
        "session_id": sid,
        "requirements": {"ld_min": 14.0, "span_max_m": 2.4},
        "persist": True})
    assert r.status_code == 200
    d = r.json()
    assert d["requirements_version"] == 1
    assert d["proposed_params"]["span_m"] <= 2.4 + 1e-6
    assert {s["name"] for s in d["statuses"]} == {
        "cl_cruise_target", "ld_min", "v_stall_max_ms", "span_max_m"}
    assert d["panel"]["ld_cruise"] > 0
    # preview mode writes nothing
    r2 = client.post("/api/requirements", json={
        "session_id": sid, "requirements": {"ld_min": 16.0}, "persist": False})
    assert r2.status_code == 200
    assert r2.json()["requirements_version"] is None
    b2 = _bootstrap(client)
    assert b2["requirements_version"] == 1
    assert b2["requirements"]["ld_min"] == pytest.approx(14.0)


def test_requirements_validation_422(client):
    b = _bootstrap(client)
    r = client.post("/api/requirements", json={
        "session_id": b["session_id"],
        "requirements": {"ld_min": 400.0}})
    assert r.status_code == 422


def test_explain_endpoint(client):
    b = _bootstrap(client)
    r = client.post("/api/explain", json={
        "params": b["params"],
        "flight": {"mtow_kg": 5.0, "cruise_speed_ms": 25.0,
                   "cruise_altitude_m": 100.0, "launch": "hand"},
        "include": ["derivation", "sensitivity", "req_sensitivity"]})
    assert r.status_code == 200
    d = r.json()
    graph = d["derivation"]
    assert "cd_cruise" in graph["nodes"]
    cd = graph["nodes"]["cd_cruise"]
    assert cd["formula"].startswith("CD = CD")
    assert all(ch in graph["nodes"] for ch in cd["children"])
    assert any(row["param"] == "sweep_outer_deg" for row in d["sensitivity"])
    assert any(row["param"] == "cruise_speed_ms" for row in d["req_sensitivity"])


def test_mission_save_keeps_geometry_valid(client, tmp_path):
    """The orphaned-geometry fix: a mission change must NOT invalidate built
    geometry. Simulated by writing a geometry version by hand (no OpenVSP)."""
    import services.design_session as ds
    b = _bootstrap(client)
    sid = b["session_id"]

    # fake a built geometry version the way build_geometry would record one
    vdir = ds.new_version_dir(sid, "geometry")
    with open(os.path.join(vdir, "params.json"), "w") as f:
        json.dump(b["params"], f)
    ds.record_version(sid, "geometry", {"source": "test", "passed": True})
    assert ds.get_state(sid)["stages"]["geometry"]["valid"] is True

    r = client.post("/api/mission", json={
        "session_id": sid,
        "mission": {"payload_kg": 2.0, "cruise_speed_ms": 22.0,
                    "cruise_altitude_m": 150.0, "launch": "catapult"}})
    assert r.status_code == 200
    state = ds.get_state(sid)
    assert state["stages"]["geometry"]["valid"] is True, \
        "mission change orphaned the built geometry"
    assert state["stages"]["fast_aero"]["valid"] is False
    # and the requirements' flight condition followed the mission
    b2 = _bootstrap(client)
    assert b2["requirements"]["cruise_speed_ms"] == pytest.approx(22.0)
    assert b2["requirements"]["launch"] == "catapult"


def test_record_version_default_behaviour_unchanged(client):
    import services.design_session as ds
    b = _bootstrap(client)
    sid = b["session_id"]
    ds.new_version_dir(sid, "geometry")
    ds.record_version(sid, "geometry", {"source": "test", "passed": True})
    ds.new_version_dir(sid, "plan")
    ds.record_version(sid, "plan", {"source": "test"})  # no invalidate_from
    assert ds.get_state(sid)["stages"]["geometry"]["valid"] is False
