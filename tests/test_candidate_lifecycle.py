"""The candidate lifecycle state machine (S5-H1..H10).

`STATUSES` was declared in candidates.py and read by nothing: no validation, no legal
transitions, no guard at the HTTP layer. The measured consequences were records that
contradicted themselves —

  * `discarded` -> promote succeeded and the record kept its `discard_reason`
  * `promoted` -> discard succeeded while the live geometry manifest still named that
    candidate as its source
  * `promoted` -> promote again OVERWROTE `promoted_geometry_version` (v2 -> v3)
  * `failed` -> build (now passing) left `status="failed"` beside `passed=True`
  * a failed promote left `geometry/v1` on disk, so the NEXT promote reused the
    directory and its manifest hashed the previous candidate's files as evidence

OpenVSP is not available here, so the builder is monkeypatched: these tests are about
the state machine and the bookkeeping around it, not about geometry.

WSL only — the services layer imports FAISS.
"""
import itertools
import json
import os

import pytest

pytestmark = pytest.mark.wsl


@pytest.fixture()
def cand():
    try:
        from services import candidates
    except (FileNotFoundError, ImportError) as exc:
        pytest.skip(f"services layer unavailable ({type(exc).__name__}: {exc})")
    return candidates


class _FakeGate:
    def __init__(self, name="watertight", passed=True):
        self.name, self.passed = name, passed

    def model_dump(self):
        return {"gate": self.name, "passed": self.passed}


class _FakeReport:
    """The shape build_geometry returns, minus OpenVSP."""

    def __init__(self, passed=True):
        self.all_gates_pass = passed
        self.gates = [_FakeGate(passed=passed)]
        self.s_ref_m2 = 0.5
        self.aspect_ratio = 5.0
        self.mean_aero_chord_m = 0.33
        self.n_facets = 1000
        self.watertight = True
        self.artifacts = {"vsp3": "x.vsp3"}

    def model_dump_json(self):
        return json.dumps({"all_gates_pass": self.all_gates_pass})


def _fake_builder(passed=True, raises=None):
    def build(params, outdir):
        os.makedirs(outdir, exist_ok=True)
        if raises is not None:
            raise raises
        report = _FakeReport(passed)
        with open(os.path.join(outdir, "geometry_report.json"), "w") as f:
            json.dump({
                "params": params.model_dump(), "s_ref_m2": report.s_ref_m2,
                "aspect_ratio": report.aspect_ratio,
                "mean_aero_chord_m": report.mean_aero_chord_m,
                "span_m": params.span_m, "n_facets": report.n_facets,
                "bbox_m": [params.span_m, params.root_chord_m, 0.1],
                "open_edges": 0,
                "watertight": True, "all_gates_pass": passed,
                "gates": [{"gate": "watertight", "passed": passed}],
                "artifacts": {"stl": os.path.join(outdir, "bwb.stl")},
            }, f)
        with open(os.path.join(outdir, "bwb.stl"), "w") as f:
            f.write("solid x\nendsolid x\n")
        return report
    return build


@pytest.fixture()
def batch(stage1_session, cand, monkeypatch):
    """(candidates module, sid, first key) with a generated batch."""
    client, sid = stage1_session
    client.post("/api/requirements", json={
        "session_id": sid, "requirements": {"span_max_m": 2.4}, "persist": True})
    from services import airfoil_selection
    recommendation = airfoil_selection.recommend(sid)
    approval = airfoil_selection.select(
        sid, recommendation["strategies"][0]["schedule_hash"])
    listing = cand.generate(sid, approval=approval, regenerate=True)
    return cand, sid, listing["candidates"][1]["key"], client


# ===================================================================================
# S5-H1 — the machine exists and is total
# ===================================================================================

def test_transition_table_is_total(cand):
    """Every status has a defined set of successors — no state may be a dead end the
    code discovers at runtime."""
    assert set(cand.TRANSITIONS) == set(cand.STATUSES)
    for status, allowed in cand.TRANSITIONS.items():
        assert allowed <= set(cand.STATUSES), status
        assert allowed, f"{status} has no legal successor"


@pytest.mark.parametrize("current,new", [
    (c, n) for c, n in itertools.product(
        ["candidate", "built", "screened", "promoted", "discarded", "failed",
         "screen_failed"],
        ["candidate", "built", "screened", "promoted", "discarded", "failed",
         "screen_failed"])
])
def test_illegal_transitions_are_refused(cand, current, new):
    record = {"status": current}
    if new in cand.TRANSITIONS[current]:
        cand._transition(record, new, "test")
        assert record["status"] == new
    else:
        with pytest.raises(cand.CandidateError, match="cannot"):
            cand._transition(record, new, "test")
        assert record["status"] == current, "a refused transition still wrote"


def test_status_has_exactly_one_writer(cand):
    """Proven by source inspection: `_transition` is the only assignment."""
    import inspect
    import re

    source = inspect.getsource(cand)
    assignments = re.findall(r'record\["status"\]\s*=', source)
    assert len(assignments) == 1, (
        f"{len(assignments)} direct writes to record['status']; all status changes "
        f"must go through _transition")


# ===================================================================================
# S5-H2 / H3 — preconditions and terminal states
# ===================================================================================

def test_discarded_cannot_be_promoted(batch, monkeypatch):
    cand, sid, key, _ = batch
    monkeypatch.setattr(cand, "build_geometry", _fake_builder())
    cand.discard(sid, key, reason="too heavy")
    with pytest.raises(cand.CandidateError, match="discarded"):
        cand.promote(sid, key)


def test_a_restored_candidate_loses_its_discard_reason(batch):
    cand, sid, key, _ = batch
    cand.discard(sid, key, reason="too heavy")
    restored = cand.restore(sid, key)
    assert restored["status"] == "candidate"
    assert "discard_reason" not in restored, \
        "a restored record still claimed why it was thrown away"


def test_promoted_cannot_be_discarded_while_geometry_sources_it(batch, monkeypatch):
    cand, sid, key, _ = batch
    monkeypatch.setattr(cand, "build_geometry", _fake_builder())
    cand.promote(sid, key)
    with pytest.raises(cand.CandidateError, match="promoted design"):
        cand.discard(sid, key)


def test_repromote_appends_a_version(batch, monkeypatch):
    cand, sid, key, _ = batch
    monkeypatch.setattr(cand, "build_geometry", _fake_builder())
    first = cand.promote(sid, key)["geometry_version"]
    second = cand.promote(sid, key)["geometry_version"]
    record = cand._load_candidate(sid, key)
    assert second != first
    assert record["promoted_geometry_versions"] == [first, second], \
        "the first geometry version reference was lost"


def test_build_on_discarded_is_refused(batch, monkeypatch):
    cand, sid, key, _ = batch
    monkeypatch.setattr(cand, "build_geometry", _fake_builder())
    cand.discard(sid, key)
    with pytest.raises(cand.CandidateError, match="discarded"):
        cand.build(sid, key)


# ===================================================================================
# S5-H4 — status is derived from evidence, not ratcheted
# ===================================================================================

def test_failed_candidate_recovers_after_a_passing_rebuild(batch, monkeypatch):
    cand, sid, key, _ = batch
    from services.geometry import GeometryRejected

    monkeypatch.setattr(cand, "build_geometry",
                        _fake_builder(raises=GeometryRejected("tip too thin")))
    with pytest.raises(cand.CandidateError):
        cand.build(sid, key)
    assert cand._load_candidate(sid, key)["status"] == "failed"

    monkeypatch.setattr(cand, "build_geometry", _fake_builder())
    record = cand.build(sid, key)
    assert record["status"] == "built", \
        "status stayed 'failed' beside evidence saying it passed"
    assert record["evidence"]["geometry"]["passed"] is True


def test_geometry_and_fast_aero_rejection_are_symmetric(batch, monkeypatch):
    """GeometryRejected set a status; FastAeroRejected left it untouched, so a
    screen-rejected candidate stayed 'built' and looked healthy."""
    cand, sid, key, _ = batch
    from services.vspaero import FastAeroRejected

    monkeypatch.setattr(cand, "build_geometry", _fake_builder())
    cand.build(sid, key)

    def reject(*args, **kwargs):
        raise FastAeroRejected("trim not found")

    monkeypatch.setattr(cand, "run_fast_aero", reject)
    with pytest.raises(cand.CandidateError):
        cand.screen(sid, key)
    record = cand._load_candidate(sid, key)
    assert record["status"] == "screen_failed"
    assert record["evidence"]["fast_aero"]["passed"] is False


# ===================================================================================
# S5-H5 — caller errors are 4xx, and environment failures leave a trace
# ===================================================================================

def test_builder_environment_failure_records_evidence(batch, monkeypatch):
    """A missing VSP_PYTHON raised FileNotFoundError straight out to a 500, leaving
    the record at 'candidate' with no trace that anything was attempted."""
    cand, sid, key, _ = batch
    monkeypatch.setattr(cand, "build_geometry",
                        _fake_builder(raises=FileNotFoundError("/opt/vsp-venv/bin/python")))
    with pytest.raises(cand.CandidateError, match="geometry build failed"):
        cand.build(sid, key)
    evidence = cand._load_candidate(sid, key)["evidence"]["geometry"]
    assert evidence["passed"] is False
    assert evidence["environment_failure"] is True
    assert "FileNotFoundError" in evidence["reason"]


def test_screen_after_a_failed_build_does_not_raise_filenotfound(batch, monkeypatch):
    """The rebuild was skipped whenever the evidence KEY existed, including after a
    failure that wrote no report — then it opened the missing file."""
    cand, sid, key, _ = batch
    from services.geometry import GeometryRejected

    monkeypatch.setattr(cand, "build_geometry",
                        _fake_builder(raises=GeometryRejected("nope")))
    with pytest.raises(cand.CandidateError):
        cand.build(sid, key)
    with pytest.raises(cand.CandidateError):     # not FileNotFoundError
        cand.screen(sid, key)


@pytest.mark.parametrize("path", [
    "/api/candidates/deadbeef",
    "/api/candidates/stl/deadbeef/max_range",
])
def test_bogus_session_returns_404_not_500(stage1_session, cand, path):
    client, _ = stage1_session
    assert client.get(path).status_code == 404


# ===================================================================================
# S5-H6 — evidence cannot record a pass it did not earn
# ===================================================================================

def test_empty_gate_list_is_not_a_pass(cand):
    assert cand._gates_passed([]) is False
    assert cand._gates_passed([{"passed": True}]) is True
    assert cand._gates_passed([{"passed": True}, {"passed": False}]) is False


def test_geometry_with_failing_gates_is_not_recorded_valid(batch, monkeypatch):
    """build_geometry only raises on a non-zero exit code, so a builder reporting a
    failed gate with exit 0 was promoted onto the main line."""
    cand, sid, key, _ = batch
    import services.design_session as ds

    monkeypatch.setattr(cand, "build_geometry", _fake_builder(passed=False))
    before = ds.get_state(sid)["stages"]["geometry"]["latest"]
    with pytest.raises(cand.CandidateError, match="gates"):
        cand.promote(sid, key)
    assert ds.get_state(sid)["stages"]["geometry"]["latest"] == before, \
        "a geometry version was recorded for a build whose gates failed"


# ===================================================================================
# S5-H7 — a failed promote leaks no version directory and no evidence
# ===================================================================================

def test_failed_promote_leaves_no_version_directory(batch, monkeypatch):
    cand, sid, key, _ = batch
    import services.design_session as ds
    from services.geometry import GeometryRejected

    geometry_root = os.path.join(ds.session_dir(sid), "geometry")
    before = sorted(os.listdir(geometry_root)) if os.path.isdir(geometry_root) else []

    monkeypatch.setattr(cand, "build_geometry",
                        _fake_builder(raises=GeometryRejected("rejected")))
    with pytest.raises(cand.CandidateError):
        cand.promote(sid, key)

    after = sorted(os.listdir(geometry_root)) if os.path.isdir(geometry_root) else []
    assert after == before, f"a failed promote left {set(after) - set(before)} behind"


def test_manifest_inventory_belongs_to_its_own_candidate(batch, monkeypatch):
    """The evidence-integrity bug: a manifest listed files written while promoting a
    DIFFERENT candidate."""
    cand, sid, key, _ = batch
    import services.design_session as ds
    from services.geometry import GeometryRejected

    listing = cand.list_candidates(sid)
    other = next(c["key"] for c in listing["candidates"] if c["key"] != key)

    monkeypatch.setattr(cand, "build_geometry",
                        _fake_builder(raises=GeometryRejected("rejected")))
    with pytest.raises(cand.CandidateError):
        cand.promote(sid, key)

    monkeypatch.setattr(cand, "build_geometry", _fake_builder())
    version = cand.promote(sid, other)["geometry_version"]

    with open(os.path.join(ds.session_dir(sid), "geometry", f"v{version}",
                           "manifest.json")) as f:
        manifest = json.load(f)
    assert manifest["candidate_key"] == other
    names = {entry["path"] for entry in manifest["artifacts"]["files"]}
    assert names, "the manifest inventoried nothing at all"
    assert names <= {"bwb.stl", "geometry_report.json"}, names


# ===================================================================================
# S5-H8 — a damaged batch degrades instead of disappearing
# ===================================================================================

def test_partially_deleted_batch_is_listable(batch):
    cand, sid, key, _ = batch
    import shutil

    shutil.rmtree(cand._cand_dir(sid, key))
    listing = cand.list_candidates(sid)
    assert key in listing["unreadable"]
    assert listing["candidates"], "one missing directory emptied the whole gallery"


def test_legacy_index_without_plan_version_is_readable(batch):
    cand, sid, key, _ = batch
    import services.design_session as ds

    ds.atomic_write_json(cand._index_path(sid), {"keys": [key]})
    listing = cand.list_candidates(sid)
    assert listing["plan_version"] is None
    assert len(listing["candidates"]) == 1


def test_stl_path_raises_for_a_missing_file(batch):
    cand, sid, key, _ = batch
    with pytest.raises(cand.CandidateError, match="no built STL"):
        cand.stl_path(sid, key)


# ===================================================================================
# S5-H9 — the atmosphere cross-check is not bypassed
# ===================================================================================

def test_candidate_derived_revalidates_the_atmosphere_invariant(batch):
    """model_copy(update=...) skips validators in pydantic v2, so
    MissionDerived.validate_atmosphere_consistency never ran for a screened
    candidate."""
    cand, sid, key, _ = batch
    base = cand._plan_derived(sid)
    record = cand._load_candidate(sid, key)

    good = cand._candidate_derived(base, record)
    assert good.params.span_m == pytest.approx(record["params"]["span_m"])

    poisoned = dict(record)
    poisoned["panel"] = dict(record["panel"], reynolds_mac=-1.0)
    with pytest.raises(cand.CandidateError, match="cross-checks"):
        cand._candidate_derived(base, poisoned)


# ===================================================================================
# S5-H10 — MDO winners cannot destroy each other
# ===================================================================================

def test_mdo_winners_at_the_same_rank_do_not_collide(batch):
    cand, sid, key, _ = batch
    from services import airfoil_selection
    approval = airfoil_selection.get_selection(sid)
    record = cand._load_candidate(sid, key)
    winners = [{"params": record["params"], "rank": 1, "score": 1.0 + i,
                "mdo": {}} for i in range(3)]

    listing = cand.add_mdo_candidates(sid, winners, approval=approval)
    keys = [c["key"] for c in listing["candidates"] if c["key"].startswith("mdo_")]
    assert len(keys) == 3
    assert len(set(keys)) == 3, f"MDO winners overwrote each other: {keys}"


def test_a_plan_bump_orphans_the_batch_rather_than_showing_it(batch):
    """Measured behaviour, documented rather than assumed: the batch directory is
    keyed by plan version, so moving the plan FORWARD hides the old batch entirely.
    It is not presented as current — it is simply gone from the listing, and its
    directory survives under the old plan_v<N> (S5-H11)."""
    cand, sid, key, _ = batch
    import services.design_session as ds

    assert cand.list_candidates(sid)["candidates"]
    old_dir = cand._batch_dir(sid)
    state = ds.get_state(sid)
    state["stages"]["plan"]["latest"] += 1
    ds._save(state)

    listing = cand.list_candidates(sid)
    assert listing["candidates"] == [], "the old batch was presented under a new plan"
    assert os.path.isdir(old_dir), "the orphaned batch was deleted, not orphaned"


def test_a_resurrected_batch_is_stale_when_requirements_moved(batch, monkeypatch):
    """The rollback case that actually bites: the plan returns to a version whose
    batch exists, but the requirements have moved on since it was sized."""
    cand, sid, key, client = batch
    assert cand.list_candidates(sid)["stale"] is False

    client.post("/api/requirements", json={
        "session_id": sid, "requirements": {"span_max_m": 1.2}, "persist": True})
    listing = cand.list_candidates(sid)
    assert listing["stale"] is True
    assert any("requirements moved" in r for r in listing["stale_reasons"]), \
        listing["stale_reasons"]


def test_an_index_naming_another_plan_version_is_stale(batch):
    """The direct S5-H11 guard: index.json records plan_version and, before this,
    nothing ever compared it to the current one at read time."""
    cand, sid, key, _ = batch
    import services.design_session as ds

    meta = cand._batch_meta(sid)
    ds.atomic_write_json(cand._index_path(sid), {**meta, "plan_version": 99})
    listing = cand.list_candidates(sid)
    assert listing["stale"] is True
    assert any("plan moved" in r for r in listing["stale_reasons"]), \
        listing["stale_reasons"]


def test_the_module_docstring_matches_rollback_behaviour(cand):
    """The docstring claimed rollback keeps working unchanged. It does not for the
    candidate batch, and saying so is the fix."""
    doc = cand.__doc__ or ""
    assert "rollback / versions keep working unchanged" not in doc
    assert "does not roll back with it" in doc


def test_a_missing_mdo_score_does_not_lose_the_batch(batch):
    cand, sid, key, _ = batch
    from services import airfoil_selection
    approval = airfoil_selection.get_selection(sid)
    record = cand._load_candidate(sid, key)
    listing = cand.add_mdo_candidates(
        sid,
        [{"params": record["params"], "rank": 1, "mdo": {}}],
        approval=approval,
    )
    mdo = next(c for c in listing["candidates"] if c["key"].startswith("mdo_"))
    assert "score unavailable" in " ".join(mdo["provenance"])
