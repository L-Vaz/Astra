"""The Stage-1 request boundary (S1-A1, S1-A2, S1-A3).

The cockpit is an unauthenticated localhost FastAPI app with no CSRF protection: any
web page the user visits while it is running can POST to it. That makes the request
boundary a security boundary, and before Stage 1 it had three holes, all
probe-confirmed against the live server:

* `session_id` was joined straight into a filesystem path, so "../escaped" and
  "/tmp/astra_probe_outside" returned 200 and CREATED stage directories outside the
  sessions root (S1-A1).
* A mutating POST with no `session_id` fell back to the most recently modified session
  in the whole store, so a request that names nothing silently edited whatever the user
  was last working on (S1-A2).
* Nothing was size-limited, and every call writes a new immutable version: one 2 MB
  edit value was persisted verbatim, 500 edits in one call produced a 519-row intake
  (S1-A3).

WSL only — importing the cockpit loads the services layer (FAISS).
"""
import os

import pytest

pytestmark = pytest.mark.wsl

# Every Stage-1 route that WRITES. A hostile id or a missing id must be refused by all
# of them; testing one and assuming the rest is how the next hole gets in.
MUTATING_POSTS = [
    ("/api/stage1/parse", {"brief": "1.2 m span hand launch"}),
    ("/api/stage1/intake", {"edits": [{"id": "payload", "value": "2 kg"}]}),
    ("/api/stage1/generate", {}),
]

HOSTILE_IDS = [
    pytest.param("../escaped", id="parent-traversal"),
    pytest.param("/tmp/astra_probe_outside", id="absolute-path"),
    pytest.param("aa/bb", id="nested-path"),
    pytest.param("..\\win", id="windows-traversal"),
    pytest.param("a" * 300, id="300-chars"),
    pytest.param("sessión", id="unicode"),
    pytest.param("AABBCCDD", id="uppercase-hex"),
    pytest.param("0e33fc0", id="seven-hex"),
    pytest.param("0e33fc0aa", id="nine-hex"),
]


def _snapshot(root) -> set:
    """Every path under a directory, for before/after comparison."""
    out = set()
    for dirpath, dirnames, filenames in os.walk(root):
        for name in list(dirnames) + list(filenames):
            out.add(os.path.join(dirpath, name))
    return out


# ---------------------------------------------------------------------------
# S1-A1 — session_id is a filesystem path segment
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("route,body", MUTATING_POSTS,
                         ids=lambda v: v if isinstance(v, str) else "")
@pytest.mark.parametrize("sid", HOSTILE_IDS)
def test_traversal_session_ids_are_rejected(stage1_session, route, body, sid):
    """A session id that is not exactly what create_session mints is refused.

    `create_session` produces `uuid4().hex[:8]` — 8 lowercase hex characters — so
    validating that shape rejects every traversal, every absolute path, and every
    over-long id without narrowing what a real client can do.
    """
    client, _ = stage1_session
    response = client.post(route, json={**body, "session_id": sid})
    assert response.status_code == 400, (
        f"{route} accepted hostile session_id {sid!r}: "
        f"{response.status_code} {response.text[:200]}")


def test_no_directory_is_created_outside_sessions_root(stage1_session, tmp_path):
    """The probe's actual finding: the server did not just accept the id, it CREATED
    intake/, plan/, candidates/ and requirements/ directories at the target."""
    client, _ = stage1_session
    before = _snapshot(tmp_path)
    for route, body in MUTATING_POSTS:
        for sid in ("../escaped", "aa/bb", "..\\win"):
            client.post(route, json={**body, "session_id": sid})
    assert _snapshot(tmp_path) == before, "a hostile session_id wrote to the filesystem"


def test_a_well_formed_unknown_session_is_404_not_400(stage1_session):
    """Shape and existence are different failures, and the caller can only act on the
    difference if the status codes differ."""
    client, _ = stage1_session
    response = client.post("/api/stage1/generate",
                           json={"session_id": "deadbeef"})
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# S1-A2 — a mutating POST must name its session
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("route,body", MUTATING_POSTS,
                         ids=lambda v: v if isinstance(v, str) else "")
@pytest.mark.parametrize("missing", [{}, {"session_id": ""}, {"session_id": None}],
                         ids=["omitted", "empty", "null"])
def test_mutating_posts_require_an_explicit_session(stage1_session, route, body,
                                                    missing):
    """No id means no write — and the live session must be untouched.

    Asserting the 400 alone would not catch the real damage: the old resolver picked
    the newest session in the store, so the request succeeded against a session the
    caller never named.
    """
    client, sid = stage1_session
    before = client.get(f"/api/stage1/intake?session={sid}").json()["version"]

    response = client.post(route, json={**body, **missing})
    assert response.status_code == 400, response.text

    after = client.get(f"/api/stage1/intake?session={sid}").json()["version"]
    assert after == before, "a session-less POST mutated the live session"


def test_get_still_resolves_the_most_recent_session(stage1_session):
    """The convenience fallback survives where it is defensible.

    Opening the cockpit with no id in the URL must keep working; only the mutating
    routes lost the default.
    """
    client, sid = stage1_session
    response = client.get("/api/stage1/intake")
    assert response.status_code == 200
    assert response.json()["session_id"] == sid


# ---------------------------------------------------------------------------
# S1-A3 — input size limits
# ---------------------------------------------------------------------------

def _versions_on_disk(sid) -> list:
    import services.design_session as ds
    root = os.path.join(ds.session_dir(sid), "intake")
    if not os.path.isdir(root):
        return []
    return sorted(n for n in os.listdir(root) if n.startswith("v"))


def test_oversize_brief_rejected(stage1_session):
    client, sid = stage1_session
    before = _versions_on_disk(sid)
    response = client.post("/api/stage1/parse",
                           json={"session_id": sid, "brief": "x" * 50_000})
    assert response.status_code == 422
    assert "brief" in response.text
    assert _versions_on_disk(sid) == before, "a rejected request still wrote a version"


def test_brief_at_the_limit_is_accepted(stage1_session):
    """Guard the guard: a limit that rejects legitimate input is a different bug."""
    client, sid = stage1_session
    from cockpit import server as cockpit_server

    prefix = "hand launch, "
    brief = prefix + "a" * (cockpit_server.MAX_BRIEF_CHARS - len(prefix))
    assert len(brief) == cockpit_server.MAX_BRIEF_CHARS
    response = client.post("/api/stage1/parse",
                           json={"session_id": sid, "brief": brief})
    assert response.status_code == 200


def test_too_many_edits_rejected(stage1_session):
    client, sid = stage1_session
    before = _versions_on_disk(sid)
    edits = [{"id": f"custom_{i}", "label": f"Row {i}", "value": "1"}
             for i in range(500)]
    response = client.post("/api/stage1/intake",
                           json={"session_id": sid, "edits": edits})
    assert response.status_code == 422
    assert "edits" in response.text
    assert _versions_on_disk(sid) == before


def test_oversize_edit_value_rejected(stage1_session):
    client, sid = stage1_session
    before = _versions_on_disk(sid)
    response = client.post(
        "/api/stage1/intake",
        json={"session_id": sid,
              "edits": [{"id": "payload", "value": "2" * (2 * 1024 * 1024)}]})
    assert response.status_code == 422
    assert _versions_on_disk(sid) == before


def test_the_row_count_cannot_grow_at_all(stage1_session):
    """S1-B: replaces test_row_count_is_capped. MAX_ROWS = 64 bounded a runaway client
    because every unknown edit id became a new row. Unknown ids are now refused, so the
    row list is fixed and the attack is closed at its source rather than capped
    downstream — 96 attempted additions add nothing and write no version."""
    from domain_packs.bwb import intake as intake_pack

    client, sid = stage1_session
    before_rows = client.get(f"/api/stage1/intake?session={sid}").json()["intake"]["rows"]
    before_versions = _versions_on_disk(sid)

    for batch in range(3):
        response = client.post("/api/stage1/intake", json={
            "session_id": sid,
            "edits": [{"id": f"custom_{batch}_{i}", "label": f"Row {i}",
                       "value": "1"} for i in range(32)]})
        assert response.status_code == 422, response.text
        assert f"custom_{batch}_0" in response.text, "the refusal must name the row"

    after_rows = client.get(f"/api/stage1/intake?session={sid}").json()["intake"]["rows"]
    assert len(after_rows) == len(before_rows)
    assert _versions_on_disk(sid) == before_versions, "a refused edit wrote a version"
    assert all(r["id"] in intake_pack.known_row_ids() for r in after_rows)


def test_a_refused_batch_does_not_lock_the_user_out(stage1_session):
    """A refusal must not leave the intake unusable: valid edits still apply after it,
    and — because apply_edits raises before any write — the refused batch's VALID edits
    are not partially applied either."""
    client, sid = stage1_session

    response = client.post("/api/stage1/intake", json={
        "session_id": sid,
        "edits": [{"id": "payload", "value": "3.25 kg"},
                  {"id": "not_a_real_row", "label": "Nope", "value": "1"}]})
    assert response.status_code == 422, response.text
    rows = client.get(f"/api/stage1/intake?session={sid}").json()["intake"]["rows"]
    payload = next(r for r in rows if r["id"] == "payload")
    assert payload["number"] != pytest.approx(3.25), "partial write from a refused batch"

    response = client.post("/api/stage1/intake", json={
        "session_id": sid, "edits": [{"id": "payload", "value": "1.75 kg"}]})
    assert response.status_code == 200, response.text
    payload = next(r for r in response.json()["intake"]["rows"] if r["id"] == "payload")
    assert payload["number"] == pytest.approx(1.75)
