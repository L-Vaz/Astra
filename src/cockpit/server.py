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

from domain_packs.bwb.defaults import (  # noqa: E402
    MU_AIR, PAYLOAD_FRACTION, derive, isa_density)
from domain_packs.bwb.schema import (  # noqa: E402
    BWBParams, GeometryReport, MissionDerived, MissionSpec)
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


def _write_plan_version(sid: str, derived: MissionDerived, note: str) -> int:
    vdir = ds.new_version_dir(sid, "plan")
    with open(os.path.join(vdir, "mission_derived.json"), "w") as f:
        f.write(derived.model_dump_json(indent=2))
    with open(os.path.join(vdir, "enriched_requirement.txt"), "w") as f:
        f.write(build_enriched_requirement(derived))
    return ds.record_version(sid, "plan", {"prompt": note, "source": "cockpit"})


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
    rho = isa_density(float(mission.get("cruise_altitude_m", 100.0)))
    v = float(mission.get("cruise_speed_ms", 25.0))
    payload = float(mission.get("payload_kg", 1.0))
    mtow = mission.get("mtow_kg") or payload / PAYLOAD_FRACTION
    q = 0.5 * rho * v ** 2
    w = mtow * 9.81
    out = {"evidence": "instant",
           "air_density_kgm3": rho, "dynamic_pressure_pa": q,
           "mtow_kg": mtow, "weight_n": w,
           "mtow_assumed": mission.get("mtow_kg") in (None, "", 0)}
    if plan:
        out.update(plan)
        out["required_cl"] = w / (q * plan["s_ref_m2"])
        out["reynolds_mac"] = rho * v * plan["mac_m"] / MU_AIR
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
        derived = derived.model_copy(update={"params": BWBParams.model_validate(current)})
    version = _write_plan_version(req.session_id, derived, "cockpit mission update")
    _queue_explanation(
        req.session_id, "mission",
        f"Mission updated in cockpit (plan v{version}): payload {mission.payload_kg} kg, "
        f"{mission.cruise_speed_ms} m/s at {mission.cruise_altitude_m} m, "
        f"launch {mission.launch}.",
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

    version = ds.record_version(req.session_id, "geometry", {
        "source": "cockpit", "note": req.note,
        "overrides": {k: req.params[k] for k in req.params
                      if abs(float(old.get(k, req.params[k])) - float(req.params[k])) > 1e-9},
        "passed": report.all_gates_pass,
    })

    changes = _diff_params(old, req.params)
    _queue_explanation(
        req.session_id, "revision",
        f"Geometry v{version} applied from cockpit"
        + (f" ({req.note})" if req.note else "") + ". "
        + ("Changed " + "; ".join(changes) + ". " if changes else "")
        + f"Result: S={report.s_ref_m2:.3f} m2, AR={report.aspect_ratio:.2f}, "
          f"MAC={report.mean_aero_chord_m:.3f} m, watertight={report.watertight}. "
          f"Downstream fast-aero/mesh/CFD results are now invalidated.",
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
    version = ds.record_version(req.session_id, "fast_aero",
                                {"source": "cockpit", "passed": True})
    _queue_explanation(
        req.session_id, "fast_aero",
        f"VSPAERO fast validation v{version} from cockpit: trim alpha "
        f"{result['alpha_trim_deg']:.2f} deg, L/D {result['ld_at_trim']:.1f}, "
        f"CL_alpha {result['cl_alpha_per_deg']:.4f}/deg, x_np {result['x_np_m']:.3f} m.",
        {"version": version})
    result["version"] = version
    return result


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
