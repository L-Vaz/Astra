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
    client, sid = stage1_session
    parsed = client.post("/api/stage1/parse",
                         json={"session_id": sid, "brief": brief})
    assert parsed.status_code == 200, parsed.text

    generated = client.post("/api/stage1/generate", json={"session_id": sid})
    assert generated.status_code == 200, generated.text

    row = next(r for r in parsed.json()["intake"]["rows"] if r["id"] == "endurance")
    assert row["number"] == pytest.approx(600.0)
    assert "capped" in row["source"], row["source"]


def test_a_capped_endurance_is_still_recorded_in_the_plan(stage1_session):
    """Capping must not quietly drop the goal: it is recorded for the propulsion
    stage, which is the row's entire purpose."""
    client, sid = stage1_session
    client.post("/api/stage1/parse",
                json={"session_id": sid, "brief": "loiter for 11 hours"})
    version = client.post("/api/stage1/generate",
                          json={"session_id": sid}).json()["plan_version"]
    assert _mission(sid, version)["mission"]["endurance_min"] == pytest.approx(600.0)


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
    assert any(w for w in response.json()["warnings"]), \
        "a substituted value must be reported"


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
    assert any("could not read" in w for w in saved.json()["warnings"])
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
    assert any("payload" in w for w in response.json()["warnings"])

    mission = _mission(sid, response.json()["plan_version"])
    assert mission["mission"]["mtow_kg"] >= mission["mission"]["payload_kg"]
    assert mission["mtow_kg"] >= mission["mission"]["payload_kg"]


# ===================================================================================
# S2-D5 — the parse endpoint merges onto stored state
# ===================================================================================

def _rows(payload) -> dict:
    return {r["id"]: r for r in payload["intake"]["rows"]}


def test_parse_preserves_user_edits_and_custom_rows(stage1_session):
    client, sid = stage1_session
    client.post("/api/stage1/parse", json={
        "session_id": sid, "brief": "surveillance, 2.2 m span, hand launch"})
    client.post("/api/stage1/intake", json={
        "session_id": sid,
        "edits": [{"id": "mtow_cap", "value": "4.5 kg"},
                  {"id": "custom_max_chord", "label": "Max chord",
                   "value": "0.35 m"}]})

    reparsed = client.post("/api/stage1/parse", json={
        "session_id": sid, "brief": "surveillance, 2.2 m span, hand launch"})
    assert reparsed.status_code == 200, reparsed.text
    rows = _rows(reparsed.json())
    assert rows["mtow_cap"]["number"] == pytest.approx(4.5)
    assert "custom_max_chord" in rows


def test_brief_overriding_an_edit_is_reported(stage1_session):
    client, sid = stage1_session
    client.post("/api/stage1/intake", json={
        "session_id": sid, "edits": [{"id": "span_max", "value": "1.4 m"}]})

    reparsed = client.post("/api/stage1/parse",
                           json={"session_id": sid, "brief": "2.2 m span"})
    assert _rows(reparsed.json())["span_max"]["number"] == pytest.approx(2.2)
    assert any("1.4" in w and "2.2" in w for w in reparsed.json()["warnings"]), \
        reparsed.json()["warnings"]


def test_replace_true_starts_from_defaults(stage1_session):
    client, sid = stage1_session
    client.post("/api/stage1/intake", json={
        "session_id": sid,
        "edits": [{"id": "span_max", "value": "1.4 m"},
                  {"id": "custom_max_chord", "label": "Max chord", "value": "0.35 m"}]})

    replaced = client.post("/api/stage1/parse", json={
        "session_id": sid, "brief": "hand launch", "replace": True})
    rows = _rows(replaced.json())
    assert rows["span_max"]["number"] == pytest.approx(3.0), "back to the default"
    assert "custom_max_chord" not in rows


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
    assert any("span_max_m" in w for w in response.json()["warnings"]), \
        response.json()["warnings"]


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
