"""One approved schedule reaches every concept, batch, promotion, and handoff."""
from __future__ import annotations

import inspect
import json
import os

import pytest

from domain_packs.bwb.defaults import derive
from domain_packs.bwb.schema import MissionSpec
from domain_packs.bwb import variants


SCHEDULE = {"root": "e474", "mid": "e472", "tip": "s5010"}


def _derived():
    return derive(MissionSpec(
        payload_kg=1.0, mtow_kg=3.5, cruise_speed_ms=18.0,
        cruise_altitude_m=0.0, launch="hand"))


def _approval(version=1, suffix="a"):
    return {
        "version": version,
        "selection_hash": f"selection-{suffix}",
        "schedule_hash": f"schedule-{suffix}",
        "schedule": dict(SCHEDULE),
        "coordinate_hashes": {key: f"coord-{key}" for key in SCHEDULE},
        "stale": False,
    }


def test_domain_candidate_generation_requires_approval_and_applies_it_after_lenses():
    parameter = inspect.signature(variants.generate_candidates).parameters["approval"]
    assert parameter.default is inspect.Parameter.empty

    candidates = variants.generate_candidates(_derived(), _approval())
    assert candidates
    for candidate in candidates:
        assert {
            "root": candidate.params.root_airfoil,
            "mid": candidate.params.mid_airfoil,
            "tip": candidate.params.tip_airfoil,
        } == SCHEDULE, candidate.key


@pytest.fixture()
def candidate_session(sessions_root):
    try:
        from services import airfoil_selection, candidates, design_session as ds
    except ImportError as exc:
        pytest.fail(f"selection propagation service is missing: {exc}")
    sid = ds.create_session("airfoil-propagation")
    derived = _derived()
    with ds.session_lock(sid):
        vdir = ds.new_version_dir(sid, "plan")
        ds.atomic_write(os.path.join(vdir, "mission_derived.json"),
                        derived.model_dump_json(indent=2))
        ds.record_version(sid, "plan", {"source": "propagation-test"})
    ds.save_requirements(sid, {
        "payload_kg": 1.0, "mtow_kg": 3.5, "cruise_speed_ms": 18.0,
        "cruise_altitude_m": 0.0, "launch": "hand",
    })
    recommendation = airfoil_selection.recommend(sid)
    approval = airfoil_selection.select(
        sid, recommendation["strategies"][0]["schedule_hash"])
    return sid, approval, airfoil_selection, candidates, ds


def test_candidate_batch_records_selection_and_stales_when_selection_changes(
        candidate_session):
    sid, approval, service, candidates, _ds = candidate_session
    batch = candidates.generate(sid, approval=approval, regenerate=True)

    assert batch["selection_version"] == approval["version"]
    assert batch["selection_hash"] == approval["selection_hash"]
    assert batch["stale"] is False
    assert all({
        "root": item["params"]["root_airfoil"],
        "mid": item["params"]["mid_airfoil"],
        "tip": item["params"]["tip_airfoil"],
    } == approval["schedule"] for item in batch["candidates"])
    baseline = next(item for item in batch["candidates"] if item["is_baseline"])
    assert baseline["params"] == approval["selected_params"]

    recommendation = service.get_recommendations(sid)
    replacement = service.select(sid, recommendation["strategies"][1]["schedule_hash"])
    assert replacement["selection_hash"] != approval["selection_hash"]
    stale = candidates.list_candidates(sid)
    assert stale["stale"] is True
    assert any("selection moved" in reason for reason in stale["stale_reasons"])


def test_candidate_generation_refuses_missing_or_stale_approval(candidate_session):
    sid, approval, service, candidates, ds = candidate_session
    with pytest.raises(candidates.CandidateError, match="approval"):
        candidates.generate(sid, approval=None, regenerate=True)

    current = ds.latest_requirements(sid)
    payload = {key: value for key, value in current.items()
               if key not in ("created", "version")}
    ds.save_requirements(sid, {**payload, "span_max_m": 2.0})
    with pytest.raises(candidates.CandidateError, match="stale"):
        candidates.generate(sid, approval=approval, regenerate=True)


def test_promotion_manifest_carries_selection_identity(candidate_session, monkeypatch):
    sid, approval, _service, candidates, ds = candidate_session
    batch = candidates.generate(sid, approval=approval, regenerate=True)
    key = batch["candidates"][0]["key"]

    from domain_packs.bwb.schema import GateResult, GeometryReport

    def fake_builder(params, outdir):
        os.makedirs(outdir, exist_ok=True)
        for filename in ("bwb.vsp3", "bwb.stl"):
            with open(os.path.join(outdir, filename), "w") as stream:
                stream.write("fixture")
        return GeometryReport(
            params=params, s_ref_m2=params.planform_area_m2(),
            mean_aero_chord_m=params.mean_aero_chord_m(),
            aspect_ratio=params.aspect_ratio(), bbox_m=[0, 0, 0, 1, 1, 1],
            n_facets=12, watertight=True, open_edges=0,
            gates=[GateResult(gate="fixture", passed=True)],
            artifacts={"vsp3": os.path.join(outdir, "bwb.vsp3")},
            airfoil_sections=[
                {"station": station, "name": name,
                 "sha256": approval["coordinate_hashes"][station],
                 "openvsp_type": "XS_FILE_AIRFOIL"}
                for station, name in approval["schedule"].items()
            ],
        )

    monkeypatch.setattr(candidates, "build_geometry", fake_builder)
    promoted = candidates.promote(sid, key)
    manifest_path = os.path.join(ds.session_dir(sid), "geometry",
                                 f"v{promoted['geometry_version']}", "manifest.json")
    with open(manifest_path) as stream:
        manifest = json.load(stream)
    assert manifest["selection_version"] == approval["version"]
    assert manifest["selection_hash"] == approval["selection_hash"]
    assert manifest["airfoil_sections"] == [
        {"station": station, "name": name,
         "sha256": approval["coordinate_hashes"][station],
         "openvsp_type": "XS_FILE_AIRFOIL"}
        for station, name in approval["schedule"].items()
    ]
