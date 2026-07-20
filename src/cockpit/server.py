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
import sys
import time
from typing import Any, Dict, List, Optional

_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_ROOT = os.path.dirname(_SRC)
for p in (_SRC, _REPO_ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel, Field, ValidationError  # noqa: E402

from domain_packs.bwb.atmosphere import (  # noqa: E402
    atmosphere_at, atmosphere_provenance, atmosphere_table_payload)
from domain_packs.bwb.defaults import PAYLOAD_FRACTION, derive  # noqa: E402
from domain_packs.bwb.aero_estimates import LAUNCH_VSTALL_MAX  # noqa: E402
from domain_packs.bwb.derivation import derive_graph  # noqa: E402
from domain_packs.bwb.inverse import (  # noqa: E402
    MET_TOLERANCES, check_requirements, solve_requirements)
from domain_packs.bwb.requirements import DesignRequirements  # noqa: E402
from domain_packs.bwb.schema import (  # noqa: E402
    BWBParams, GeometryReport, MissionDerived, MissionSpec)
from domain_packs.bwb.sensitivity import (  # noqa: E402
    requirement_sensitivity_table, sensitivity_table)
from services import candidates as cand_svc  # noqa: E402
from services import design_session as ds  # noqa: E402
from services.geometry import GeometryRejected, build_geometry as build_geometry_svc  # noqa: E402
from services.intent import build_enriched_requirement  # noqa: E402
from services.vspaero import FastAeroRejected, run_fast_aero as fast_aero_svc  # noqa: E402

app = FastAPI(title="Astra Aero Design Cockpit")

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

def _session_path(sid: str, *parts: str) -> str:
    return os.path.join(ds.sessions_root(), sid, *parts)


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
    vdir = ds.new_version_dir(sid, "plan")
    with open(os.path.join(vdir, "mission_derived.json"), "w") as f:
        f.write(derived.model_dump_json(indent=2))
    with open(os.path.join(vdir, "enriched_requirement.txt"), "w") as f:
        f.write(build_enriched_requirement(derived))
    return ds.record_version(sid, "plan", {
                                 "prompt": note,
                                 "source": "cockpit",
                                 **atmosphere_provenance(derived.atmosphere),
                             },
                             invalidate_from=invalidate_from)


def _ensure_session(sid: Optional[str]) -> str:
    """Resolve a usable session: the given one, the most recent, or a fresh
    deterministic demo session (default mission, no LLM involved)."""
    if sid:
        try:
            ds.get_state(sid)
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
    geo = ds.latest_dir(sid, "geometry")
    if geo and os.path.exists(os.path.join(geo, "params.json")):
        with open(os.path.join(geo, "params.json")) as f:
            return json.load(f)
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
        return DesignRequirements.model_validate(
            {k: v for k, v in saved.items() if k in _REQ_FIELDS})
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

@app.get("/api/health")
def health() -> Dict[str, Any]:
    return {"ok": True, "service": "astra-cockpit"}


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


@app.get("/api/candidates/{session_id}")
def candidates_list(session_id: str) -> Dict[str, Any]:
    return cand_svc.list_candidates(session_id)


class CandidateAction(BaseModel):
    session_id: str
    key: str
    note: str = ""


@app.post("/api/candidates/build")
def candidates_build(req: CandidateAction) -> Dict[str, Any]:
    try:
        return cand_svc.build(req.session_id, req.key)
    except cand_svc.CandidateError as e:
        raise HTTPException(422, str(e))


@app.post("/api/candidates/screen")
def candidates_screen(req: CandidateAction) -> Dict[str, Any]:
    try:
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
    try:
        result = cand_svc.promote(req.session_id, req.key, note=req.note)
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
    try:
        return cand_svc.discard(req.session_id, req.key, reason=req.note)
    except cand_svc.CandidateError as e:
        raise HTTPException(422, str(e))


@app.get("/api/candidates/stl/{session_id}/{key}")
def candidates_stl(session_id: str, key: str):
    path = cand_svc.stl_path(session_id, key)
    if not os.path.exists(path):
        raise HTTPException(404, f"candidate '{key}' has no built STL yet")
    return FileResponse(path, media_type="model/stl",
                        filename=f"candidate_{key}.stl")


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
