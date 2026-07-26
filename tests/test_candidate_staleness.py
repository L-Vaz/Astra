"""The candidate batch versus the requirements that produced it (S4-C3, S4-C4).

Requirements are deliberately NOT a pipeline stage (design_session.py:182-187), which
is the right design — but it meant `plan_version` did not move when a cap changed, and
`generate()` short-circuited on the mere existence of a batch index. Measured: saving
span_max 3.0 -> 1.2 and calling generate() returned the OLD batch, four of whose five
candidates violated the live cap, with `evidence.equation.passed` True on every one.

Staleness is therefore detected at READ time and the batch records the requirements
version it was built under.

WSL only — the services layer imports FAISS.
"""
import json
import os

import pytest

pytestmark = pytest.mark.wsl


@pytest.fixture()
def services():
    try:
        from services import candidates, design_session
    except (FileNotFoundError, ImportError) as exc:
        pytest.skip(f"services layer unavailable ({type(exc).__name__}: {exc})")
    return candidates, design_session


def _batch_index(design_session, sid: str) -> dict:
    from services import candidates as cand

    with open(cand._index_path(sid)) as f:
        return json.load(f)


def _seeded(client, sid, **requirements):
    """A session with a plan, a candidate batch, and known requirements."""
    response = client.post("/api/requirements", json={
        "session_id": sid, "requirements": requirements, "persist": True})
    assert response.status_code == 200, response.text
    return response


def test_batch_index_records_the_requirements_version(stage1_session, services):
    cand, ds = services
    client, sid = stage1_session
    _seeded(client, sid, span_max_m=2.4)
    cand.generate(sid, regenerate=True)

    meta = _batch_index(ds, sid)
    assert meta["requirements_version"] == ds._latest_requirements_version(sid)


def test_requirements_change_marks_the_batch_stale(stage1_session, services):
    cand, ds = services
    client, sid = stage1_session
    _seeded(client, sid, span_max_m=2.4)
    cand.generate(sid, regenerate=True)
    assert cand.list_candidates(sid)["stale"] is False

    _seeded(client, sid, span_max_m=1.2)
    listing = cand.list_candidates(sid)
    assert listing["stale"] is True
    assert listing["requirements_version"] != listing["current_requirements_version"]


def test_generate_after_a_requirements_change_regenerates(stage1_session, services):
    """The probed failure: the old batch came back, violating the live cap."""
    cand, ds = services
    client, sid = stage1_session
    _seeded(client, sid, span_max_m=2.4)
    cand.generate(sid, regenerate=True)

    _seeded(client, sid, span_max_m=1.2)
    batch = cand.generate(sid)          # NOT regenerate=True — this is the short-circuit
    assert batch["stale"] is False
    for record in batch["candidates"]:
        assert record["params"]["span_m"] <= 1.2 + 1e-6, \
            f"{record['key']} kept the old 2.4 m span cap"


def test_an_unchanged_batch_is_not_regenerated(stage1_session, services):
    """Guard the guard: staleness must not mean "rebuild on every read"."""
    cand, ds = services
    client, sid = stage1_session
    _seeded(client, sid, span_max_m=2.4)
    first = cand.generate(sid, regenerate=True)
    again = cand.generate(sid)
    assert again["generated"] == first["generated"]


def test_violating_candidate_is_flagged_and_not_silently_promotable(stage1_session,
                                                                    services):
    cand, ds = services
    client, sid = stage1_session
    # 20 kg on a 0.9 m span: the stall cap needs 2.569 m^2 of wing and the schema
    # allows 0.324 m^2 at that span. Nothing can satisfy it; everything must say so.
    _seeded(client, sid, mtow_kg=20.0, span_max_m=0.9, v_stall_max_ms=10.5)
    batch = cand.generate(sid, regenerate=True)

    violating = [r for r in batch["candidates"] if r["violations"]]
    assert violating, "no candidate reported a violation for an impossible cap"
    for record in violating:
        assert record["evidence"]["equation"]["passed"] is False
        with pytest.raises(cand.CandidateError, match="does not meet your requirements"):
            cand.promote(sid, record["key"])


def test_the_baseline_is_capped_like_every_other_card(stage1_session, services):
    """S4-C2: the baseline used to be passed through verbatim, so a plan solved at
    span_max 3.0 kept its span while all four lenses moved to a later 1.2 m cap."""
    cand, ds = services
    client, sid = stage1_session
    _seeded(client, sid, span_max_m=3.0)
    cand.generate(sid, regenerate=True)

    _seeded(client, sid, span_max_m=1.2)
    batch = cand.generate(sid, regenerate=True)
    baseline = next(r for r in batch["candidates"] if r["is_baseline"])
    assert baseline["params"]["span_m"] <= 1.2 + 1e-6, \
        f"baseline span {baseline['params']['span_m']:.4f} ignores the 1.2 m cap"


def test_gallery_response_surfaces_violations(stage1_session, services):
    """The API surface the gallery reads."""
    cand, ds = services
    client, sid = stage1_session
    _seeded(client, sid, mtow_kg=20.0, span_max_m=0.9, v_stall_max_ms=10.5)
    cand.generate(sid, regenerate=True)

    response = client.get(f"/api/candidates/{sid}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert "stale" in body
    flagged = [c for c in body["candidates"] if c["violations"]]
    assert flagged
    entry = flagged[0]["violations"][0]
    assert {"cap", "required", "achieved", "unit", "blocked_by", "message"} <= set(entry)


def test_promotion_override_requires_a_reason(stage1_session, services):
    cand, ds = services
    client, sid = stage1_session
    _seeded(client, sid, mtow_kg=20.0, span_max_m=0.9, v_stall_max_ms=10.5)
    batch = cand.generate(sid, regenerate=True)
    key = next(r["key"] for r in batch["candidates"] if r["violations"])

    refused = client.post("/api/candidates/promote", json={
        "session_id": sid, "key": key, "override_violations": True, "note": "  "})
    assert refused.status_code == 422
    assert "note" in refused.text
