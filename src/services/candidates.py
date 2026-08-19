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

A candidate batch is tied to the plan version that produced it AND to the
requirements version it was sized under. Both are recorded in index.json and both
are compared at read time, so a batch never silently outlives the state that made
it (S4-C3, S5-H11).

What rollback does and does not preserve (the previous wording here — "rollback /
versions keep working unchanged" — was false): promotion writes a normal geometry
version, so the geometry line rolls back like any other stage. The CANDIDATE batch
does not roll back with it. An external plan move hides the old batch; the one explicit
exception is a replacement Airfoil Sets approval, which keeps the immediately prior
gallery visible but stale for comparison. Moving the plan back restores the batch that
belongs to that plan version, which is why the
plan-version comparison above exists — it is returned as `stale` with a reason
rather than presented as current.
"""
import json
import math
import os
import re
import shutil
import time
from typing import Any, Dict, List, Optional

from domain_packs.bwb.aero_estimates import estimate
from domain_packs.bwb.requirements import DesignRequirements
from domain_packs.bwb.schema import BWBParams, GeometryReport, MissionDerived
from domain_packs.bwb.variants import DesignCandidate, generate_candidates

from . import design_session as ds
from .geometry import GeometryRejected, build_geometry
from .vspaero import FastAeroRejected, run_fast_aero

class CandidateError(Exception):
    pass


class UnknownSession(CandidateError):
    """A session id with no store behind it — a 404, not a 500 (S5-H5c)."""


# ---------------------------------------------------------------------------
# lifecycle state machine (S5-H1)
#
# STATUSES was declared and never read: no validation, no transition table, no guard
# at the HTTP layer. The measured consequences were records that contradicted
# themselves — a `discarded` candidate promoted onto the main line while keeping its
# discard reason, a `promoted` candidate discarded while the live geometry manifest
# still named it as its source, and a re-promote that overwrote the first geometry
# version reference.
#
# The table below is the whole machine. `_transition` is the ONLY writer of
# record["status"], and test_candidate_lifecycle.py asserts by source inspection that
# no other assignment exists.
# ---------------------------------------------------------------------------

STATUSES = ["candidate", "built", "screened", "promoted", "discarded", "failed",
            "screen_failed"]

TRANSITIONS: Dict[str, set] = {
    # from        -> allowed next
    "candidate":   {"built", "failed", "screened", "screen_failed", "promoted",
                    "discarded"},
    "built":       {"built", "failed", "screened", "screen_failed", "promoted",
                    "discarded"},
    "screened":    {"built", "failed", "screened", "screen_failed", "promoted",
                    "discarded"},
    # a passing rebuild RECOVERS a failed candidate (S5-H4): status is derived from
    # evidence, not ratcheted past it
    "failed":      {"built", "failed", "screened", "screen_failed", "promoted",
                    "discarded"},
    "screen_failed": {"built", "failed", "screened", "screen_failed", "promoted",
                      "discarded"},
    # terminal-ish: a promoted candidate may be promoted again (appending a version),
    # but discarding it would leave the geometry stage sourced from a discarded design
    "promoted":    {"promoted"},
    # discard is undone by restore(), never by walking straight into another state
    "discarded":   {"candidate"},
}


def _transition(record: Dict[str, Any], new_status: str, action: str) -> None:
    """The only place record["status"] is ever assigned."""
    current = record.get("status", "candidate")
    if new_status not in STATUSES:
        raise CandidateError(f"unknown status '{new_status}'")
    if new_status not in TRANSITIONS.get(current, set()):
        raise CandidateError(
            f"cannot {action} a candidate that is '{current}' "
            f"(would become '{new_status}')")
    record["status"] = new_status


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
    try:
        return ds.get_state(sid)["stages"]["plan"]["latest"]
    except FileNotFoundError:
        # every caller (list_candidates, stl_path, generate) used to leak this as a
        # bare FileNotFoundError and the HTTP layer turned it into 500 (S5-H5c)
        raise UnknownSession(f"unknown design session: {sid}")


def _batch_dir(sid: str, plan_version: Optional[int] = None) -> str:
    pv = plan_version if plan_version is not None else _plan_version(sid)
    # session_dir, not sessions_root + sid: the id reaches here straight from an HTTP
    # body, and joining it unchecked wrote outside the sessions root (S1-A1).
    return os.path.join(ds.session_dir(sid), "candidates", f"plan_v{pv}")


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


def _load_candidate_at_plan(sid: str, key: str, plan_version: int) -> Dict[str, Any]:
    path = os.path.join(_batch_dir(sid, plan_version), key, "candidate.json")
    if not os.path.exists(path):
        raise CandidateError(f"unknown candidate '{key}' for plan v{plan_version}")
    with open(path) as f:
        return json.load(f)


def _save_candidate(sid: str, record: Dict[str, Any]) -> None:
    # Atomic (S1-B2): the gallery polls these files while a build/screen/promote is
    # rewriting them, and a half-written candidate.json is a 500 on the next read.
    ds.atomic_write_json(_cand_file(sid, record["key"]), record)


def _index_path(sid: str) -> str:
    return os.path.join(_batch_dir(sid), "index.json")


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------

def _current_approval(sid: str, approval: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not approval:
        raise CandidateError("a current airfoil approval is required")
    from . import airfoil_selection
    try:
        current = airfoil_selection.get_selection(sid, require_current=True)
    except airfoil_selection.AirfoilConflict as exc:
        raise CandidateError(str(exc)) from exc
    except FileNotFoundError as exc:
        raise CandidateError("a current airfoil approval is required") from exc
    if (approval.get("version") != current.get("version")
            or approval.get("selection_hash") != current.get("selection_hash")
            or approval.get("schedule") != current.get("schedule")):
        raise CandidateError("the supplied airfoil approval is stale or not current")
    return current


def generate(sid: str, *, approval: Optional[Dict[str, Any]] = None,
             regenerate: bool = False) -> Dict[str, Any]:
    """Tier 0: create (or return) the candidate batch for the current plan.

    Regenerates when the REQUIREMENTS have moved as well as when the plan has (S4-C3).
    Requirements are deliberately not a pipeline stage, so `plan_version` does not
    change when a cap does — and the short-circuit below returned the old batch:
    saving span_max 3.0 -> 1.2 and calling generate() handed back four candidates
    violating the live cap."""
    approval = _current_approval(sid, approval)
    derived = _plan_derived(sid)
    idx = _index_path(sid)
    if os.path.exists(idx) and not regenerate and not _is_stale(sid):
        return list_candidates(sid)

    # Vehicle constraints (span/stall caps) ride along so lens sizing happens
    # inside the same box the requirements check used.
    requirements = None
    payload = ds.latest_requirements(sid)
    if payload:
        try:
            requirements = DesignRequirements.model_validate(payload)
        except Exception:
            requirements = None  # legacy payloads without the full field set

    cands = generate_candidates(derived, approval, requirements)
    keys = []
    for c in cands:
        violations = [v.model_dump() for v in c.violations]
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
            "violations": violations,
            "duplicate_of": c.duplicate_of,
            "distinct_sizings": c.distinct_sizings,
            # `passed` was unconditionally True, so a candidate breaching the user's
            # own hard cap reported success to the gallery (S4-C4).
            "evidence": {"equation": {"passed": not violations}},
            "artifacts": {},
            "selection_version": approval["version"],
            "selection_hash": approval["selection_hash"],
            "schedule_hash": approval["schedule_hash"],
            "airfoil_schedule": dict(approval["schedule"]),
            "airfoil_coordinate_hashes": dict(approval["coordinate_hashes"]),
        }
        _save_candidate(sid, record)
        keys.append(c.key)
    ds.atomic_write_json(idx, {"plan_version": _plan_version(sid),
                               "requirements_version":
                                   ds._latest_requirements_version(sid),
                               "intake_version":
                                   approval["sources"]["intake_version"],
                               "intake_hash": approval["sources"]["intake_hash"],
                               "selection_version": approval["version"],
                               "selection_hash": approval["selection_hash"],
                               "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
                               "keys": keys})
    return list_candidates(sid)


def _batch_meta(sid: str) -> Optional[Dict[str, Any]]:
    idx = _index_path(sid)
    if not os.path.exists(idx):
        return None
    with open(idx) as f:
        return json.load(f)


def _latest_stored_batch_meta(sid: str) -> Optional[Dict[str, Any]]:
    """Return the newest stored batch even when selection advanced the plan.

    Approval now creates a converged plan version.  Selecting a replacement set
    therefore moves from (for example) plan_v2 to plan_v3; retaining plan_v2 as a
    visible stale gallery is required for comparison instead of presenting an empty,
    apparently-current batch.
    """
    root = os.path.join(ds.session_dir(sid), "candidates")
    if not os.path.isdir(root):
        return None
    versions = []
    for name in os.listdir(root):
        match = re.fullmatch(r"plan_v(\d+)", name)
        if match and os.path.isfile(os.path.join(root, name, "index.json")):
            versions.append(int(match.group(1)))
    if not versions:
        return None
    with open(os.path.join(_batch_dir(sid, max(versions)), "index.json")) as f:
        return json.load(f)


def _selection_replaced_batch(sid: str, meta: Dict[str, Any]) -> bool:
    """True only for the Airfoil Sets transition that promises stale comparison.

    Ordinary plan moves retain the established candidate-lifecycle behavior: their
    old directory is not listed.  A replacement selection is distinguishable because
    it owns the current plan version and has a different immutable identity from the
    stored batch.
    """
    from . import airfoil_selection
    try:
        selection = airfoil_selection.get_selection(sid, require_current=False)
    except FileNotFoundError:
        return False
    return (
        not selection.get("stale")
        and selection.get("plan_version_created") == _plan_version(sid)
        and meta.get("plan_version") != selection.get("plan_version_created")
        and meta.get("selection_hash") is not None
        and meta.get("selection_hash") != selection.get("selection_hash")
    )


def _staleness(sid: str, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Why (and whether) the stored batch no longer describes the current state.

    Two independent inputs, both compared at READ time (S4-C3, S5-H11): the
    requirements the batch was sized under, and the plan version it belongs to. A plan
    ROLLBACK used to resurrect a stale batch silently — index.json recorded
    plan_version and nothing ever compared it."""
    meta = _batch_meta(sid) if meta is None else meta
    if meta is None:
        return {"stale": False, "reasons": []}
    reasons = []
    current_requirements = ds._latest_requirements_version(sid)
    if meta.get("requirements_version") != current_requirements:
        reasons.append(
            f"requirements moved from v{meta.get('requirements_version')} to "
            f"v{current_requirements}")
    current_plan = _plan_version(sid)
    if meta.get("plan_version") != current_plan:
        reasons.append(
            f"plan moved from v{meta.get('plan_version')} to v{current_plan}")
    from . import airfoil_selection
    try:
        selection = airfoil_selection.get_selection(sid, require_current=False)
    except FileNotFoundError:
        selection = None
    if selection is None:
        reasons.append("airfoil selection is missing")
    elif selection.get("stale"):
        reasons.append("airfoil selection is stale")
    elif (meta.get("selection_version") != selection.get("version")
          or meta.get("selection_hash") != selection.get("selection_hash")):
        reasons.append(
            f"selection moved from v{meta.get('selection_version')} to "
            f"v{selection.get('version')}")
    return {"stale": bool(reasons), "reasons": reasons,
            "requirements_version": meta.get("requirements_version"),
            "current_requirements_version": current_requirements,
            "batch_plan_version": meta.get("plan_version"),
            "current_plan_version": current_plan,
            "selection_version": meta.get("selection_version"),
            "selection_hash": meta.get("selection_hash")}


def _is_stale(sid: str) -> bool:
    return _staleness(sid)["stale"]


def list_candidates(sid: str) -> Dict[str, Any]:
    meta = _batch_meta(sid)
    if meta is None:
        previous = _latest_stored_batch_meta(sid)
        meta = previous if previous and _selection_replaced_batch(sid, previous) else None
    if meta is None:
        return {"plan_version": _plan_version(sid), "generated": None,
                "candidates": [], "stale": False, "unreadable": []}
    staleness = _staleness(sid, meta)

    # Degrade per candidate (S5-H8): one missing directory used to raise for the whole
    # listing, so a single deleted folder made the entire gallery disappear. `.get`
    # defaults for the metadata, because a legacy or truncated index.json has no
    # plan_version key and KeyError is not a useful answer either.
    candidates, unreadable = [], []
    for k in meta.get("keys", []):
        try:
            plan_version = meta.get("plan_version")
            candidates.append(
                _load_candidate(sid, k) if plan_version is None
                else _load_candidate_at_plan(sid, k, int(plan_version))
            )
        except (CandidateError, json.JSONDecodeError, OSError):
            unreadable.append(k)
    return {"plan_version": meta.get("plan_version"),
            "generated": meta.get("generated"),
            "selection_version": meta.get("selection_version"),
            "selection_hash": meta.get("selection_hash"),
            "requirements_version": staleness.get("requirements_version"),
            "current_requirements_version":
                staleness.get("current_requirements_version"),
            # staleness is detected at READ time, which is what keeps the
            # requirements store out of the stage machinery (design_session.py:182-187)
            "stale": staleness["stale"],
            "stale_reasons": staleness["reasons"],
            "unreadable": unreadable,
            "candidates": candidates}


def _gates_passed(gates: List[Dict[str, Any]]) -> bool:
    """`all([])` is True, so an empty gate list used to record a pass with nothing
    evaluated (S5-H6)."""
    return bool(gates) and all(g.get("passed") for g in gates)


def _run_builder(record: Dict[str, Any], params: BWBParams, outdir: str,
                 sid: str, what: str):
    """build_geometry with EVERY failure recorded as evidence before it propagates.

    Only GeometryRejected was caught. `build_geometry` also raises
    subprocess.TimeoutExpired (600 s), FileNotFoundError when VSP_PYTHON is absent,
    and ValidationError when the builder exits 0 without a report — all of which
    escaped as HTTP 500 leaving the record at status 'candidate' with no trace that
    anything was ever attempted (S5-H5b)."""
    try:
        return build_geometry(params, outdir)
    except GeometryRejected as e:
        record["evidence"]["geometry"] = {
            "passed": False, "reason": str(e),
            "gates": (e.report or {}).get("gates", [])}
        _transition(record, "failed", what)
        _save_candidate(sid, record)
        raise CandidateError(f"geometry rejected: {e}")
    except Exception as e:
        record["evidence"]["geometry"] = {
            "passed": False,
            "reason": f"{type(e).__name__}: {e}",
            "environment_failure": True,
            "gates": []}
        _transition(record, "failed", what)
        _save_candidate(sid, record)
        raise CandidateError(f"geometry build failed: {type(e).__name__}: {e}")


def build(sid: str, key: str) -> Dict[str, Any]:
    """Tier 0.5: OpenVSP build + geometry gates + watertight STL."""
    record = _load_candidate(sid, key)
    if record.get("status") == "discarded":
        # it used to do the work and then leave the status at 'discarded' (S5-H3d)
        raise CandidateError("this candidate is discarded — restore it first")
    params = BWBParams.model_validate(record["params"])
    outdir = os.path.join(_cand_dir(sid, key), "geometry")
    report = _run_builder(record, params, outdir, sid, "build")

    # Derived from evidence, not ratcheted: a passing rebuild recovers a failed
    # candidate instead of leaving status='failed' beside passed=True (S5-H4).
    _transition(record, "built" if report.all_gates_pass else "failed", "build")
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
    if not report.all_gates_pass:
        raise CandidateError(
            "geometry gates did not pass: "
            + "; ".join(g.name for g in report.gates if not g.passed))
    return record


def _candidate_derived(base: MissionDerived, record: Dict[str, Any]) -> MissionDerived:
    """Per-candidate sizing context: same mission, this candidate's wing.
    Required CL is recomputed for the candidate's own area.

    Rebuilt THROUGH the model, not with model_copy(update=...): in pydantic v2 that
    skips validators, so MissionDerived.validate_atmosphere_consistency never ran and
    the reynolds/density invariant was bypassed for every screened candidate (S5-H9)."""
    panel = record["panel"]
    update = {
        "params": BWBParams.model_validate(record["params"]).model_dump(),
        "s_ref_target_m2": panel["s_ref_m2"],
        "wing_loading_kgm2": panel["wing_loading_kgm2"],
        "required_cl": panel["cl_cruise"],
        "reynolds_mac": panel["reynolds_mac"],
    }
    try:
        return MissionDerived.model_validate({**base.model_dump(), **update})
    except Exception as e:
        raise CandidateError(
            f"candidate '{record['key']}' has a sizing context that fails "
            f"MissionDerived's own cross-checks: {e}")


def screen(sid: str, key: str) -> Dict[str, Any]:
    """Tier 1: VSPAERO sweep + hybrid-drag rescore.

    The thin-surface VLM sees induced drag and the neutral point but not
    profile drag, so the hybrid total is CD0(equation) + CD(VSPAERO) and both
    contributions stay visible."""
    record = _load_candidate(sid, key)
    if record.get("status") == "discarded":
        raise CandidateError("this candidate is discarded — restore it first")

    # Gate the rebuild on the evidence PASSING and the report existing, not on the key
    # being present: a failed build also populates evidence["geometry"] without writing
    # geometry_report.json, so this skipped the rebuild and then raised a bare
    # FileNotFoundError — HTTP 500 where the route intended 422 (S5-H5a).
    geo_dir = os.path.join(_cand_dir(sid, key), "geometry")
    report_path = os.path.join(geo_dir, "geometry_report.json")
    if not record["evidence"].get("geometry", {}).get("passed") \
            or not os.path.exists(report_path):
        build(sid, key)
        record = _load_candidate(sid, key)

    with open(report_path) as f:
        report = GeometryReport.model_validate(json.load(f))
    derived = _candidate_derived(_plan_derived(sid), record)

    outdir = os.path.join(_cand_dir(sid, key), "fast_aero")
    try:
        fast = run_fast_aero(report, derived, outdir)
    except FastAeroRejected as e:
        record["evidence"]["fast_aero"] = {
            "passed": False, "reason": str(e),
            "gates": (e.result or {}).get("gates", [])}
        # Symmetric with GeometryRejected, which set a status while this left it
        # untouched: a screen-rejected candidate stayed 'built' and looked healthy
        # (S5-H4). A distinct status keeps "which tier rejected me" machine-readable.
        _transition(record, "screen_failed", "screen")
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
    hybrid_unavailable_reason = ""
    if cd_ind is None:
        hybrid_unavailable_reason = "VSPAERO reported no induced-drag component"
    elif cd_ind <= 0.0:
        # `if cd_ind:` was falsy for 0.0 (deltas became None with no explanation) and
        # TRUTHY for a negative, which wrote ld_hybrid 110.97 and a NEGATIVE Oswald e
        # into evidence unguarded (S5-H6).
        hybrid_unavailable_reason = (
            f"VSPAERO induced drag came back non-physical ({cd_ind:.6g} <= 0), so the "
            f"hybrid rescore was not computed")
    else:
        cd_total = panel["cd0"] + cd_ind
        hybrid = {
            "cd0_equation": panel["cd0"],
            "cd0_vlm_estimate": fast.get("cdo_at_trim"),
            "cd_induced_vlm": cd_ind,
            "cd_total_hybrid": cd_total,
            "ld_hybrid": cl / cd_total,
            "oswald_e_measured": cl ** 2 / (math.pi * ar * cd_ind),
        }
    gates = fast.get("gates", [])
    record["evidence"]["fast_aero"] = {
        "passed": _gates_passed(gates),
        "gates_evaluated": len(gates),
        "reason": "" if gates else "no gates evaluated",
        "alpha_trim_deg": fast.get("alpha_trim_deg"),
        "cl_alpha_per_deg": fast.get("cl_alpha_per_deg"),
        "x_np_m": fast.get("x_np_m"),
        "ld_at_trim_vlm": fast.get("ld_at_trim"),
        "gates": gates,
        "hybrid": hybrid,
        "hybrid_unavailable_reason": hybrid_unavailable_reason,
        "deltas_vs_equation": {
            "ld": (hybrid.get("ld_hybrid") - panel["ld_cruise"]) if hybrid else None,
            "oswald_e": (hybrid.get("oswald_e_measured") - panel["oswald_e"]) if hybrid else None,
            "x_np_m": (fast.get("x_np_m") - panel["x_np_m"])
                      if fast.get("x_np_m") is not None else None,
        },
    }
    _transition(record,
                "screened" if record["evidence"]["fast_aero"]["passed"]
                else "screen_failed",
                "screen")
    _save_candidate(sid, record)
    return record


def promote(sid: str, key: str, note: str = "",
            override_violations: bool = False) -> Dict[str, Any]:
    """Adopt a candidate onto the main geometry line (new geometry version).
    Downstream mesh/CFD invalidation is handled by record_version as usual.

    A candidate that breaks one of the user's own hard caps is not silently
    promotable (S4-C4): the caller must pass `override_violations` AND a note, which
    is recorded on the geometry version so the decision is auditable rather than
    accidental."""
    record = _load_candidate(sid, key)
    if record.get("status") == "discarded":
        # it became 'promoted' while KEEPING discard_reason — a record asserting two
        # contradictory things about itself (S5-H3)
        raise CandidateError(
            "this candidate is discarded — restore it before promoting")
    violations = record.get("violations") or []
    if violations and not override_violations:
        raise CandidateError(
            "this concept does not meet your requirements: "
            + "; ".join(v["message"] for v in violations)
            + " Promote it only with an explicit override and a reason.")
    params = BWBParams.model_validate(record["params"])

    # Build into a STAGING directory (S5-H7). ds.new_version_dir creates geometry/v{n}
    # on disk immediately, and a failed promote never calls record_version — so the
    # next promote got the same v1 path, still holding the failed candidate's files,
    # and record_version's artifact inventory hashed them into a manifest attesting to
    # a DIFFERENT candidate. That is an evidence-integrity bug, not a tidiness one.
    staging = os.path.join(_cand_dir(sid, key), "promote_staging")
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging, exist_ok=True)
    try:
        report = _run_builder(record, params, staging, sid, "promote")
        if not report.all_gates_pass:
            # record_version marked the stage VALID even with failing gates, because
            # build_geometry only raises on a non-zero exit code (S5-H6c).
            failed = "; ".join(g.name for g in report.gates if not g.passed)
            record["evidence"]["geometry"] = {
                "passed": False, "reason": f"gates failed: {failed}",
                "gates": [g.model_dump() for g in report.gates]}
            _transition(record, "failed", "promote")
            _save_candidate(sid, record)
            raise CandidateError(f"promotion build failed its gates: {failed}")
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    with ds.session_lock(sid):
        vdir = ds.new_version_dir(sid, "geometry")
        for name in os.listdir(staging):
            os.replace(os.path.join(staging, name), os.path.join(vdir, name))
        shutil.rmtree(staging, ignore_errors=True)
        airfoil_sections = getattr(report, "airfoil_sections", None) or [
            {
                "station": station,
                "name": name,
                "sha256": record["airfoil_coordinate_hashes"][station],
                "openvsp_type": "XS_FILE_AIRFOIL",
            }
            for station, name in record["airfoil_schedule"].items()
        ]
        version = ds.record_version(sid, "geometry", {
            "source": "candidate",
            "candidate_key": key,
            "candidate_title": record["title"],
            "note": note or f"promoted candidate '{record['title']}'",
            "passed": report.all_gates_pass,
            "violations_overridden": violations,
            "selection_version": record["selection_version"],
            "selection_hash": record["selection_hash"],
            "schedule_hash": record["schedule_hash"],
            "airfoil_sections": [entry.model_dump() if hasattr(entry, "model_dump")
                                  else entry for entry in airfoil_sections],
        })

    _transition(record, "promoted", "promote")
    # Append-only: re-promoting used to overwrite promoted_geometry_version (v2 -> v3)
    # and lose the first reference entirely (S5-H3c).
    versions = record.get("promoted_geometry_versions") or []
    if record.get("promoted_geometry_version") and \
            record["promoted_geometry_version"] not in versions:
        versions.append(record["promoted_geometry_version"])
    versions.append(version)
    record["promoted_geometry_versions"] = versions
    record["promoted_geometry_version"] = version      # newest, for older readers
    record["evidence"]["geometry"] = {
        "passed": report.all_gates_pass,
        "gates": [g.model_dump() for g in report.gates],
    }
    _save_candidate(sid, record)
    return {"record": record, "geometry_version": version,
            "report": json.loads(report.model_dump_json())}


def discard(sid: str, key: str, reason: str = "") -> Dict[str, Any]:
    """Set a candidate aside.

    Refused for a promoted candidate: the session's geometry stage is sourced from it
    and stays valid, so discarding would leave the live manifest naming a discarded
    design as its origin (S5-H3a)."""
    record = _load_candidate(sid, key)
    if record.get("status") == "promoted":
        raise CandidateError(
            f"'{record['title']}' is the promoted design (geometry v"
            f"{record.get('promoted_geometry_version')}) — promote another candidate "
            f"instead of discarding this one")
    _transition(record, "discarded", "discard")
    if reason:
        record["discard_reason"] = reason
    _save_candidate(sid, record)
    return record


def restore(sid: str, key: str) -> Dict[str, Any]:
    """Undo a discard. Explicit, and it clears the reason — a restored candidate must
    not keep claiming why it was thrown away (S5-H3b)."""
    record = _load_candidate(sid, key)
    if record.get("status") != "discarded":
        raise CandidateError("this candidate is not discarded")
    _transition(record, "candidate", "restore")
    record.pop("discard_reason", None)
    _save_candidate(sid, record)
    return record


def stl_path(sid: str, key: str) -> str:
    """Raises when the file is absent, so the MCP and cockpit callers behave alike —
    the cockpit checked existence, the MCP path returned a path to nothing (S5-H8)."""
    path = os.path.join(_cand_dir(sid, key), "geometry", "bwb.stl")
    if not os.path.exists(path):
        raise CandidateError(f"candidate '{key}' has no built STL yet")
    return path


# ---------------------------------------------------------------------------
# MDO winners (Slice 2): write optimiser results into the same candidate batch
# ---------------------------------------------------------------------------

def add_mdo_candidates(sid: str, winners: List[Dict[str, Any]], *,
                       approval: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Write differential-evolution winners as tier-0 candidates (source='mdo').

    Each winner dict carries `params` (a BWBParams dump) and `mdo` (the score
    breakdown, for evidence). We attach the analytic panel via `estimate()` so the
    records are indistinguishable in shape from lens candidates — build / screen /
    promote all work unchanged, and the VSPAERO confirmation path is untouched.

    Re-running replaces any previous `mdo_*` records for the current plan; lens
    candidates in the batch are preserved. No OpenVSP here: geometry is built later,
    on demand, exactly as for lens candidates."""
    approval = _current_approval(sid, approval)
    derived = _plan_derived(sid)
    idx_path = _index_path(sid)

    existing_keys: List[str] = []
    if os.path.exists(idx_path):
        with open(idx_path) as f:
            existing_keys = json.load(f).get("keys", [])
    kept = [k for k in existing_keys if not k.startswith("mdo_")]

    # Orphaned directories from the previous MDO run: generate(regenerate=True) drops
    # mdo_* keys from the index but leaves their folders on disk, so a later run could
    # find stale records under keys it thinks are fresh (S5-H10).
    for stale_key in [k for k in existing_keys if k.startswith("mdo_")]:
        shutil.rmtree(_cand_dir(sid, stale_key), ignore_errors=True)

    new_keys: List[str] = []
    for position, w in enumerate(winners, start=1):
        raw_params = dict(w["params"])
        for computed in ("root_thickness_pct", "mid_thickness_pct",
                         "tip_thickness_pct"):
            raw_params.pop(computed, None)
        raw_params.update({
            "root_airfoil": approval["schedule"]["root"],
            "mid_airfoil": approval["schedule"]["mid"],
            "tip_airfoil": approval["schedule"]["tip"],
        })
        params = BWBParams.model_validate(raw_params)
        panel = estimate(params, derived.atmosphere,
                         derived.mission.cruise_speed_ms, derived.weight_n,
                         derived.mission.launch)
        # `rank` comes from the optimiser and was used verbatim as the key: three
        # winners all at rank 1 produced ONE record on disk and three identical index
        # entries, so list_candidates returned the same design three times (S5-H10).
        rank = int(w.get("rank", position))
        key = f"mdo_{position}"
        score = w.get("score", w.get("mdo", {}).get("score"))
        if score is None:
            # `f"{None:.3f}"` raised TypeError and lost the whole batch
            score_text = "score unavailable"
        else:
            score_text = f"score {float(score):.3f} vs base"
        record = {
            "key": key,
            "title": f"MDO winner #{rank}",
            "objective": "Maximise the MDO score: L/D minus stability, trim, and "
                         "geometry penalties (structures dormant).",
            "is_baseline": False,
            "source": "mdo",
            "status": "candidate",
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "params": params.model_dump(),
            "panel": panel.model_dump(),
            "provenance": [
                f"Found by differential-evolution MDO search (rank {rank}, "
                f"{score_text}).",
                "Fast tier: AeroSandbox VLM + NeuralFoil; confirm with VSPAERO then CFD.",
            ],
            "constraint_notes": [],
            "tradeoffs": [],
            "evidence": {"equation": {"passed": True}, "mdo": w.get("mdo", {})},
            "artifacts": {},
            "selection_version": approval["version"],
            "selection_hash": approval["selection_hash"],
            "schedule_hash": approval["schedule_hash"],
            "airfoil_schedule": dict(approval["schedule"]),
            "airfoil_coordinate_hashes": dict(approval["coordinate_hashes"]),
        }
        _save_candidate(sid, record)
        new_keys.append(key)

    ds.atomic_write_json(idx_path, {"plan_version": _plan_version(sid),
                                    "requirements_version":
                                        ds._latest_requirements_version(sid),
                                    "intake_version":
                                        approval["sources"]["intake_version"],
                                    "intake_hash":
                                        approval["sources"]["intake_hash"],
                                    "selection_version": approval["version"],
                                    "selection_hash": approval["selection_hash"],
                                    "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
                                    "keys": kept + new_keys})
    return list_candidates(sid)
