"""Manifest provenance tests for the design-session store.

record_version() must leave each version directory self-describing: caller
summary, upstream stage versions, requirements version, artifact inventory
with hashes, and tool identity. Pure filesystem — no WSL, no solvers.
"""
import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


@pytest.fixture()
def ds(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRA_SESSIONS_DIR", str(tmp_path / "sessions"))
    # design_session itself is pure filesystem, but importing it goes through
    # services/__init__.py -> src/utils.py, which loads FAISS and the LLM service.
    # Skip rather than ERROR where that environment is absent (S0-2).
    try:
        from services import design_session
    except (FileNotFoundError, ImportError) as exc:
        pytest.skip(f"services layer unavailable ({type(exc).__name__}: {exc})")
    return design_session


def test_manifest_records_summary_inventory_and_tools(ds):
    sid = ds.create_session("manifest-test")
    vdir = Path(ds.new_version_dir(sid, "plan"))
    (vdir / "plan.json").write_text('{"a": 1}')
    ds.record_version(sid, "plan", {"source": "test"})

    m = json.loads((vdir / "manifest.json").read_text())
    assert m["stage"] == "plan" and m["version"] == 1
    assert m["source"] == "test"                      # caller summary preserved
    assert m["inputs"]["upstream"] == {}              # no earlier stages
    assert m["inputs"]["requirements_version"] == 0   # none saved yet

    files = {f["path"]: f for f in m["artifacts"]["files"]}
    assert set(files) == {"plan.json"}                # manifest itself excluded
    assert files["plan.json"]["bytes"] == len('{"a": 1}')
    assert files["plan.json"]["sha256"] == hashlib.sha256(b'{"a": 1}').hexdigest()
    assert m["artifacts"]["truncated"] is False
    assert m["tools"]["python"]


def test_manifest_records_upstream_and_requirements_versions(ds):
    sid = ds.create_session("manifest-test-2")
    ds.new_version_dir(sid, "plan")
    ds.record_version(sid, "plan", {})
    ds.save_requirements(sid, {"mtow_kg": 5.0})

    vdir = Path(ds.new_version_dir(sid, "geometry"))
    (vdir / "params.json").write_text("{}")
    ds.record_version(sid, "geometry", {})

    m = json.loads((vdir / "manifest.json").read_text())
    assert m["inputs"]["upstream"] == {"plan": 1}
    assert m["inputs"]["requirements_version"] == 1


def test_manifest_nested_artifacts_use_forward_slashes(ds):
    sid = ds.create_session("manifest-test-3")
    vdir = Path(ds.new_version_dir(sid, "geometry"))
    (vdir / "renders").mkdir()
    (vdir / "renders" / "top.png").write_bytes(b"png")
    ds.record_version(sid, "geometry", {})

    m = json.loads((vdir / "manifest.json").read_text())
    assert [f["path"] for f in m["artifacts"]["files"]] == ["renders/top.png"]
