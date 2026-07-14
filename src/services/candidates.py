"""Design-candidate service — the multi-design funnel over the session store.

Tier 0 (equations, domain_packs/bwb/variants.py) proposes several named
candidates. Each can then be built (OpenVSP STL, tier-0.5 geometry gates),
screened (VSPAERO, tier 1, hybrid drag bookkeeping), and finally promoted
onto the session's main geometry line for meshing and OpenFOAM (tier 2).

Layout:  <sessions_root>/<sid>/candidates/plan_v<P>/<key>/
             candidate.json            tier-0 record + status + evidence
             geometry/...              OpenVSP artifacts once built
             fast_aero/...             VSPAERO artifacts once screened
         <sessions_root>/<sid>/candidates/plan_v<P>/index.json

A candidate batch is tied to the plan version that produced it; changing the
mission starts a fresh batch. Promotion writes a normal geometry version on
the main line, so rollback/versions keep working unchanged.
"""
import json
import math
import os
import time
from typing import Any, Dict, List, Optional

from domain_packs.bwb.schema import BWBParams, GeometryReport, MissionDerived
from domain_packs.bwb.variants import DesignCandidate, generate_candidates

from . import design_session as ds
from .geometry import GeometryRejected, build_geometry
from .vspaero import FastAeroRejected, run_fast_aero

STATUSES = ["candidate", "built", "screened", "promoted", "discarded", "failed"]


class CandidateError(Exception):
    pass


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------

def _plan_derived(sid: str) -> MissionDerived:
    d = ds.latest_dir(sid, "plan")
    if not d or not os.path.exists(os.path.join(d, "mission_derived.json")):
        raise CandidateError("session has no plan — set a mission first")
    with open(os.path.join(d, "mission_derived.json")) as f:
        return MissionDerived.model_validate_json(f.read())


def _plan_version(sid: str) -> int:
    return ds.get_state(sid)["stages"]["plan"]["latest"]


def _batch_dir(sid: str, plan_version: Optional[int] = None) -> str:
    pv = plan_version if plan_version is not None else _plan_version(sid)
    return os.path.join(ds.sessions_root(), sid, "candidates", f"plan_v{pv}")


def _cand_dir(sid: str, key: str) -> str:
    return os.path.join(_batch_dir(sid), key)


def _cand_file(sid: str, key: str) -> str:
    return os.path.join(_cand_dir(sid, key), "candidate.json")


def _load_candidate(sid: str, key: str) -> Dict[str, Any]:
    path = _cand_file(sid, key)
    if not os.path.exists(path):
        raise CandidateError(f"unknown candidate '{key}' for the current plan")
    with open(path) as f:
        return json.load(f)


def _save_candidate(sid: str, record: Dict[str, Any]) -> None:
    path = _cand_file(sid, record["key"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(record, f, indent=2)


def _index_path(sid: str) -> str:
    return os.path.join(_batch_dir(sid), "index.json")


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------

def generate(sid: str, regenerate: bool = False) -> Dict[str, Any]:
    """Tier 0: create (or return) the candidate batch for the current plan."""
    derived = _plan_derived(sid)
    idx = _index_path(sid)
    if os.path.exists(idx) and not regenerate:
        return list_candidates(sid)

    cands = generate_candidates(derived)
    keys = []
    for c in cands:
        record = {
            "key": c.key,
            "title": c.title,
            "objective": c.objective,
            "is_baseline": c.is_baseline,
            "status": "candidate",
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "params": c.params.model_dump(),
            "panel": c.panel.model_dump(),
            "provenance": c.provenance,
            "constraint_notes": c.constraint_notes,
            "tradeoffs": c.tradeoffs,
            "evidence": {"equation": {"passed": True}},
            "artifacts": {},
        }
        _save_candidate(sid, record)
        keys.append(c.key)
    os.makedirs(os.path.dirname(idx), exist_ok=True)
    with open(idx, "w") as f:
        json.dump({"plan_version": _plan_version(sid),
                   "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "keys": keys}, f, indent=2)
    return list_candidates(sid)


def list_candidates(sid: str) -> Dict[str, Any]:
    idx = _index_path(sid)
    if not os.path.exists(idx):
        return {"plan_version": _plan_version(sid), "generated": None,
                "candidates": []}
    with open(idx) as f:
        meta = json.load(f)
    return {"plan_version": meta["plan_version"], "generated": meta["generated"],
            "candidates": [_load_candidate(sid, k) for k in meta["keys"]]}


def build(sid: str, key: str) -> Dict[str, Any]:
    """Tier 0.5: OpenVSP build + geometry gates + watertight STL."""
    record = _load_candidate(sid, key)
    params = BWBParams.model_validate(record["params"])
    outdir = os.path.join(_cand_dir(sid, key), "geometry")
    try:
        report = build_geometry(params, outdir)
    except GeometryRejected as e:
        record["status"] = "failed"
        record["evidence"]["geometry"] = {
            "passed": False, "reason": str(e),
            "gates": (e.report or {}).get("gates", [])}
        _save_candidate(sid, record)
        raise CandidateError(f"geometry rejected: {e}")

    record["status"] = "built" if record["status"] == "candidate" else record["status"]
    record["evidence"]["geometry"] = {
        "passed": report.all_gates_pass,
        "s_ref_m2": report.s_ref_m2,
        "aspect_ratio": report.aspect_ratio,
        "mac_m": report.mean_aero_chord_m,
        "n_facets": report.n_facets,
        "watertight": report.watertight,
        "gates": [g.model_dump() for g in report.gates],
    }
    record["artifacts"]["stl"] = os.path.join(outdir, "bwb.stl")
    record["artifacts"]["vsp3"] = report.artifacts.get("vsp3", "")
    _save_candidate(sid, record)
    return record


def _candidate_derived(base: MissionDerived, record: Dict[str, Any]) -> MissionDerived:
    """Per-candidate sizing context: same mission, this candidate's wing.
    Required CL is recomputed for the candidate's own area."""
    panel = record["panel"]
    return base.model_copy(update={
        "params": BWBParams.model_validate(record["params"]),
        "s_ref_target_m2": panel["s_ref_m2"],
        "wing_loading_kgm2": panel["wing_loading_kgm2"],
        "required_cl": panel["cl_cruise"],
        "reynolds_mac": panel["reynolds_mac"],
    })


def screen(sid: str, key: str) -> Dict[str, Any]:
    """Tier 1: VSPAERO sweep + hybrid-drag rescore.

    The thin-surface VLM sees induced drag and the neutral point but not
    profile drag, so the hybrid total is CD0(equation) + CD(VSPAERO) and both
    contributions stay visible."""
    record = _load_candidate(sid, key)
    if "geometry" not in record["evidence"]:
        build(sid, key)
        record = _load_candidate(sid, key)

    geo_dir = os.path.join(_cand_dir(sid, key), "geometry")
    with open(os.path.join(geo_dir, "geometry_report.json")) as f:
        report = GeometryReport.model_validate(json.load(f))
    derived = _candidate_derived(_plan_derived(sid), record)

    outdir = os.path.join(_cand_dir(sid, key), "fast_aero")
    try:
        fast = run_fast_aero(report, derived, outdir)
    except FastAeroRejected as e:
        record["evidence"]["fast_aero"] = {
            "passed": False, "reason": str(e),
            "gates": (e.result or {}).get("gates", [])}
        _save_candidate(sid, record)
        raise CandidateError(f"fast aero rejected: {e}")

    panel = record["panel"]
    cl = panel["cl_cruise"]
    ar = panel["aspect_ratio"]
    # induced component only: VSPAERO's CDtot bundles its own skin-friction
    # estimate, which would double-count against the equation CD0
    cd_ind = fast.get("cdi_at_trim")
    if cd_ind is None and fast.get("cd_at_trim") is not None \
            and fast.get("cdo_at_trim") is not None:
        cd_ind = fast["cd_at_trim"] - fast["cdo_at_trim"]
    hybrid = {}
    if cd_ind:
        cd_total = panel["cd0"] + cd_ind
        hybrid = {
            "cd0_equation": panel["cd0"],
            "cd0_vlm_estimate": fast.get("cdo_at_trim"),
            "cd_induced_vlm": cd_ind,
            "cd_total_hybrid": cd_total,
            "ld_hybrid": cl / cd_total,
            "oswald_e_measured": cl ** 2 / (math.pi * ar * cd_ind),
        }
    record["evidence"]["fast_aero"] = {
        "passed": all(g["passed"] for g in fast.get("gates", [])),
        "alpha_trim_deg": fast.get("alpha_trim_deg"),
        "cl_alpha_per_deg": fast.get("cl_alpha_per_deg"),
        "x_np_m": fast.get("x_np_m"),
        "ld_at_trim_vlm": fast.get("ld_at_trim"),
        "gates": fast.get("gates", []),
        "hybrid": hybrid,
        "deltas_vs_equation": {
            "ld": (hybrid.get("ld_hybrid") - panel["ld_cruise"]) if hybrid else None,
            "oswald_e": (hybrid.get("oswald_e_measured") - panel["oswald_e"]) if hybrid else None,
            "x_np_m": (fast.get("x_np_m") - panel["x_np_m"])
                      if fast.get("x_np_m") is not None else None,
        },
    }
    record["status"] = "screened" if record["status"] in ("candidate", "built") \
        else record["status"]
    _save_candidate(sid, record)
    return record


def promote(sid: str, key: str, note: str = "") -> Dict[str, Any]:
    """Adopt a candidate onto the main geometry line (new geometry version).
    Downstream mesh/CFD invalidation is handled by record_version as usual."""
    record = _load_candidate(sid, key)
    params = BWBParams.model_validate(record["params"])
    vdir = ds.new_version_dir(sid, "geometry")
    try:
        report = build_geometry(params, vdir)
    except GeometryRejected as e:
        raise CandidateError(f"promotion build rejected: {e}")
    version = ds.record_version(sid, "geometry", {
        "source": "candidate",
        "candidate_key": key,
        "candidate_title": record["title"],
        "note": note or f"promoted candidate '{record['title']}'",
        "passed": report.all_gates_pass,
    })
    record["status"] = "promoted"
    record["promoted_geometry_version"] = version
    _save_candidate(sid, record)
    return {"record": record, "geometry_version": version,
            "report": json.loads(report.model_dump_json())}


def discard(sid: str, key: str, reason: str = "") -> Dict[str, Any]:
    record = _load_candidate(sid, key)
    record["status"] = "discarded"
    if reason:
        record["discard_reason"] = reason
    _save_candidate(sid, record)
    return record


def stl_path(sid: str, key: str) -> str:
    return os.path.join(_cand_dir(sid, key), "geometry", "bwb.stl")
