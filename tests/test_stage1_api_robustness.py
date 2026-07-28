"""Stage-1 routes under hostile and damaged input (S2-B1, S2-B5, S2-D5/D6/D7).

The worst Stage-1 bugs were not crashes on absurd input — they were plain-English
briefs and ordinary clicks producing permanent damage:

* "loiter for 11 hours" parsed 200, then made EVERY later generate return 500, forever.
  GET still returned 200, so the screen showed a healthy requirements list with a dead
  Generate button and no indication which row was at fault (S2-B1).
* A negative number in a stored intake 500'd generate; S1-B3 shows how such a file
  appears in normal use (S2-B5).
* Pressing Parse after editing reverted an edited MTOW cap and DELETED a user-added
  custom row (S2-D5).
* One Stage-1 generate silently reset a persisted span_max_m of 2.4 m to 3.0 and
  v_stall_max_ms to 10.5 — Stage 1 destroying requirements set on the M3 screen
  (S2-D6).
* Payload 6 kg with an MTOW cap of 1 kg wrote a mission_derived.json describing a 1 kg
  aircraft carrying 6 kg (S2-D7).

WSL only — importing the cockpit loads the services layer (FAISS).
"""
import json
import os

import pytest

pytestmark = pytest.mark.wsl


def _messages(response) -> list:
    """Warnings are structured objects since S3-D2; most assertions want the prose."""
    return [w["message"] for w in response.json().get("warnings", [])]


def _intake_path(sid: str, version: int) -> str:
    import services.design_session as ds
    return os.path.join(ds.session_dir(sid), "intake", f"v{version}", "intake.json")


def _write_row_number(sid: str, version: int, rid: str, number) -> None:
    """Poke a value straight into the stored file — the S1-B3 damage model."""
    path = _intake_path(sid, version)
    with open(path) as f:
        stored = json.load(f)
    for row in stored["rows"]:
        if row["id"] == rid:
            row["number"] = number
    with open(path, "w") as f:
        json.dump(stored, f)


def _mission(sid: str, version: int) -> dict:
    import services.design_session as ds
    path = os.path.join(ds.session_dir(sid), "plan", f"v{version}",
                        "mission_derived.json")
    with open(path) as f:
        return json.load(f)


# ===================================================================================
# S2-B1 — no brief expressible in English can brick Generate
# ===================================================================================

@pytest.mark.parametrize("brief", [
    "loiter for 11 hours",
    "12 hour endurance",
    "endurance 900 min",
    "surveillance, 2 m span, hand launch, fly for 24 hours",
])
def test_long_endurance_brief_still_generates(stage1_session, brief):
    """The original defect was that these briefs bricked Generate. S1-A removed the
    row rather than clamping it, so the brief is now simply inert — but the route must
    still survive it, which is what this pins."""
    client, sid = stage1_session
    parsed = client.post("/api/stage1/parse",
                         json={"session_id": sid, "brief": brief})
    assert parsed.status_code == 200, parsed.text

    generated = client.post("/api/stage1/generate", json={"session_id": sid})
    assert generated.status_code == 200, generated.text

    assert not any(r["id"] == "endurance" for r in parsed.json()["intake"]["rows"])


def test_the_plan_carries_no_endurance(stage1_session):
    """S1-A: replaces test_a_capped_endurance_is_still_recorded_in_the_plan. The goal
    is no longer recorded because it is no longer collected — a requirement no equation
    reads is a promise the tool does not keep."""
    client, sid = stage1_session
    client.post("/api/stage1/parse",
                json={"session_id": sid, "brief": "loiter for 11 hours"})
    version = client.post("/api/stage1/generate",
                          json={"session_id": sid}).json()["plan_version"]
    mission = _mission(sid, version)["mission"]
    assert "endurance_min" not in mission
    assert "range_km" not in mission


# ===================================================================================
# S2-B5 — a damaged stored intake cannot 500 the generate route
# ===================================================================================

@pytest.mark.parametrize("rid", ["payload", "altitude", "mtow_cap", "span_max",
                                 "v_stall_max"])
@pytest.mark.parametrize("number", [-3.0, -500.0], ids=["neg-small", "neg-large"])
def test_negative_row_number_does_not_500(stage1_session, rid, number):
    client, sid = stage1_session
    version = client.post("/api/stage1/intake", json={
        "session_id": sid,
        "edits": [{"id": "payload", "value": "1.5 kg"}]}).json()["version"]
    _write_row_number(sid, version, rid, number)

    response = client.post("/api/stage1/generate", json={"session_id": sid})
    assert response.status_code == 200, response.text
    rejected = [w for w in response.json()["warnings"]
                if w["row_id"] == rid and w["kind"] == "rejected"]
    assert rejected, f"a substituted value must be reported: {_messages(response)}"


@pytest.mark.parametrize("number", [float("nan"), float("inf")],
                         ids=["nan", "inf"])
def test_non_finite_row_number_does_not_500(stage1_session, number):
    client, sid = stage1_session
    version = client.post("/api/stage1/intake", json={
        "session_id": sid,
        "edits": [{"id": "payload", "value": "1.5 kg"}]}).json()["version"]
    # json.dump writes NaN/Infinity, which json.load accepts back
    _write_row_number(sid, version, "span_max", number)

    response = client.post("/api/stage1/generate", json={"session_id": sid})
    assert response.status_code == 200, response.text


def test_an_unreadable_edit_is_flagged_and_does_not_500(stage1_session):
    client, sid = stage1_session
    saved = client.post("/api/stage1/intake", json={
        "session_id": sid, "edits": [{"id": "span_max", "value": "wide enough"}]})
    assert saved.status_code == 200, saved.text
    row = next(r for r in saved.json()["intake"]["rows"] if r["id"] == "span_max")
    assert row["unparsed"] is True
    assert row["value"] == "wide enough", "the user's text must survive"
    assert any("could not read" in m for m in _messages(saved))
    assert client.post("/api/stage1/generate",
                       json={"session_id": sid}).status_code == 200


# ===================================================================================
# S2-D7 — no artifact describing an aircraft lighter than its payload
# ===================================================================================

def test_payload_above_mtow_cap_is_reconciled_not_persisted(stage1_session):
    client, sid = stage1_session
    client.post("/api/stage1/intake", json={
        "session_id": sid,
        "edits": [{"id": "payload", "value": "6 kg"},
                  {"id": "mtow_cap", "value": "1 kg"}]})

    response = client.post("/api/stage1/generate", json={"session_id": sid})
    assert response.status_code == 200, response.text
    assert any("payload" in m for m in _messages(response)), _messages(response)

    mission = _mission(sid, response.json()["plan_version"])
    assert mission["mission"]["mtow_kg"] >= mission["mission"]["payload_kg"]
    assert mission["mtow_kg"] >= mission["mission"]["payload_kg"]


# ===================================================================================
# S2-D5 — the parse endpoint merges onto stored state
# ===================================================================================

def _rows(payload) -> dict:
    return {r["id"]: r for r in payload["intake"]["rows"]}


def test_parse_preserves_user_edits(stage1_session):
    """S1-B: the custom-row leg was removed with the closed requirement set. Edit
    preservation across a reparse is the contract that mattered."""
    client, sid = stage1_session
    client.post("/api/stage1/parse", json={
        "session_id": sid, "brief": "surveillance, 2.2 m span, hand launch"})
    saved = client.post("/api/stage1/intake", json={
        "session_id": sid,
        "edits": [{"id": "mtow_cap", "value": "4.5 kg"}]})
    assert saved.status_code == 200, saved.text

    reparsed = client.post("/api/stage1/parse", json={
        "session_id": sid, "brief": "surveillance, 2.2 m span, hand launch"})
    assert reparsed.status_code == 200, reparsed.text
    assert _rows(reparsed.json())["mtow_cap"]["number"] == pytest.approx(4.5)


def test_brief_overriding_an_edit_is_reported(stage1_session):
    client, sid = stage1_session
    client.post("/api/stage1/intake", json={
        "session_id": sid, "edits": [{"id": "span_max", "value": "1.4 m"}]})

    reparsed = client.post("/api/stage1/parse",
                           json={"session_id": sid, "brief": "2.2 m span"})
    assert _rows(reparsed.json())["span_max"]["number"] == pytest.approx(2.2)
    assert any("1.4" in m and "2.2" in m for m in _messages(reparsed)), \
        _messages(reparsed)


def test_replace_true_starts_from_defaults(stage1_session):
    client, sid = stage1_session
    client.post("/api/stage1/intake", json={
        "session_id": sid,
        "edits": [{"id": "span_max", "value": "1.4 m"}]})

    replaced = client.post("/api/stage1/parse", json={
        "session_id": sid, "brief": "hand launch", "replace": True})
    rows = _rows(replaced.json())
    assert rows["span_max"]["number"] == pytest.approx(3.0), "back to the default"


# ===================================================================================
# S2-D6 — Stage 1 does not destroy requirements set on the requirements screen
# ===================================================================================

def _persist_requirement(client, sid, **fields):
    response = client.post("/api/requirements", json={
        "session_id": sid, "requirements": fields, "persist": True})
    assert response.status_code == 200, response.text
    return response


def test_generate_does_not_clobber_explicit_requirements(stage1_session):
    """The probed case: a persisted 2.4 m span cap survives a generate whose span row
    is still `assumed`."""
    import services.design_session as ds

    client, sid = stage1_session
    _persist_requirement(client, sid, ld_min=14.0, span_max_m=2.4)

    assert client.post("/api/stage1/generate",
                       json={"session_id": sid}).status_code == 200
    saved = ds.latest_requirements(sid)
    assert saved["span_max_m"] == pytest.approx(2.4)
    assert saved["ld_min"] == pytest.approx(14.0)


def test_an_explicit_intake_row_still_overrides(stage1_session):
    """The sync is non-destructive, not inert: a row the user actually set must win."""
    import services.design_session as ds

    client, sid = stage1_session
    _persist_requirement(client, sid, span_max_m=2.4)
    client.post("/api/stage1/intake", json={
        "session_id": sid, "edits": [{"id": "span_max", "value": "1.6 m"}]})

    response = client.post("/api/stage1/generate", json={"session_id": sid})
    assert response.status_code == 200, response.text
    assert ds.latest_requirements(sid)["span_max_m"] == pytest.approx(1.6)
    assert any("span_max_m" in m for m in _messages(response)), _messages(response)


def test_generate_override_is_recorded_in_the_requirements_version(stage1_session):
    import services.design_session as ds

    client, sid = stage1_session
    _persist_requirement(client, sid, span_max_m=2.4)
    client.post("/api/stage1/intake", json={
        "session_id": sid, "edits": [{"id": "span_max", "value": "1.6 m"}]})
    client.post("/api/stage1/generate", json={"session_id": sid})

    overrides = ds.latest_requirements(sid)["intake_overrides"]
    entry = next(o for o in overrides if o["field"] == "span_max_m")
    assert entry["previous"] == pytest.approx(2.4)
    assert entry["new"] == pytest.approx(1.6)


def test_a_fresh_session_still_gets_requirements_from_its_intake(stage1_session):
    """Guard the guard: refusing to overwrite must not mean refusing to write."""
    import services.design_session as ds

    client, sid = stage1_session
    client.post("/api/stage1/parse", json={
        "session_id": sid,
        "brief": "surveillance, 1.8 m span, stall 9 m/s, catapult launch"})
    assert client.post("/api/stage1/generate",
                       json={"session_id": sid}).status_code == 200

    saved = ds.latest_requirements(sid)
    assert saved["span_max_m"] == pytest.approx(1.8)
    assert saved["v_stall_max_ms"] == pytest.approx(9.0)
    assert saved["launch"] == "catapult"


# ===================================================================================
# S1-I — the handoff route
#
# Added because it 500'd on its very first live request while the whole Python suite
# was green: `check_requirements(req, params)` was called with the PANEL. Nothing in
# tests/ exercised the route, so nothing could catch a wrong argument at a call site
# the type checker does not see. A route with no test is a route that has never run.
# ===================================================================================

def test_handoff_route_returns_a_complete_artifact(stage1_session):
    client, sid = stage1_session
    r = client.get(f"/api/handoff/{sid}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["schema_version"]
    h = body["handoff"]

    # the four things Stage 2 cannot work without
    assert h["geometry"]["span_m"] > 0
    assert h["flight_condition"]["cruise_speed_ms"] > 0
    assert h["requirements"]["mtow_kg"] > 0
    assert h["performance"]["cl_max_3d"] > 0

    # and the thing that makes it a handoff rather than a dump
    grades = {e["quantity"]: e["grade"] for e in h["evidence"]}
    assert grades, "an artifact with no evidence block is just a model_dump"
    assert set(grades.values()) <= {"A", "B", "C", "D"}
    assert grades["cd0"] == "D", "the number driving every L/D must travel graded"
    assert len(h["known_disagreements"]) >= 3
    assert len(h["provenance"]["airfoil_table_sha256"]) == 64


def test_handoff_requirement_status_matches_a_direct_check(stage1_session):
    """The regression guard for the actual bug: the status list must be computed from
    the GEOMETRY, not from the panel. Passing the wrong object raised AttributeError
    at request time and nowhere else."""
    from domain_packs.bwb.inverse import check_requirements
    from domain_packs.bwb.requirements import DesignRequirements
    from domain_packs.bwb.schema import BWBParams

    client, sid = stage1_session
    h = client.get(f"/api/handoff/{sid}").json()["handoff"]
    direct = check_requirements(DesignRequirements.model_validate(h["requirements"]),
                                BWBParams.model_validate(h["geometry"]))
    assert [(s["name"], s["met"]) for s in h["requirement_status"]] == \
           [(s.name, s.met) for s in direct]


def test_handoff_on_an_unknown_session_is_a_404_not_a_500(stage1_session):
    client, _ = stage1_session
    assert client.get("/api/handoff/deadbeef").status_code in (404, 422)
