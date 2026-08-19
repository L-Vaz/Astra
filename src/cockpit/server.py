"""Aero Design Cockpit — browser workspace over the design-session store.

Serves a three.js frontend plus a JSON API from the FoamAgent conda env in
WSL. The MCP tools and this server share services/design_session.py as the
single source of truth. Nothing here is LLM-backed: parameter validation,
OpenVSP rebuilds, gates, and VSPAERO all run deterministically, and every
applied change is queued as a plain-language explanation for the chat.

Run:  python -m src.cockpit.server --port 8765
"""
import argparse
import json
import math
import os
import queue
import re
import sys
import threading
import time
from typing import Any, Dict, List, Optional

_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_ROOT = os.path.dirname(_SRC)
for p in (_SRC, _REPO_ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel, Field, ValidationError  # noqa: E402

from domain_packs.bwb.atmosphere import (  # noqa: E402
    atmosphere_at, atmosphere_provenance, atmosphere_table_payload)
from domain_packs.bwb.defaults import PAYLOAD_FRACTION, derive  # noqa: E402
from domain_packs.bwb.aero_estimates import (  # noqa: E402
    LAUNCH_VSTALL_MAX, estimate as aero_estimate)
from domain_packs.bwb.derivation import derive_graph  # noqa: E402
from domain_packs.bwb.inverse import (  # noqa: E402
    MET_TOLERANCES, check_requirements, solve_requirements)
from domain_packs.bwb.requirements import DesignRequirements  # noqa: E402
from domain_packs.bwb.schema import (  # noqa: E402
    BWBParams, GeometryReport, MissionDerived, MissionSpec)
from domain_packs.bwb.sensitivity import (  # noqa: E402
    requirement_sensitivity_table, sensitivity_table)
from domain_packs.bwb.spanwise import curve_payload  # noqa: E402
from domain_packs.bwb.handoff import SCHEMA_VERSION, build_handoff  # noqa: E402
from domain_packs.bwb.inverse import LockedValues  # noqa: E402
from domain_packs.bwb.airfoils import has_section  # noqa: E402
from services import candidates as cand_svc  # noqa: E402
from services import design_session as ds  # noqa: E402
from services.geometry import GeometryRejected, build_geometry as build_geometry_svc  # noqa: E402
from services.intent import build_enriched_requirement  # noqa: E402
from services.vspaero import FastAeroRejected, run_fast_aero as fast_aero_svc  # noqa: E402
from services.asb_aero import AsbAeroRejected, score_geometry as asb_score_svc  # noqa: E402
from services.optimize import optimize as optimize_svc  # noqa: E402
from domain_packs.bwb.design_space import DesignSpace  # noqa: E402
from domain_packs.bwb import objective_presets as presets  # noqa: E402
from domain_packs.bwb import intake as intake_pack  # noqa: E402
from cockpit.airfoil_router import router as airfoil_router  # noqa: E402

app = FastAPI(title="Astra Aero Design Cockpit")
app.include_router(airfoil_router)


@app.exception_handler(ds.SessionBusy)
def _session_busy_handler(request, exc: ds.SessionBusy) -> JSONResponse:
    """A contended session is a retry, not a server fault (S1-B2).

    The per-session lock has a timeout precisely so a stuck writer surfaces as a clear
    message instead of a hung request."""
    return JSONResponse(status_code=503, content={"detail": str(exc)},
                        headers={"Retry-After": "1"})


@app.exception_handler(ds.SessionIdRejected)
def _session_id_rejected_handler(request, exc: ds.SessionIdRejected) -> JSONResponse:
    """The store's containment backstop answers 400, not 500 (S1-A1)."""
    return JSONResponse(status_code=400, content={"detail": str(exc)})

DEFAULT_MISSION = dict(payload_kg=1.5, cruise_speed_ms=25.0, cruise_altitude_m=100.0,
                       launch="hand")

# Map pydantic cross-check messages to the control that owns the problem, so
# gate failures can highlight the affected slider (spec §12).
_CROSSCHECK_OWNERS = [
    ("tip chord", ["taper_outer", "mid_chord_frac", "root_chord_m"]),
    ("tip airfoil thicker", ["tip_thickness_pct", "root_thickness_pct"]),
    ("aspect ratio", ["span_m", "root_chord_m"]),
    ("wash-in", ["tip_twist_deg", "root_twist_deg"]),
]


# ---------------------------------------------------------------------------
# session helpers
# ---------------------------------------------------------------------------

# A session id is exactly what create_session mints: uuid4().hex[:8]
# (design_session.py). Anything else is a client that made one up, and before this it
# was joined straight into a filesystem path — probe-confirmed: "../escaped",
# "/tmp/astra_probe_outside" and "aa/bb" all returned 200 and created stage directories
# outside the sessions root (S1-A1).
_SESSION_ID_RE = re.compile(r"[0-9a-f]{8}\Z")


def _validated_sid(sid: str) -> str:
    if not _SESSION_ID_RE.fullmatch(sid or ""):
        raise HTTPException(400, "session_id must be 8 lowercase hex characters")
    return sid


def _session_path(sid: str, *parts: str) -> str:
    # ds.session_dir re-checks containment, so a route that forgets _validated_sid
    # still cannot escape the sessions root.
    return os.path.join(ds.session_dir(_validated_sid(sid)), *parts)


def _list_sessions() -> List[Dict[str, Any]]:
    root = ds.sessions_root()
    if not os.path.isdir(root):
        return []
    out = []
    for sid in os.listdir(root):
        sfile = os.path.join(root, sid, "session.json")
        if os.path.exists(sfile):
            with open(sfile) as f:
                state = json.load(f)
            out.append({"session_id": sid, "label": state.get("label", ""),
                        "created": state.get("created", ""),
                        "mtime": os.path.getmtime(sfile)})
    return sorted(out, key=lambda s: s["mtime"], reverse=True)


def _write_plan_version(sid: str, derived: MissionDerived, note: str,
                        invalidate_from: Optional[str] = None) -> int:
    """Persist a mission as a new plan version, or reuse the identical current one.

    The lock spans allocate-directory → write artifacts → record: `new_version_dir`
    and `record_version` each derive the number from the stored state, so without it
    two concurrent generates write their artifacts into the SAME vN and one mission is
    lost (S1-B2).

    Idempotent (S1-D8): a mission byte-identical to the current plan reuses that
    version. Nothing downstream is invalidated in that case, which is correct — the
    flight condition every downstream artifact was built from has not changed.
    """
    payload = derived.model_dump_json(indent=2)
    with ds.session_lock(sid):
        current = ds.latest_dir(sid, "plan")
        if current:
            existing = os.path.join(current, "mission_derived.json")
            if os.path.exists(existing):
                with open(existing) as f:
                    if f.read() == payload:
                        return ds.get_state(sid)["stages"]["plan"]["latest"]

        vdir = ds.new_version_dir(sid, "plan")
        ds.atomic_write(os.path.join(vdir, "mission_derived.json"), payload)
        ds.atomic_write(os.path.join(vdir, "enriched_requirement.txt"),
                        build_enriched_requirement(derived))
        return ds.record_version(sid, "plan", {
                                     "prompt": note,
                                     "source": "cockpit",
                                     **atmosphere_provenance(derived.atmosphere),
                                 },
                                 invalidate_from=invalidate_from)


def _ensure_session(sid: Optional[str]) -> str:
    """Resolve a usable session for a READ: the given one, the most recent, or a fresh
    deterministic demo session (default mission, no LLM involved).

    The fall-back to "most recent" is a convenience for opening the cockpit with no id
    in the URL. It is defensible for a GET and indefensible for a mutating POST — see
    _require_session (S1-A2)."""
    if sid:
        try:
            ds.get_state(_validated_sid(sid))
            return sid
        except FileNotFoundError:
            raise HTTPException(404, f"unknown design session: {sid}")
    sessions = _list_sessions()
    if sessions:
        return sessions[0]["session_id"]
    new_sid = ds.create_session(label="cockpit demo session")
    derived = derive(MissionSpec(**DEFAULT_MISSION))
    _write_plan_version(new_sid, derived, "cockpit default mission")
    return new_sid


def _require_session(sid: Optional[str]) -> str:
    """Resolve a session for a WRITE. No id means no write (S1-A2).

    `_ensure_session(None)` returns the most recently modified session in the whole
    store. On a mutating POST that means an unauthenticated localhost request with no
    session_id at all — which any web page the user visits can send — silently edits
    whatever session they were last working on. Probe-confirmed: POST
    /api/stage1/intake with the field omitted, and with "", both mutated the newest
    session and returned 200."""
    if not sid:
        raise HTTPException(400, "session_id is required for this request")
    try:
        ds.get_state(_validated_sid(sid))
    except FileNotFoundError:
        raise HTTPException(404, f"unknown design session: {sid}")
    return sid


def _plan_derived(sid: str) -> Optional[MissionDerived]:
    d = ds.latest_dir(sid, "plan")
    if not d:
        return None
    path = os.path.join(d, "mission_derived.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return MissionDerived.model_validate_json(f.read())


def _geometry_report(sid: str, version: Optional[int] = None) -> Optional[GeometryReport]:
    if version is not None:
        d = _session_path(sid, "geometry", f"v{version}")
    else:
        d = ds.latest_dir(sid, "geometry")
    if not d:
        return None
    path = os.path.join(d, "geometry_report.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        raw = json.load(f)
    try:
        return GeometryReport.model_validate(raw)
    except ValidationError:
        return None  # rejected build wrote a partial report


def _current_params(sid: str) -> Optional[Dict[str, Any]]:
    """The current geometry, ALWAYS normalised through BWBParams (S1-C).

    The stored `params.json` was previously returned raw. That was invisible until
    the section became the owner of thickness: a file written before S1-C has
    `root_thickness_pct` and no aerofoil at all, so the cockpit rendered "—" for
    every section name and a blank blend row while both test suites were green.
    Round-tripping through the model applies the legacy migration (nearest section
    by t/c) and guarantees the browser always receives the CURRENT schema, not
    whatever shape happened to be on disk."""
    geo = ds.latest_dir(sid, "geometry")
    if geo and os.path.exists(os.path.join(geo, "params.json")):
        with open(os.path.join(geo, "params.json")) as f:
            stored = json.load(f)
        try:
            return BWBParams.model_validate(stored).model_dump()
        except ValidationError:
            # A stored geometry outside the current gates must not brick the screen;
            # the geometry report surfaces the violation separately.
            return stored
    plan = _plan_derived(sid)
    return plan.params.model_dump() if plan else None


def _versions(sid: str) -> Dict[str, List[Dict[str, Any]]]:
    """All recorded versions per stage (manifest contents), plus validity."""
    state = ds.get_state(sid)
    out: Dict[str, List[Dict[str, Any]]] = {}
    for stage in ds.STAGES:
        entries = []
        stage_dir = _session_path(sid, stage)
        latest_valid = state["stages"][stage]["latest"] if state["stages"][stage]["valid"] else 0
        if os.path.isdir(stage_dir):
            for name in sorted(os.listdir(stage_dir),
                               key=lambda n: int(n[1:]) if n[1:].isdigit() else 0):
                mpath = os.path.join(stage_dir, name, "manifest.json")
                entry: Dict[str, Any] = {"version": int(name[1:]) if n_ok(name) else 0,
                                         "recorded": os.path.exists(mpath)}
                if os.path.exists(mpath):
                    with open(mpath) as f:
                        entry.update(json.load(f))
                entry["is_latest_valid"] = entry["version"] == latest_valid
                entries.append(entry)
        out[stage] = entries
    return out


def n_ok(name: str) -> bool:
    return name.startswith("v") and name[1:].isdigit()


def _locks_path(sid: str) -> str:
    return _session_path(sid, "cockpit", "locks.json")


def _read_locks(sid: str) -> Dict[str, bool]:
    path = _locks_path(sid)
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def _queue_explanation(sid: str, kind: str, summary: str, details: Dict[str, Any]) -> None:
    """Append a pending plain-language explanation for the chat to deliver
    (spec decision 3: cockpit applies deterministically, chat narrates later)."""
    path = _session_path(sid, "cockpit", "explanations.jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps({
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": kind,
            "summary": summary, "details": details, "delivered": False}) + "\n")


def _read_explanations(sid: str) -> List[Dict[str, Any]]:
    path = _session_path(sid, "cockpit", "explanations.jsonl")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


# ---------------------------------------------------------------------------
# deterministic instant-preview math (mirrors BWBParams analytics, but works
# on raw dicts so out-of-range candidates still get numbers + warnings)
# ---------------------------------------------------------------------------

def _planform(raw: Dict[str, Any]) -> Optional[Dict[str, float]]:
    try:
        span = float(raw["span_m"]); cr = float(raw["root_chord_m"])
        cbf = float(raw["centre_body_fraction"]); mf = float(raw["mid_chord_frac"])
        tp = float(raw["taper_outer"])
    except (KeyError, TypeError, ValueError):
        return None
    if span <= 0 or cr <= 0:
        return None
    c_mid = cr * mf
    c_tip = c_mid * tp
    b1 = cbf * span / 2.0
    b2 = span / 2.0 - b1
    s = 2.0 * (b1 * (cr + c_mid) / 2.0 + b2 * (c_mid + c_tip) / 2.0)

    def trap_mac(a: float, b: float) -> float:
        return (2.0 / 3.0) * (a + b - a * b / (a + b))
    a1 = b1 * (cr + c_mid) / 2.0
    a2 = b2 * (c_mid + c_tip) / 2.0
    mac = (a1 * trap_mac(cr, c_mid) + a2 * trap_mac(c_mid, c_tip)) / (a1 + a2)
    return {"s_ref_m2": s, "mac_m": mac, "aspect_ratio": span ** 2 / s,
            "tip_chord_m": c_tip, "mid_chord_m": c_mid, "b1_m": b1, "b2_m": b2}


def _instant_derived(mission: Dict[str, Any], raw_params: Dict[str, Any]) -> Dict[str, Any]:
    """Every value here is a mathematical consequence, never a prediction."""
    plan = _planform(raw_params) or {}
    atmosphere = atmosphere_at(float(mission.get("cruise_altitude_m", 100.0)))
    rho = atmosphere.density_kgm3
    v = float(mission.get("cruise_speed_ms", 25.0))
    payload = float(mission.get("payload_kg", 1.0))
    mtow = mission.get("mtow_kg") or payload / PAYLOAD_FRACTION
    q = 0.5 * rho * v ** 2
    w = mtow * 9.81
    out = {"evidence": "instant", "atmosphere": atmosphere.model_dump(),
           "air_temperature_k": atmosphere.temperature_k,
           "air_pressure_pa": atmosphere.pressure_pa,
           "air_density_kgm3": rho,
           "dynamic_viscosity_kgms": atmosphere.dynamic_viscosity_kgms,
           "kinematic_viscosity_m2s": atmosphere.kinematic_viscosity_m2s,
           "dynamic_pressure_pa": q,
           "mtow_kg": mtow, "weight_n": w,
           "mtow_assumed": mission.get("mtow_kg") in (None, "", 0)}
    if plan:
        out.update(plan)
        out["required_cl"] = w / (q * plan["s_ref_m2"])
        out["reynolds_mac"] = (
            v * plan["mac_m"] / atmosphere.kinematic_viscosity_m2s)
        out["wing_loading_kgm2"] = mtow / plan["s_ref_m2"]
    return out


def _validate_params(raw: Dict[str, Any]) -> List[Dict[str, Any]]:
    """-> list of {param(s), message} issues; empty when valid."""
    try:
        BWBParams.model_validate(raw)
        return []
    except ValidationError as e:
        issues = []
        for err in e.errors():
            msg = err["msg"].removeprefix("Value error, ")
            if err["loc"]:
                issues.append({"params": [str(err["loc"][0])], "message": msg})
            else:
                owners = next((o for key, o in _CROSSCHECK_OWNERS if key in msg), [])
                issues.append({"params": owners, "message": msg})
        return issues


def _diff_params(old: Dict[str, Any], new: Dict[str, Any]) -> List[str]:
    changes = []
    for k in new:
        ov, nv = old.get(k), new.get(k)
        if ov is not None and nv is not None and abs(float(ov) - float(nv)) > 1e-9:
            changes.append(f"{k}: {float(ov):.4g} -> {float(nv):.4g}")
    return changes


# ---------------------------------------------------------------------------
# requirements — first-class store + staleness vs the built geometry
# ---------------------------------------------------------------------------

_REQ_FIELDS = set(DesignRequirements.model_fields)


def _current_requirements(sid: str) -> DesignRequirements:
    """Latest saved requirements, or a synthesis from the plan for sessions
    predating the requirements store (targets all None)."""
    saved = ds.latest_requirements(sid)
    if saved:
        try:
            return DesignRequirements.model_validate(
                {k: v for k, v in saved.items() if k in _REQ_FIELDS})
        except ValidationError:
            # a poisoned payload (out-of-range value saved by an older writer)
            # must not brick the session — fall through to plan synthesis
            pass
    plan = _plan_derived(sid)
    if plan:
        return DesignRequirements(
            mtow_kg=plan.mtow_kg,
            cruise_speed_ms=plan.mission.cruise_speed_ms,
            cruise_altitude_m=plan.mission.cruise_altitude_m,
            launch=plan.mission.launch)
    m = DEFAULT_MISSION
    return DesignRequirements(
        mtow_kg=m["payload_kg"] / PAYLOAD_FRACTION,
        cruise_speed_ms=m["cruise_speed_ms"],
        cruise_altitude_m=m["cruise_altitude_m"], launch=m["launch"])


def _requirements_version(sid: str) -> int:
    saved = ds.latest_requirements(sid)
    return saved.get("version", 0) if saved else 0


def _requirements_status_for_params(sid: str, params: BWBParams) -> Dict[str, Any]:
    """Requirement statuses of one specific parameter set (no staleness logic)."""
    statuses = check_requirements(_current_requirements(sid), params)
    return {"statuses": [s.model_dump() for s in statuses],
            "unmet": [s.name for s in statuses if s.met is False]}


def _requirements_status(sid: str, req: Optional[DesignRequirements] = None
                         ) -> Dict[str, Any]:
    """How the latest BUILT geometry measures against the requirements.
    Never invalidates anything — staleness is surfaced, not enforced."""
    req = req or _current_requirements(sid)
    params_raw = _current_params(sid)
    geo_valid = ds.get_state(sid)["stages"]["geometry"]["valid"]
    out: Dict[str, Any] = {"stale": False, "statuses": [], "unmet": [],
                           "checked_against": "built geometry" if geo_valid else "plan baseline"}
    if not params_raw:
        return out
    try:
        params = BWBParams.model_validate(params_raw)
    except ValidationError:
        return out
    statuses = check_requirements(req, params)
    out["statuses"] = [s.model_dump() for s in statuses]
    out["unmet"] = [s.name for s in statuses if s.met is False]
    out["stale"] = bool(out["unmet"]) and geo_valid
    return out


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

_SECTION_FIELDS = ("root_airfoil", "mid_airfoil", "tip_airfoil")


def _section_curves_for(params: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Reynolds curves for whichever sections this geometry actually uses (S1-C2)."""
    if not params:
        return {}
    names = [params.get(f) for f in _SECTION_FIELDS]
    return curve_payload(*[n for n in names if isinstance(n, str) and has_section(n)])


@app.get("/api/health")
def health() -> Dict[str, Any]:
    return {"ok": True, "service": "astra-cockpit"}


@app.get("/api/sections/curves")
def section_curves(names: str) -> Dict[str, Any]:
    """Top-up route for sections the client does not yet hold (S1-C2).

    Bootstrap ships the current three. A revision or a promoted candidate can change
    them, and the browser cannot compute a stall speed for a section it has never
    seen. An unknown name is REPORTED, not skipped: silently returning a short map
    would leave the client waiting forever for a curve that is never coming."""
    wanted = [n.strip() for n in names.split(",") if n.strip()]
    if not wanted:
        raise HTTPException(status_code=400, detail="names must be a non-empty list")
    if len(wanted) > 16:
        raise HTTPException(status_code=400, detail="at most 16 sections per request")
    unknown = [n for n in wanted if not has_section(n)]
    if unknown:
        raise HTTPException(status_code=404,
                            detail=f"unknown section(s): {', '.join(sorted(unknown))}")
    return {"curves": curve_payload(*wanted)}


@app.get("/api/bootstrap")
def bootstrap(session: Optional[str] = None) -> Dict[str, Any]:
    sid = _ensure_session(session)
    plan = _plan_derived(sid)
    params = _current_params(sid)
    mission = plan.mission.model_dump() if plan else dict(DEFAULT_MISSION)
    geo = _geometry_report(sid)
    state = ds.get_state(sid)

    fast = None
    fa_dir = ds.latest_dir(sid, "fast_aero")
    if fa_dir and os.path.exists(os.path.join(fa_dir, "fast_aero.json")):
        with open(os.path.join(fa_dir, "fast_aero.json")) as f:
            fast = json.load(f)

    return {
        "session_id": sid,
        "label": state.get("label", ""),
        "sessions": _list_sessions()[:12],
        "param_schema": BWBParams.model_json_schema(),
        "mission_schema": MissionSpec.model_json_schema(),
        "atmosphere_table": atmosphere_table_payload(),
        "mission": mission,
        "params": params,
        # S1-C2: the browser runs Schrenk itself so that dragging stays instant, which
        # needs section CLmax(Re) for the three selected sections. The full table is
        # 2.33 MB; this is 30 numbers. Shipped at bootstrap so the FIRST paint already
        # has a real stall speed rather than a blank waiting on a second round trip.
        "section_curves": _section_curves_for(params),
        "derived": _instant_derived(mission, params) if params else None,
        "plan_assumptions": plan.assumptions if plan else [],
        "sizing_targets": {
            "s_ref_target_m2": plan.s_ref_target_m2,
            "required_cl": plan.required_cl,
            "static_margin_target": plan.static_margin_target,
        } if plan else None,
        "geometry": {
            "version": state["stages"]["geometry"]["latest"],
            "valid": state["stages"]["geometry"]["valid"],
            "report": json.loads(geo.model_dump_json()) if geo else None,
        },
        "fast_aero": fast,
        "versions": _versions(sid),
        "locks": _read_locks(sid),
        "pending_explanations": sum(1 for e in _read_explanations(sid) if not e["delivered"]),
        "requirements": _current_requirements(sid).model_dump(),
        "requirements_version": _requirements_version(sid),
        "requirements_status": _requirements_status(sid),
        "requirements_schema": DesignRequirements.model_json_schema(),
        # solver-owned numbers the req panel must not duplicate: met-tolerances
        # (inverse._statuses) and per-launch-method stall caps
        "requirements_meta": {
            "tolerances": MET_TOLERANCES,
            "launch_vstall_max": LAUNCH_VSTALL_MAX,
        },
    }


class PreviewRequest(BaseModel):
    mission: Dict[str, Any]
    params: Dict[str, Any]


@app.post("/api/preview")
def preview(req: PreviewRequest) -> Dict[str, Any]:
    """Authoritative server-side echo of the instant math + validation.
    The frontend mirrors this locally for zero-latency; this endpoint is the
    tie-breaker and what Apply uses as its final pre-flight check."""
    return {"derived": _instant_derived(req.mission, req.params),
            "issues": _validate_params(req.params)}


class RequirementsRequest(BaseModel):
    session_id: str
    requirements: Dict[str, Any]
    keep_shape: bool = Field(default=True, description="Hold the current design's "
                             "shape fractions so the change morphs, not resets")
    persist: bool = Field(default=True, description="False = pure preview while "
                          "dragging; nothing is written")


@app.post("/api/requirements")
def set_requirements(req: RequirementsRequest) -> Dict[str, Any]:
    """The heart of the explorer: requirements in, solved geometry out.
    Deterministic closed-form inverse sizing — NEVER builds OpenVSP; /api/revision
    remains the one and only Apply."""
    merged = _current_requirements(req.session_id).model_dump()
    merged.update({k: v for k, v in req.requirements.items() if k in _REQ_FIELDS})
    try:
        requirements = DesignRequirements.model_validate(merged)
    except ValidationError as e:
        raise HTTPException(422, detail=[
            {"params": [str(err["loc"][0])] if err["loc"] else [],
             "message": err["msg"]} for err in e.errors()])

    shape_from = None
    if req.keep_shape:
        current = _current_params(req.session_id)
        if current:
            try:
                shape_from = BWBParams.model_validate(current)
            except ValidationError:
                shape_from = None
    result = solve_requirements(requirements, shape_from=shape_from)

    version = None
    if req.persist:
        version = ds.save_requirements(req.session_id, requirements.model_dump())
        unmet = [s.label for s in result.requirements if s.met is False]
        _queue_explanation(
            req.session_id, "requirements",
            f"Requirements v{version} set from cockpit: "
            + ", ".join(f"{k}={v}" for k, v in requirements.model_dump().items()
                        if v is not None) + ". "
            + (f"Solver result: all targets met."
               if not result.binding_constraint else
               f"Unmet: {', '.join(unmet)} — binding: {result.binding_explanation}"),
            {"requirements_version": version,
             "binding_constraint": result.binding_constraint})

    built = _current_params(req.session_id) or {}
    stale = any(abs(float(built.get(k, 0)) - float(v)) > 1e-9
                for k, v in result.params.model_dump().items()) if built else True
    return {
        "requirements_version": version,
        "requirements": requirements.model_dump(),
        "proposed_params": result.params.model_dump(),
        "panel": json.loads(result.panel.model_dump_json()),
        "statuses": [s.model_dump() for s in result.requirements],
        "binding_constraint": result.binding_constraint,
        "binding_explanation": result.binding_explanation,
        "notes": result.notes,
        "stale_vs_built_geometry": stale,
    }


class ExplainRequest(BaseModel):
    params: Dict[str, Any]
    flight: Dict[str, Any]     # mtow_kg, cruise_speed_ms, cruise_altitude_m, launch
    include: List[str] = Field(default=["derivation", "sensitivity"],
                               description="any of: derivation, sensitivity, req_sensitivity")


@app.post("/api/explain")
def explain(req: ExplainRequest) -> Dict[str, Any]:
    """First-principles drill-down: machine-readable equation tree + finite-
    difference sensitivities for ANY parameter set (stateless — also serves
    candidates and unsaved slider states). Microseconds server-side."""
    try:
        params = BWBParams.model_validate(req.params)
    except ValidationError as e:
        raise HTTPException(422, detail=[{"message": err["msg"]} for err in e.errors()])
    try:
        flight = DesignRequirements.model_validate(
            {k: v for k, v in req.flight.items() if k in _REQ_FIELDS})
    except ValidationError as e:
        raise HTTPException(422, detail=[{"message": err["msg"]} for err in e.errors()])

    atmosphere = atmosphere_at(flight.cruise_altitude_m)
    w = flight.mtow_kg * 9.81
    out: Dict[str, Any] = {}
    if "derivation" in req.include:
        graph = derive_graph(params, atmosphere, flight.cruise_speed_ms,
                             w, flight.launch)
        out["derivation"] = json.loads(graph.model_dump_json())
    if "sensitivity" in req.include:
        rows = sensitivity_table(params, atmosphere, flight.cruise_speed_ms,
                                 w, flight.launch)
        out["sensitivity"] = [r.model_dump() for r in rows]
    if "req_sensitivity" in req.include:
        rows = requirement_sensitivity_table(flight, params)
        out["req_sensitivity"] = [r.model_dump() for r in rows]
    return out


class MissionRequest(BaseModel):
    session_id: str
    mission: Dict[str, Any]


@app.post("/api/mission")
def save_mission(req: MissionRequest) -> Dict[str, Any]:
    try:
        mission = MissionSpec.model_validate(req.mission)
    except ValidationError as e:
        raise HTTPException(422, detail=[
            {"params": [str(err["loc"][0])] if err["loc"] else [],
             "message": err["msg"]} for err in e.errors()])
    derived = derive(mission)
    # Keep the cockpit's current geometry authoritative: the plan records the
    # mission + sizing targets, but does not overwrite edited geometry params.
    current = _current_params(req.session_id)
    if current:
        current_params = BWBParams.model_validate(current)
        derived = MissionDerived.model_validate({
            **derived.model_dump(),
            "params": current_params,
            "reynolds_mac": (
                mission.cruise_speed_ms * current_params.mean_aero_chord_m()
                / derived.atmosphere.kinematic_viscosity_m2s
            ),
        })
    # invalidate_from="fast_aero": a mission change alters the flight condition
    # (fast-aero/mesh/CFD genuinely stale) but NOT the already-built shape —
    # the geometry stays valid and staleness vs requirements is surfaced instead.
    version = _write_plan_version(req.session_id, derived, "cockpit mission update",
                                  invalidate_from="fast_aero")
    # keep the requirements' flight condition in sync so the two never disagree
    synced = _current_requirements(req.session_id).model_dump()
    synced.update({"mtow_kg": derived.mtow_kg,
                   "cruise_speed_ms": mission.cruise_speed_ms,
                   "cruise_altitude_m": mission.cruise_altitude_m,
                   "launch": mission.launch})
    ds.save_requirements(req.session_id, synced)
    _queue_explanation(
        req.session_id, "mission",
        f"Mission updated in cockpit (plan v{version}): payload {mission.payload_kg} kg, "
        f"{mission.cruise_speed_ms} m/s at {mission.cruise_altitude_m} m, "
        f"launch {mission.launch}. Built geometry kept (staleness is tracked "
        f"against requirements); fast-aero/mesh/CFD invalidated.",
        {"mission": mission.model_dump(), "plan_version": version})
    return {"plan_version": version,
            "assumptions": derived.assumptions,
            "sizing_targets": {
                "s_ref_target_m2": derived.s_ref_target_m2,
                "required_cl": derived.required_cl,
                "static_margin_target": derived.static_margin_target},
            "derived": _instant_derived(mission.model_dump(), current or {})}


class RevisionRequest(BaseModel):
    session_id: str
    params: Dict[str, Any]
    note: str = Field(default="", description="Why this revision was made")


@app.post("/api/revision")
def apply_revision(req: RevisionRequest) -> Dict[str, Any]:
    """Rebuild the authoritative OpenVSP geometry (deterministic, no LLM)."""
    issues = _validate_params(req.params)
    if issues:
        raise HTTPException(422, detail=issues)
    params = BWBParams.model_validate(req.params)
    old = _current_params(req.session_id) or {}

    vdir = ds.new_version_dir(req.session_id, "geometry")
    try:
        report = build_geometry_svc(params, vdir)
    except GeometryRejected as e:
        gates = (e.report or {}).get("gates", [])
        raise HTTPException(422, detail=[
            {"params": [], "message": str(e), "gates": gates}])

    req_status = _requirements_status_for_params(req.session_id, report.params)
    version = ds.record_version(req.session_id, "geometry", {
        "source": "cockpit", "note": req.note,
        "overrides": {k: req.params[k] for k in req.params
                      if abs(float(old.get(k, req.params[k])) - float(req.params[k])) > 1e-9},
        "passed": report.all_gates_pass,
        "requirements_version": _requirements_version(req.session_id),
        "requirements_unmet": req_status["unmet"],
    })

    changes = _diff_params(old, req.params)
    _queue_explanation(
        req.session_id, "revision",
        f"Geometry v{version} applied from cockpit"
        + (f" ({req.note})" if req.note else "") + ". "
        + ("Changed " + "; ".join(changes) + ". " if changes else "")
        + f"Result: S={report.s_ref_m2:.3f} m2, AR={report.aspect_ratio:.2f}, "
          f"MAC={report.mean_aero_chord_m:.3f} m, watertight={report.watertight}. "
        + (f"Requirements unmet: {', '.join(req_status['unmet'])}. "
           if req_status["unmet"] else "All set requirements met. ")
        + "Downstream fast-aero/mesh/CFD results are now invalidated.",
        {"version": version, "changes": changes, "note": req.note})

    return {
        "version": version,
        "passed": report.all_gates_pass,
        "gates": [g.model_dump() for g in report.gates],
        "summary": {"s_ref_m2": report.s_ref_m2, "aspect_ratio": report.aspect_ratio,
                    "mac_m": report.mean_aero_chord_m, "span_m": report.params.span_m,
                    "n_facets": report.n_facets, "watertight": report.watertight},
        "params": report.params.model_dump(),
        "stl_url": f"/api/stl/{req.session_id}/{version}",
        "warnings": report.warnings,
    }


@app.get("/api/stl/{session_id}/{version}")
def get_stl(session_id: str, version: int):
    path = _session_path(session_id, "geometry", f"v{version}", "bwb.stl")
    if not os.path.exists(path):
        raise HTTPException(404, f"no STL for geometry v{version}")
    return FileResponse(path, media_type="model/stl", filename=f"bwb_v{version}.stl")


@app.get("/api/params/{session_id}/{version}")
def get_version_params(session_id: str, version: int) -> Dict[str, Any]:
    path = _session_path(session_id, "geometry", f"v{version}", "params.json")
    if not os.path.exists(path):
        raise HTTPException(404, f"no params for geometry v{version}")
    with open(path) as f:
        return {"version": version, "params": json.load(f)}


class FastAeroReq(BaseModel):
    session_id: str


@app.post("/api/fast_aero")
def fast_aero(req: FastAeroReq) -> Dict[str, Any]:
    plan = _plan_derived(req.session_id)
    geo = _geometry_report(req.session_id)
    if not plan or not geo:
        raise HTTPException(409, "need a plan and a valid geometry before fast aero")
    vdir = ds.new_version_dir(req.session_id, "fast_aero")
    try:
        result = fast_aero_svc(geo, plan, vdir)
    except FastAeroRejected as e:
        raise HTTPException(422, detail=[{"params": [], "message": str(e),
                                          "gates": (e.result or {}).get("gates", [])}])
    version = ds.record_version(req.session_id, "fast_aero", {
        "source": "cockpit",
        "passed": True,
        **atmosphere_provenance(plan.atmosphere),
    })
    _queue_explanation(
        req.session_id, "fast_aero",
        f"VSPAERO fast validation v{version} from cockpit: trim alpha "
        f"{result['alpha_trim_deg']:.2f} deg, L/D {result['ld_at_trim']:.1f}, "
        f"CL_alpha {result['cl_alpha_per_deg']:.4f}/deg, x_np {result['x_np_m']:.3f} m.",
        {"version": version})
    result["version"] = version
    return result


# ---------------------------------------------------------------------------
# design candidates (tier-0 equations -> tier-1 VSPAERO -> promote to main line)
# ---------------------------------------------------------------------------

class CandidatesGenerate(BaseModel):
    session_id: str
    regenerate: bool = False


@app.post("/api/candidates/generate")
def candidates_generate(req: CandidatesGenerate) -> Dict[str, Any]:
    try:
        batch = cand_svc.generate(req.session_id, regenerate=req.regenerate)
    except cand_svc.CandidateError as e:
        raise HTTPException(409, str(e))
    _queue_explanation(
        req.session_id, "candidates",
        f"Design candidates generated (plan v{batch['plan_version']}): "
        + ", ".join(c["title"] for c in batch["candidates"]) + ".",
        {"plan_version": batch["plan_version"],
         "keys": [c["key"] for c in batch["candidates"]]})
    return batch


@app.exception_handler(cand_svc.UnknownSession)
def _unknown_session_handler(request, exc: cand_svc.UnknownSession) -> JSONResponse:
    """A bogus session id is a 404. Every candidate route used to leak the store's
    FileNotFoundError as a 500 (S5-H5c)."""
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.get("/api/candidates/{session_id}")
def candidates_list(session_id: str) -> Dict[str, Any]:
    return cand_svc.list_candidates(_validated_sid(session_id))


class CandidateAction(BaseModel):
    session_id: str
    key: str
    note: str = ""
    override_violations: bool = Field(
        default=False,
        description=("Promote a candidate that breaks one of your hard caps anyway "
                     "(S4-C4). Requires a `note` saying why; both are recorded on the "
                     "geometry version."))


@app.post("/api/candidates/build")
def candidates_build(req: CandidateAction) -> Dict[str, Any]:
    _require_session(req.session_id)
    try:
        with ds.session_lock(req.session_id):
            return cand_svc.build(req.session_id, req.key)
    except cand_svc.CandidateError as e:
        raise HTTPException(422, str(e))


@app.post("/api/candidates/screen")
def candidates_screen(req: CandidateAction) -> Dict[str, Any]:
    _require_session(req.session_id)
    try:
        with ds.session_lock(req.session_id):
            record = cand_svc.screen(req.session_id, req.key)
    except cand_svc.CandidateError as e:
        raise HTTPException(422, str(e))
    fa = record["evidence"].get("fast_aero", {})
    hybrid = fa.get("hybrid") or {}
    _queue_explanation(
        req.session_id, "candidate_screen",
        f"Candidate '{record['title']}' VSPAERO-screened: hybrid L/D "
        f"{hybrid.get('ld_hybrid', 0):.1f} (equation predicted "
        f"{record['panel']['ld_cruise']:.1f}), x_np {fa.get('x_np_m', 0):.3f} m.",
        {"key": req.key})
    return record


@app.post("/api/candidates/promote")
def candidates_promote(req: CandidateAction) -> Dict[str, Any]:
    _require_session(req.session_id)
    if req.override_violations and not req.note.strip():
        raise HTTPException(422, "overriding a violation requires a note saying why")
    try:
        with ds.session_lock(req.session_id):
            result = cand_svc.promote(req.session_id, req.key, note=req.note,
                                      override_violations=req.override_violations)
    except cand_svc.CandidateError as e:
        raise HTTPException(422, str(e))
    version = result["geometry_version"]
    _queue_explanation(
        req.session_id, "candidate_promote",
        f"Candidate '{result['record']['title']}' promoted to main geometry "
        f"v{version}. Downstream fast-aero/mesh/CFD are invalidated.",
        {"key": req.key, "geometry_version": version})
    result["stl_url"] = f"/api/stl/{req.session_id}/{version}"
    return result


@app.post("/api/candidates/discard")
def candidates_discard(req: CandidateAction) -> Dict[str, Any]:
    _require_session(req.session_id)
    try:
        with ds.session_lock(req.session_id):
            return cand_svc.discard(req.session_id, req.key, reason=req.note)
    except cand_svc.CandidateError as e:
        raise HTTPException(422, str(e))


@app.post("/api/candidates/restore")
def candidates_restore(req: CandidateAction) -> Dict[str, Any]:
    """Undo a discard (S5-H3b). Explicit, because promote-from-discarded is refused
    and an un-discarded record must not keep its discard reason."""
    _require_session(req.session_id)
    try:
        with ds.session_lock(req.session_id):
            return cand_svc.restore(req.session_id, req.key)
    except cand_svc.CandidateError as e:
        raise HTTPException(422, str(e))


@app.get("/api/candidates/stl/{session_id}/{key}")
def candidates_stl(session_id: str, key: str):
    try:
        path = cand_svc.stl_path(_validated_sid(session_id), key)
    except cand_svc.UnknownSession:
        raise
    except cand_svc.CandidateError as e:
        raise HTTPException(404, str(e))
    return FileResponse(path, media_type="model/stl",
                        filename=f"candidate_{key}.stl")


# ---------------------------------------------------------------------------
# MDO — AeroSandbox fast optimize tier, exposed with few, plain-language options.
# The nine penalty weights collapse to three priorities (efficiency / stability /
# buildable); everything else is defaulted and hidden. Winners land in the same
# candidate navigator as the equation lenses, promotable via VSPAERO -> CFD.
# ---------------------------------------------------------------------------
class MdoProposeReq(BaseModel):
    session_id: str


@app.post("/api/mdo/propose")
def mdo_propose(req: MdoProposeReq) -> Dict[str, Any]:
    """Auto-fill the MDO panel: quick-pick presets, default priorities/weights, a
    suggested search spread, and the plain-language questions that customise it."""
    return presets.propose(_plan_derived(req.session_id))


class MdoScoreReq(BaseModel):
    session_id: str
    params: Optional[Dict[str, Any]] = None
    efficiency: float = 0.5
    stability: float = 0.5
    buildable: float = 0.5
    cg_frac_mac: float = 0.25


@app.post("/api/mdo/score")
def mdo_score(req: MdoScoreReq) -> Dict[str, Any]:
    """Live MDO score of the current wing under the chosen priorities — the number
    the panel shows updating as the user drags a priority."""
    derived = _plan_derived(req.session_id)
    if derived is None:
        raise HTTPException(400, "no plan for this session — set a mission first")
    raw = req.params or _current_params(req.session_id)
    if raw is None:
        raise HTTPException(400, "no current geometry to score")
    try:
        params = BWBParams.model_validate(raw)
    except ValidationError as e:
        return {"feasible": False, "score": None, "reject_reason": "geometry out of gate",
                "errors": [er.get("msg", str(er)) for er in e.errors()]}
    weights = presets.weights_from_priorities(req.efficiency, req.stability, req.buildable)
    try:
        payload = asb_score_svc(params, derived, weights=weights, cg_frac_mac=req.cg_frac_mac)
    except AsbAeroRejected as e:
        raise HTTPException(422, str(e))
    return {
        "score": payload["score"], "feasible": payload["feasible"],
        "reject_reason": payload["reject_reason"],
        "l_over_d": payload["aero"]["l_over_d"],
        "static_margin": payload["breakdown"]["static_margin"],
        "penalties": {k: v["contribution"]
                      for k, v in payload["breakdown"]["penalties"].items()},
        "weights": payload["weights"],
    }


class MdoOptimizeReq(BaseModel):
    session_id: str
    efficiency: float = 0.5
    stability: float = 0.5
    buildable: float = 0.5
    spread: float = Field(default=0.15, gt=0, le=0.5)
    freeze: List[str] = Field(default_factory=list)
    seed: int = 0
    maxiter: int = Field(default=24, ge=1)
    popsize: int = Field(default=12, ge=4)
    top_n: int = Field(default=9, ge=1)
    cg_frac_mac: float = 0.25


@app.post("/api/mdo/optimize")
def mdo_optimize(req: MdoOptimizeReq):
    """Run the differential-evolution search around the current wing, streaming the
    gallery (per-generation top-N candidates + best score) as Server-Sent Events.
    On completion, winners are written to the candidate navigator (source='mdo')."""
    derived = _plan_derived(req.session_id)
    if derived is None:
        raise HTTPException(400, "no plan for this session — set a mission first")
    raw = _current_params(req.session_id)
    try:
        base = BWBParams.model_validate(raw) if raw else derived.params
    except ValidationError:
        raise HTTPException(400, "current geometry is out of gate — fix it before optimising")
    d2 = derived.model_copy(update={"params": base})  # search around the current wing
    space = DesignSpace.from_base(base, spread=req.spread, frozen=req.freeze)
    weights = presets.weights_from_priorities(req.efficiency, req.stability, req.buildable)

    events: "queue.Queue" = queue.Queue()
    holder: Dict[str, Any] = {}

    def on_gen(gen, best, n_evals, tiles):
        events.put({"type": "generation", "gen": gen, "best": best,
                    "n_evals": n_evals, "tiles": tiles})

    def run():
        try:
            res = optimize_svc(d2, space=space, weights=weights,
                               cg_frac_mac=req.cg_frac_mac, seed=req.seed,
                               maxiter=req.maxiter, popsize=req.popsize,
                               top_n=req.top_n, on_generation=on_gen)
            winners = [w.model_dump() for w in res.top]
            if winners:
                cand_svc.add_mdo_candidates(req.session_id, winners)
            holder["result"] = res
        except Exception as e:  # noqa: BLE001 — surfaced to the client as an SSE error
            holder["error"] = str(e)
        finally:
            events.put({"type": "done"})

    threading.Thread(target=run, daemon=True).start()

    def stream():
        while True:
            ev = events.get()
            if ev.get("type") == "done":
                break
            yield f"data: {json.dumps(ev)}\n\n"
        if "result" in holder:
            r = holder["result"]
            payload = {
                "type": "result",
                "winner": (r.winner.model_dump() if r.winner else None),
                "top": [{"rank": t.rank, "key": f"mdo_{t.rank}", "score": t.score,
                         "l_over_d": t.l_over_d, "static_margin": t.static_margin,
                         "params": t.params} for t in r.top],
                "reference": r.reference, "n_evals": r.n_evals,
                "n_feasible": r.n_feasible,
                "candidate_keys": [f"mdo_{t.rank}" for t in r.top],
            }
            yield f"data: {json.dumps(payload)}\n\n"
        else:
            yield f"data: {json.dumps({'type': 'error', 'message': holder.get('error', 'unknown')})}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


class LocksRequest(BaseModel):
    session_id: str
    locks: Dict[str, bool]


@app.post("/api/locks")
def set_locks(req: LocksRequest) -> Dict[str, Any]:
    path = _locks_path(req.session_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(req.locks, f, indent=2)
    return {"locks": req.locks}


@app.get("/api/handoff/{session_id}")
def stage1_handoff(session_id: str) -> Dict[str, Any]:
    """The Stage-1 -> Stage-2 artifact for this session (S1-I).

    Built on demand rather than persisted: it is a VIEW of the session's current
    geometry, requirements and locks, and a stale copy on disk would be worse than no
    copy at all. Stage 2 does not exist yet, so the transport is deliberately the
    simplest thing that can be versioned — `schema_version` is how a future consumer
    tells what it is reading."""
    sid = _validated_sid(session_id)
    # A session id that passes the format check but does not exist raises
    # FileNotFoundError out of the store, which uvicorn turns into a 500. A caller
    # asking for a session that is not there has made a 404-shaped mistake, and a 500
    # tells them the server is broken instead.
    try:
        raw = _current_params(sid)
        requirements = _current_requirements(sid)
    except FileNotFoundError:
        raise HTTPException(404, detail=f"unknown design session: {session_id}")
    if not raw:
        raise HTTPException(404, detail="no geometry in this session yet")
    params = BWBParams.model_validate(raw)
    atmosphere = atmosphere_at(requirements.cruise_altitude_m)
    panel = aero_estimate(params, atmosphere, requirements.cruise_speed_ms,
                          requirements.mtow_kg * 9.81, requirements.launch)

    locks_flags = _read_locks(sid)
    locked = LockedValues(
        span_m=params.span_m if locks_flags.get("span_max") else None,
        s_ref_m2=params.planform_area_m2() if locks_flags.get("s_ref") else None,
        mtow_kg=requirements.mtow_kg if locks_flags.get("mtow_cap") else None)

    handoff = build_handoff(
        params, panel, requirements,
        requirement_status=check_requirements(requirements, params),
        locked=locked, altitude_m=requirements.cruise_altitude_m)
    return {"schema_version": SCHEMA_VERSION,
            "handoff": json.loads(handoff.model_dump_json())}


@app.get("/api/explanations/{session_id}")
def explanations(session_id: str) -> Dict[str, Any]:
    return {"explanations": _read_explanations(session_id)}


class SnapshotRequest(BaseModel):
    name: str
    data_url: str


@app.post("/api/debug/snapshot")
def save_snapshot(req: SnapshotRequest) -> Dict[str, str]:
    """Verification helper: persist a canvas capture from the frontend so
    tooling can inspect the 3D view where compositor screenshots fail."""
    import base64
    import re
    if not re.fullmatch(r"[\w.-]{1,60}", req.name):
        raise HTTPException(400, "bad snapshot name")
    try:
        b64 = req.data_url.split(",", 1)[1]
        blob = base64.b64decode(b64)
    except Exception:
        raise HTTPException(400, "bad data url")
    d = os.path.join(os.path.dirname(ds.sessions_root()), "cockpit_snaps")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, req.name + ".jpg")
    with open(path, "wb") as f:
        f.write(blob)
    return {"path": path}


@app.get("/api/render/{session_id}/{version}/{view}")
def get_render(session_id: str, version: int, view: str):
    path = _session_path(session_id, "geometry", f"v{version}", "renders", f"{view}.png")
    if not os.path.exists(path):
        raise HTTPException(404, "render not found")
    return FileResponse(path, media_type="image/png")


# ---------------------------------------------------------------------------
# Stage 1 — requirements intake (three-tier list) and concept gallery.
# The intake is its own versioned artifact under the session
# (intake/v<N>/intake.json); saving it never touches stage validity. The
# "generate" endpoint is the only bridge: intake -> plan version -> candidates.
# ---------------------------------------------------------------------------

def _intake_dir(sid: str) -> str:
    return _session_path(sid, "intake")


def _warning(kind: str, message: str, **fields) -> Dict[str, Any]:
    """A store- or boundary-level notice in the same shape as an IntakeWarning (S3-D2).

    One channel, one shape: the screen renders "you typed X · using Y" identically
    whether the substitution happened in the intake, at the request boundary, or
    during a recovery from a damaged file."""
    return intake_pack.IntakeWarning(kind=kind, message=message,
                                     **fields).model_dump()


def _intake_warnings(intake) -> List[Dict[str, Any]]:
    """The disclosures for an intake AS IT IS NOW (S3-D2).

    The stored warnings were computed when the file was written. A file can change
    underneath them — S1-B3's damage model, an older build's schema, a hand-edited
    version — so the substitution warnings are recomputed from the rows in front of us
    and the stored ones contribute only the event kinds (an ambiguous brief, an
    overridden edit) that nothing can recompute. Same ownership split as
    `intake.derive`.
    """
    recomputed = {"clamped", "rejected", "unparsed"}
    events = [w for w in intake.warnings if w.kind not in recomputed]
    # refresh_disclosure also updates each row's used_value / drives_computation, so a
    # version written before those fields existed still renders honestly.
    fresh = intake_pack.refresh_disclosure(intake)
    return [w.model_dump() for w in events + fresh]


def _intake_latest_version(sid: str) -> int:
    """The version index.json DECLARES latest, or 0 if it is absent or unreadable.

    A corrupt index is not fatal: the directory scan below is the authority on what
    actually exists (S1-B4)."""
    idx = os.path.join(_intake_dir(sid), "index.json")
    if not os.path.exists(idx):
        return 0
    try:
        with open(idx) as f:
            return int(json.load(f).get("latest", 0))
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return 0


def _intake_versions_on_disk(sid: str) -> List[int]:
    """Version numbers with a directory, newest first."""
    root = _intake_dir(sid)
    if not os.path.isdir(root):
        return []
    found = []
    for name in os.listdir(root):
        if name.startswith("v") and name[1:].isdigit() \
                and os.path.isdir(os.path.join(root, name)):
            found.append(int(name[1:]))
    return sorted(found, reverse=True)


def _load_intake(sid: str) -> tuple:
    """(intake, version, warnings). A fresh session gets a derived default list (v0,
    unsaved) so the screen never opens empty.

    Degrades instead of 500-ing (S1-B3/B4). The declared-latest version can be
    unreadable (truncated JSON, a payload the schema no longer accepts) or simply
    absent, and every intake route used to raise straight out of here — which bricked
    all three of them, including the GET that would have shown the user what was wrong.
    Recovery falls back to the newest version that DOES parse and names the damaged one
    in `warnings`. The corrupt file is never rewritten or deleted: it is the user's
    data, and the next save lands on a new version number beside it.
    """
    declared = _intake_latest_version(sid)
    on_disk = _intake_versions_on_disk(sid)
    default = intake_pack.derive(intake_pack.default_intake())

    if not on_disk:
        if declared:
            return default, 0, [_warning(
                "recovered",
                f"intake v{declared} is recorded as current but no version exists on "
                f"disk — starting from the assumed defaults")]
        return default, 0, []

    order = ([declared] if declared in on_disk else []) \
        + [v for v in on_disk if v != declared]
    unreadable = []
    for version in order:
        path = os.path.join(_intake_dir(sid), f"v{version}", "intake.json")
        try:
            with open(path) as f:
                intake = intake_pack.Intake.model_validate(json.load(f))
        except (json.JSONDecodeError, ValidationError, OSError, ValueError) as exc:
            unreadable.append(f"v{version} ({type(exc).__name__})")
            continue
        warnings = []
        if declared and version != declared:
            warnings.append(_warning(
                "recovered",
                f"intake v{declared} unreadable — recovered from v{version}"))
        elif not declared:
            warnings.append(_warning(
                "recovered",
                f"the intake version index was unreadable — recovered from "
                f"v{version}"))
        return intake, version, warnings

    return default, 0, [_warning(
        "recovered",
        f"no readable intake version ({', '.join(unreadable)}) — starting from the "
        f"assumed defaults. Your saved versions are untouched on disk.")]


def _save_intake(sid: str, intake: intake_pack.Intake) -> int:
    """Persist a new intake version, or reuse the identical current one.

    Locked (S1-B2): read-latest → allocate → write index was an unguarded
    read-modify-write, and 12 parallel saves produced 5x HTTP 500 with 10 of the 12
    versions silently lost.

    Idempotent (S1-D8): an edit that changes nothing (including `edits: []`, which the
    UI sends on any no-op interaction) returns the current version rather than stacking
    a byte-identical copy.
    """
    payload = intake.model_dump_json(indent=2)
    root = _intake_dir(sid)
    with ds.session_lock(sid):
        declared = _intake_latest_version(sid)
        on_disk = _intake_versions_on_disk(sid)

        if declared and declared in on_disk:
            current_path = os.path.join(root, f"v{declared}", "intake.json")
            try:
                with open(current_path) as f:
                    if f.read() == payload:
                        return declared
            except OSError:
                pass  # unreadable current version: write a new one, never overwrite

        # Highest of declared and on-disk: a corrupt index must not hand out a number
        # that overwrites an existing version.
        n = max([declared] + on_disk) + 1
        ds.atomic_write(os.path.join(root, f"v{n}", "intake.json"), payload)
        ds.atomic_write_json(os.path.join(root, "index.json"), {"latest": n})
        return n


@app.get("/stage1")
def stage1_page():
    return FileResponse(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "static", "stage1.html"),
        media_type="text/html")


@app.get("/api/stage1/intake")
def stage1_intake(session: Optional[str] = None) -> Dict[str, Any]:
    sid = _ensure_session(session)
    intake, version, warnings = _load_intake(sid)
    # compute the disclosures BEFORE dumping: refresh_disclosure writes onto the rows
    warnings = warnings + _intake_warnings(intake)
    # store-level notices (a recovered version) plus the intake's own (an ambiguous
    # brief, an unreadable value, a clamp)
    return {"session_id": sid, "version": version,
            "intake": intake.model_dump(), "warnings": warnings}


# Request-size limits (S1-A3). Every call writes a new immutable version, so an
# unbounded request is unbounded disk: a single 2 MB edit value was persisted verbatim,
# 500 edits in one call produced a 519-row intake, and a 50,000-char brief was stored.
MAX_BRIEF_CHARS = 4000
MAX_EDITS_PER_REQUEST = 64
MAX_EDIT_VALUE_CHARS = 200


def _check_edit_sizes(edits: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    for edit in edits:
        for field in ("value", "label"):
            if field in edit and len(str(edit[field])) > MAX_EDIT_VALUE_CHARS:
                raise ValueError(
                    f"edit '{edit.get('id', '?')}' has a {field} of "
                    f"{len(str(edit[field]))} characters; the limit is "
                    f"{MAX_EDIT_VALUE_CHARS}")
    return edits


class IntakeParseReq(BaseModel):
    session_id: Optional[str] = None
    brief: str = Field(max_length=MAX_BRIEF_CHARS)
    replace: bool = False
    """Start over from the assumed defaults instead of merging onto the stored intake.

    The deliberate start-over case, kept explicit because the destructive behaviour
    used to be the only behaviour (S2-D5)."""


@app.post("/api/stage1/parse")
def stage1_parse(req: IntakeParseReq) -> Dict[str, Any]:
    """Deterministic brief parse — the LLM parse arrives via chat/MCP; this
    keeps the standalone cockpit honest (matched fields only).

    Merges onto the stored intake (S2-D5): parsing a brief after editing used to
    revert edited rows to their assumed defaults and delete user-added custom rows,
    with a 200 and no warning.
    """
    sid = _require_session(req.session_id)
    with ds.session_lock(sid):
        if req.replace:
            merged = intake_pack.parse_brief(req.brief)
        else:
            stored, _, load_warnings = _load_intake(sid)
            merged = intake_pack.merge_brief(stored, req.brief)
            merged.warnings = list(load_warnings) + merged.warnings
        intake = intake_pack.derive(merged)
        version = _save_intake(sid, intake)
    _queue_explanation(
        sid, "stage1_intake",
        f"Stage-1 brief parsed (intake v{version}): "
        + ", ".join(f"{r.label}={r.value}" for r in intake.rows
                    if r.origin == "you_said") + ".",
        {"intake_version": version})
    disclosures = _intake_warnings(intake)      # refreshes the rows; dump after
    return {"session_id": sid, "version": version, "intake": intake.model_dump(),
            "warnings": disclosures}


class IntakeEditReq(BaseModel):
    session_id: Optional[str] = None
    edits: List[Dict[str, Any]] = Field(max_length=MAX_EDITS_PER_REQUEST)


@app.post("/api/stage1/intake")
def stage1_intake_save(req: IntakeEditReq) -> Dict[str, Any]:
    sid = _require_session(req.session_id)
    with ds.session_lock(sid):
        intake, _, warnings = _load_intake(sid)
        try:
            intake = intake_pack.derive(
                intake_pack.apply_edits(intake, _check_edit_sizes(req.edits)))
        except ValueError as exc:
            # Rejected BEFORE any write, so an oversize or over-long request cannot
            # leave a version behind (S1-A3).
            raise HTTPException(422, str(exc))
        version = _save_intake(sid, intake)
    disclosures = warnings + _intake_warnings(intake)   # refreshes the rows first
    return {"session_id": sid, "version": version, "intake": intake.model_dump(),
            "warnings": disclosures}


class IntakeGenerateReq(BaseModel):
    session_id: Optional[str] = None


@app.post("/api/stage1/generate")
def stage1_generate(req: IntakeGenerateReq) -> Dict[str, Any]:
    """Intake -> plan version -> tier-0 concept candidates for the gallery."""
    sid = _require_session(req.session_id)
    # One lock for the whole chain: this route writes an intake version, a plan
    # version, a requirements version and a candidate batch, and a concurrent generate
    # interleaving with it produced plan versions with no matching batch and returned
    # plan_version values that matched no listable state (S1-B2).
    with ds.session_lock(sid):
        return _stage1_generate_locked(sid, req)


# requirements field <- the intake row that owns it. A field whose row is still
# `assumed` must not overwrite a requirement the user set explicitly on the M3 screen.
_REQUIREMENT_SOURCE_ROWS = {
    "mtow_kg": "mtow_cap",
    "cruise_altitude_m": "altitude",
    "launch": "launch",
    "span_max_m": "span_max",
    "v_stall_max_ms": "v_stall_max",
}


def _synced_requirements(sid: str, intake, mission: MissionSpec,
                         req_ds: DesignRequirements) -> tuple:
    """(payload, overrides) — the Stage-1 sync, made non-destructive (S2-D6).

    Before this, one generate silently reset a persisted `span_max_m` of 2.4 m to 3.0
    and `v_stall_max_ms` to 10.5, because it copied every field from the intake whether
    or not the user had ever touched the corresponding row. Stage 1 was destroying
    requirements set on the older requirements screen.

    Now a field is written only when its intake row carries the user's intent
    (`you_said` from a brief, or `edited`). Where that genuinely overrides a saved
    value, the previous value is recorded — in the response warnings and in the
    requirements version itself — so the overwrite is auditable rather than silent.
    """
    current = _current_requirements(sid)
    synced = current.model_dump()
    from_intake = {
        "mtow_kg": req_ds.mtow_kg,
        "cruise_altitude_m": mission.cruise_altitude_m,
        "launch": mission.launch,
        "span_max_m": req_ds.span_max_m,
        "v_stall_max_ms": req_ds.v_stall_max_ms,
    }
    rows = {r.id: r for r in intake.rows}
    overrides = []
    for field, value in from_intake.items():
        row = rows.get(_REQUIREMENT_SOURCE_ROWS[field])
        if row is None or row.origin not in ("you_said", "edited"):
            continue
        previous = synced.get(field)
        if previous != value:
            overrides.append({"field": field, "previous": previous, "new": value})
        synced[field] = value
    # Cruise speed is an OUTCOME of the sizing solve, not an intake row the user can
    # own, so it always follows the plan.
    synced["cruise_speed_ms"] = mission.cruise_speed_ms
    return synced, overrides


def _stage1_generate_locked(sid: str, req: "IntakeGenerateReq") -> Dict[str, Any]:
    intake, iv, warnings = _load_intake(sid)
    if iv == 0:
        iv = _save_intake(sid, intake)
    if intake.feasibility and intake.feasibility.verdict == "error":
        raise HTTPException(409, "requirements do not size — fix the intake first")

    # A stored intake is a file, and files get damaged (S1-B3) or written by an older
    # build. Every number is re-validated here before it reaches MissionSpec: unusable
    # ones fall back to the documented default and say so, rather than raising a
    # pydantic error that the route turns into a 500 (S2-B5).
    warnings = list(warnings) + _intake_warnings(intake)

    def row_num(rid: str, fallback: float) -> float:
        value = intake_pack.usable_number(intake, rid)
        return fallback if value is None else value

    launch_row = next((x for x in intake.rows if x.id == "launch"), None)
    launch = launch_row.value.strip().lower() if launch_row else "hand"
    if launch not in ("hand", "catapult", "runway"):
        launch = "hand"

    payload_kg = min(max(row_num("payload", 1.0), 0.01), 10.0)
    mtow_kg = min(row_num("mtow_cap", 0.0), 25.0) or None
    if mtow_kg is not None and mtow_kg < payload_kg:
        # Reconciled BEFORE constructing the model, so the impossible combination
        # never reaches disk (S2-D7). Raising the cap rather than rejecting keeps the
        # request working; the warning is what makes the change visible.
        warnings.append(_warning(
            "clamped", row_id="mtow_cap", typed=f"{mtow_kg:g} kg",
            used=f"{payload_kg:g} kg", limit=f"payload {payload_kg:g} kg",
            message=(f"MTOW cap: {mtow_kg:g} kg is below the {payload_kg:g} kg "
                     f"payload — raised the cap to {payload_kg:g} kg")))
        mtow_kg = payload_kg

    mission = MissionSpec(
        payload_kg=payload_kg,
        cruise_speed_ms=min(max(row_num("cruise_speed", 18.0), 8.0), 60.0),
        cruise_altitude_m=min(max(row_num("altitude", 100.0), 0.0), 4000.0),
        mtow_kg=mtow_kg,
        launch=launch)
    # Plan sizing comes from the SAME inverse solve the intake's feasibility
    # ran — never defaults.derive's naive planform, which rejects envelope
    # corners (tiny aircraft) that solve_requirements repairs deterministically.
    req_ds = intake_pack.to_requirements(intake).model_copy(
        update={"cruise_speed_ms": mission.cruise_speed_ms})
    res = solve_requirements(req_ds)
    atmo = atmosphere_at(mission.cruise_altitude_m)
    derived = MissionDerived(
        mission=mission, atmosphere=atmo, mtow_kg=req_ds.mtow_kg,
        air_density_kgm3=atmo.density_kgm3, weight_n=req_ds.mtow_kg * 9.81,
        wing_loading_kgm2=res.panel.wing_loading_kgm2,
        s_ref_target_m2=res.panel.s_ref_m2,
        required_cl=res.panel.cl_cruise,
        reynolds_mac=(mission.cruise_speed_ms * res.params.mean_aero_chord_m()
                      / atmo.kinematic_viscosity_m2s),
        params=res.params,
        assumptions=(["Sized by the stage-1 intake (inverse solve at "
                      "best-L/D cruise)"] + res.notes))
    version = _write_plan_version(sid, derived,
                                  f"stage-1 intake v{iv} locked",
                                  invalidate_from="fast_aero")
    # sync the VALIDATED requirements (req_ds already clamped span/stall into
    # schema range) — never raw row values, which may be out of range
    synced, overrides = _synced_requirements(sid, intake, mission, req_ds)
    for override in overrides:
        warnings.append(_warning(
            "overridden", row_id=_REQUIREMENT_SOURCE_ROWS[override["field"]],
            typed=str(override["new"]), used=str(override["new"]),
            limit=str(override["previous"]),
            message=(f"{override['field']}: replaced your saved "
                     f"{override['previous']} with {override['new']} from the "
                     f"Stage-1 intake")))
    ds.save_requirements(sid, dict(synced, intake_overrides=overrides))
    try:
        batch = cand_svc.generate(sid, regenerate=True)
    except cand_svc.CandidateError as e:
        raise HTTPException(409, str(e))
    _queue_explanation(
        sid, "stage1_generate",
        f"Stage-1 concepts generated from intake v{iv} (plan v{version}): "
        + ", ".join(c["title"] for c in batch["candidates"]) + ".",
        {"plan_version": version, "intake_version": iv})
    return {"session_id": sid, "plan_version": version, "intake_version": iv,
            "candidates": batch["candidates"], "warnings": warnings}


# static frontend (mounted last so /api/* wins)
_STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app.mount("/", StaticFiles(directory=_STATIC, html=True), name="static")


def main() -> None:
    import uvicorn
    ap = argparse.ArgumentParser(description="Astra Aero Design Cockpit server")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
