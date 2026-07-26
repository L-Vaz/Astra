"""The Stage-1 store: concurrency, corruption recovery, idempotence (S1-B2/B3/B4/D8).

Measured before Stage 1, against the live server:

* 12 parallel `POST /api/stage1/intake` produced **5x HTTP 500**
  (`json.JSONDecodeError` from half-written files) and left only v1+v2 on disk with
  `index.latest = 2` — **10 versions silently lost**. Parallel generates failed the same
  way, leaving plan versions with no candidate batch and returning `plan_version` values
  that matched no listable state.
* A corrupt `intake.json` — which S1-B2 is exactly how you get in normal use — 500'd all
  three intake endpoints, including the GET that would have shown the user the problem.
* Three identical requests wrote three identical versions.

The fixes are an atomic write helper, one advisory lock per session, and
content-addressed dedupe. This module holds the tests that would have caught all three.

WSL only — importing the cockpit loads the services layer (FAISS).
"""
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

pytestmark = pytest.mark.wsl


@pytest.fixture()
def store():
    """services.design_session, or a named skip.

    design_session is pure filesystem, but importing it goes through
    services/__init__.py -> src/utils.py, which loads FAISS. Tests that do not take the
    cockpit fixture need the same guard, or they ERROR on the Windows python instead of
    skipping (S0-2).
    """
    try:
        from services import design_session
    except (FileNotFoundError, ImportError) as exc:
        pytest.skip(f"services layer unavailable ({type(exc).__name__}: {exc})")
    return design_session


def _intake_root(sid: str) -> str:
    import services.design_session as ds
    return os.path.join(ds.session_dir(sid), "intake")


def _versions(sid: str) -> list:
    root = _intake_root(sid)
    if not os.path.isdir(root):
        return []
    return sorted(int(n[1:]) for n in os.listdir(root)
                  if n.startswith("v") and n[1:].isdigit())


def _index_latest(sid: str) -> int:
    with open(os.path.join(_intake_root(sid), "index.json")) as f:
        return json.load(f)["latest"]


def _corrupt(sid: str, version: int) -> None:
    """Truncated JSON — exactly what an interrupted non-atomic write leaves behind."""
    path = os.path.join(_intake_root(sid), f"v{version}", "intake.json")
    with open(path, "w") as f:
        f.write('{"rows": [{"id": "payload", "va')


# ===================================================================================
# S1-B2 — concurrency
# ===================================================================================

def test_parallel_intake_saves_lose_nothing(stage1_session):
    """12 concurrent distinct edits must produce 12 readable versions.

    Distinct values on purpose: identical payloads would be collapsed by the S1-D8
    dedupe and the test would pass without proving anything about locking.
    """
    client, sid = stage1_session

    def save(i):
        return client.post("/api/stage1/intake", json={
            "session_id": sid,
            "edits": [{"id": "payload", "value": f"{i + 1}.5 kg"}]})

    with ThreadPoolExecutor(max_workers=12) as pool:
        responses = list(pool.map(save, range(12)))

    assert [r.status_code for r in responses] == [200] * 12, \
        [r.text[:120] for r in responses if r.status_code != 200]

    versions = _versions(sid)
    assert len(versions) == 12, f"versions lost: {versions}"
    assert versions == list(range(1, 13))
    assert _index_latest(sid) == 12

    # every returned version number is real, and every file on disk parses
    assert sorted(r.json()["version"] for r in responses) == list(range(1, 13))
    from domain_packs.bwb import intake as intake_pack
    for version in versions:
        path = os.path.join(_intake_root(sid), f"v{version}", "intake.json")
        with open(path) as f:
            intake_pack.Intake.model_validate(json.load(f))


def test_parallel_generates_leave_a_consistent_store(stage1_session):
    """Every returned plan_version must be listable and have a candidate batch.

    The old failure was not a crash but an inconsistency: plan v1-v3 on disk, no
    candidate batch, and returned plan_version values matching no listable state.
    """
    import services.design_session as ds

    client, sid = stage1_session

    def generate(_):
        return client.post("/api/stage1/generate", json={"session_id": sid})

    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(pool.map(generate, range(6)))

    assert [r.status_code for r in responses] == [200] * 6, \
        [r.text[:200] for r in responses if r.status_code != 200]

    state = ds.get_state(sid)
    for response in responses:
        version = response.json()["plan_version"]
        assert version <= state["stages"]["plan"]["latest"]
        vdir = os.path.join(ds.session_dir(sid), "plan", f"v{version}")
        assert os.path.exists(os.path.join(vdir, "mission_derived.json"))
        assert os.path.exists(os.path.join(vdir, "manifest.json"))
        batch = os.path.join(ds.session_dir(sid), "candidates", f"plan_v{version}",
                             "index.json")
        assert os.path.exists(batch), f"plan v{version} has no candidate batch"
        assert response.json()["candidates"], "generate returned no candidates"


def test_atomic_write_never_leaves_a_partial_file(store, tmp_path):
    """A reader either sees the old content or the new one, never a prefix of the new.

    Proven with a write that fails partway: os.replace never runs, so the previous
    content is intact and no temp file is left beside it.
    """
    ds = store
    target = tmp_path / "artifact.json"
    ds.atomic_write(str(target), '{"v": 1}')

    with pytest.raises(TypeError):
        ds.atomic_write(str(target), object())  # not a string: fails mid-write

    assert target.read_text() == '{"v": 1}', "a failed write damaged the target"
    leftovers = [p.name for p in tmp_path.iterdir() if ".tmp." in p.name]
    assert not leftovers, f"temp files left behind: {leftovers}"


def test_session_lock_is_reentrant_within_a_thread(stage1_session):
    """Routes hold the lock while calling helpers that take it again; a non-reentrant
    lock would deadlock every generate."""
    import services.design_session as ds

    _, sid = stage1_session
    with ds.session_lock(sid):
        with ds.session_lock(sid):
            assert True


def test_session_lock_excludes_other_threads(stage1_session):
    """The point of the lock: two threads cannot be inside it at once."""
    import services.design_session as ds

    _, sid = stage1_session
    inside = []

    def worker():
        with ds.session_lock(sid):
            inside.append("second")

    with ds.session_lock(sid):
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=0.5)
        assert inside == [], "another thread entered a locked session"
    thread.join(timeout=10)
    assert inside == ["second"], "the lock was not released"


def test_a_stuck_lock_becomes_an_error_not_a_hang(stage1_session, monkeypatch):
    """A held lock must time out with a named error. A request that blocks forever is
    worse than one that fails: nothing tells the user what happened."""
    import services.design_session as ds

    _, sid = stage1_session
    monkeypatch.setattr(ds, "LOCK_TIMEOUT_S", 0.2)
    ds._lock_registry.clear()  # pick up the shortened timeout

    def hold():
        with ds.session_lock(sid):
            barrier.wait()
            done.wait(timeout=10)

    barrier, done = threading.Barrier(2), threading.Event()
    holder = threading.Thread(target=hold)
    holder.start()
    try:
        barrier.wait(timeout=10)
        with pytest.raises(ds.SessionBusy) as excinfo:
            with ds.session_lock(sid):
                pass
        assert sid in str(excinfo.value)
    finally:
        done.set()
        holder.join(timeout=10)
        ds._lock_registry.clear()


# ===================================================================================
# S1-B3 / S1-B4 — a damaged store degrades instead of 500-ing
# ===================================================================================

def _save_two_versions(client, sid):
    for value in ("1.5 kg", "2.5 kg"):
        response = client.post("/api/stage1/intake", json={
            "session_id": sid, "edits": [{"id": "payload", "value": value}]})
        assert response.status_code == 200
    assert _versions(sid) == [1, 2]


def test_corrupt_intake_recovers_to_previous_version(stage1_session):
    """All three intake endpoints answer 200 and name the damaged version."""
    client, sid = stage1_session
    _save_two_versions(client, sid)
    _corrupt(sid, 2)

    read = client.get(f"/api/stage1/intake?session={sid}")
    assert read.status_code == 200, read.text
    assert read.json()["version"] == 1
    assert any("v2" in w for w in read.json()["warnings"]), read.json()["warnings"]

    edit = client.post("/api/stage1/intake", json={
        "session_id": sid, "edits": [{"id": "payload", "value": "3.5 kg"}]})
    assert edit.status_code == 200, edit.text
    assert any("v2" in w for w in edit.json()["warnings"])

    generate = client.post("/api/stage1/generate", json={"session_id": sid})
    assert generate.status_code == 200, generate.text


def test_the_corrupt_file_is_never_repaired_in_place(stage1_session):
    """The damaged bytes are the user's data. Recovery reads around them and the next
    save lands on a NEW version beside them — it never overwrites or deletes."""
    client, sid = stage1_session
    _save_two_versions(client, sid)
    _corrupt(sid, 2)
    damaged = os.path.join(_intake_root(sid), "v2", "intake.json")
    before = open(damaged).read()

    saved = client.post("/api/stage1/intake", json={
        "session_id": sid, "edits": [{"id": "payload", "value": "3.5 kg"}]})
    assert saved.json()["version"] == 3
    assert open(damaged).read() == before, "recovery rewrote the corrupt file"


def test_all_versions_corrupt_returns_default_with_warning(stage1_session):
    client, sid = stage1_session
    _save_two_versions(client, sid)
    _corrupt(sid, 1)
    _corrupt(sid, 2)

    response = client.get(f"/api/stage1/intake?session={sid}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["version"] == 0
    assert body["intake"]["rows"], "the screen must not open empty"
    assert body["warnings"] and "v1" in body["warnings"][0] \
        and "v2" in body["warnings"][0], body["warnings"]


def test_index_pointing_at_missing_version_recovers(stage1_session):
    """S1-B4: index.latest names a version that is not there."""
    import shutil

    client, sid = stage1_session
    _save_two_versions(client, sid)
    shutil.rmtree(os.path.join(_intake_root(sid), "v2"))

    response = client.get(f"/api/stage1/intake?session={sid}")
    assert response.status_code == 200, response.text
    assert response.json()["version"] == 1
    assert response.json()["warnings"]


def test_a_corrupt_index_does_not_overwrite_an_existing_version(stage1_session):
    """If index.json is unreadable, the next save must still allocate ABOVE what is on
    disk. Trusting a corrupt index would silently overwrite the user's newest intake."""
    client, sid = stage1_session
    _save_two_versions(client, sid)
    with open(os.path.join(_intake_root(sid), "index.json"), "w") as f:
        f.write("{not json")

    response = client.post("/api/stage1/intake", json={
        "session_id": sid, "edits": [{"id": "payload", "value": "9 kg"}]})
    assert response.status_code == 200, response.text
    assert response.json()["version"] == 3
    assert _versions(sid) == [1, 2, 3]


# ===================================================================================
# S1-D8 — identical requests are idempotent
# ===================================================================================

def test_noop_edit_does_not_create_a_version(stage1_session):
    """`edits: []` is what the UI sends on any no-op interaction; three of them used to
    produce versions 1, 2 and 3."""
    client, sid = stage1_session
    versions = [client.post("/api/stage1/intake",
                            json={"session_id": sid, "edits": []}).json()["version"]
                for _ in range(3)]
    assert versions[0] == versions[1] == versions[2]
    assert len(_versions(sid)) == 1


def test_identical_parse_is_idempotent(stage1_session):
    client, sid = stage1_session
    brief = "Surveillance BWB, 250 g camera, 2.2 m span, hand launch"
    versions = [client.post("/api/stage1/parse",
                            json={"session_id": sid, "brief": brief}).json()["version"]
                for _ in range(3)]
    assert versions[0] == versions[1] == versions[2]
    assert len(_versions(sid)) == 1


def test_a_real_edit_still_creates_a_version(stage1_session):
    """Guard the guard: dedupe that swallows genuine changes is data loss, not a fix."""
    client, sid = stage1_session
    first = client.post("/api/stage1/intake", json={
        "session_id": sid, "edits": [{"id": "payload", "value": "1.5 kg"}]})
    second = client.post("/api/stage1/intake", json={
        "session_id": sid, "edits": [{"id": "payload", "value": "2.5 kg"}]})
    assert second.json()["version"] == first.json()["version"] + 1


def test_identical_generate_reuses_the_plan_version(stage1_session):
    """A mission identical to the current plan reuses its version rather than stacking
    a byte-identical copy. Candidate batches are deliberately NOT deduped."""
    import services.design_session as ds

    client, sid = stage1_session
    client.post("/api/stage1/intake", json={
        "session_id": sid, "edits": [{"id": "payload", "value": "1.5 kg"}]})

    versions = [client.post("/api/stage1/generate",
                            json={"session_id": sid}).json()["plan_version"]
                for _ in range(3)]
    assert versions[0] == versions[1] == versions[2]
    assert ds.get_state(sid)["stages"]["plan"]["latest"] == versions[0]


def test_identical_requirements_save_is_idempotent(stage1_session):
    import services.design_session as ds

    _, sid = stage1_session
    payload = {"mtow_kg": 2.0, "cruise_speed_ms": 18.0, "cruise_altitude_m": 100.0,
               "launch": "hand", "span_max_m": 1.6, "v_stall_max_ms": 9.0}
    first = ds.save_requirements(sid, payload)
    assert ds.save_requirements(sid, dict(payload)) == first
    assert ds.save_requirements(sid, {**payload, "mtow_kg": 2.5}) == first + 1
