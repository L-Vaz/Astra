"""MDO winners -> candidate funnel integration (Slice 2, Task 2.3).

Follows the services-test convention (imports through the services package, like
test_bwb_atmosphere_services). Uses synthetic winners so it needs no aerosandbox: the
funnel write itself is pure (params + analytic panel + mdo evidence). Verifies MDO
records are shaped like lens candidates, are idempotent on re-run, and coexist with the
equation lenses in one navigable batch.
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "src"))

from domain_packs.bwb.defaults import derive
from domain_packs.bwb.schema import BWBParams, MissionSpec
from services import candidates as cand  # noqa: E402
from services import design_session as ds  # noqa: E402


@pytest.fixture()
def session(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRA_SESSIONS_DIR", str(tmp_path))
    d = derive(MissionSpec(payload_kg=1.0, cruise_speed_ms=18.0,
                           cruise_altitude_m=300.0, launch="hand"))
    sid = ds.create_session("mdo")
    pv = ds.new_version_dir(sid, "plan")
    with open(os.path.join(pv, "mission_derived.json"), "w") as f:
        f.write(d.model_dump_json())
    ds.record_version(sid, "plan", {"passed": True})
    return sid, d


def _winners(base: BWBParams, n=3):
    out = []
    for i in range(1, n + 1):
        p = BWBParams.model_validate({**base.model_dump(),
                                      "sweep_outer_deg": base.sweep_outer_deg + i})
        out.append({"rank": i, "score": 20.0 - i, "l_over_d": 21.0 - i,
                    "static_margin": 0.05,
                    "params": p.model_dump(), "mdo": {"score": 20.0 - i}})
    return out


def test_winners_written_as_candidates(session):
    sid, d = session
    winners = _winners(d.params)
    listing = cand.add_mdo_candidates(sid, winners)
    mdo = [c for c in listing["candidates"] if c.get("source") == "mdo"]
    assert [c["key"] for c in mdo] == ["mdo_1", "mdo_2", "mdo_3"]
    rec = mdo[0]
    assert rec["status"] == "candidate"
    assert rec["panel"]["s_ref_m2"] > 0            # analytic panel attached
    assert rec["evidence"]["mdo"]["score"] == 19.0  # rank-1 score carried
    BWBParams.model_validate(rec["params"])        # gate-valid, build/screen would accept


def test_rerun_is_idempotent(session):
    sid, d = session
    winners = _winners(d.params)
    cand.add_mdo_candidates(sid, winners)
    listing = cand.add_mdo_candidates(sid, winners)
    mdo = [c for c in listing["candidates"] if c.get("source") == "mdo"]
    assert len(mdo) == len(winners)  # replaced, not duplicated


def test_coexists_with_lens_candidates(session):
    sid, d = session
    cand.generate(sid)                       # lens batch first (Suggest designs)
    cand.add_mdo_candidates(sid, _winners(d.params))
    cands = cand.list_candidates(sid)["candidates"]
    sources = {c.get("source", "lens") for c in cands}
    assert "mdo" in sources
    assert any(c.get("source") != "mdo" for c in cands)  # lenses preserved
