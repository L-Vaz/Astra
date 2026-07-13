"""Design-session store — versioned per-stage artifacts with rollback.

Layout:  runs/design_sessions/<session_id>/<stage>/v<N>/...
         runs/design_sessions/<session_id>/session.json

Claude Code (the MCP client) is stateless between calls, so this store is the
single source of truth for a design session. Creating a new version of stage k
invalidates every stage after k; old versions stay on disk for comparison.
"""
import json
import os
import time
import uuid
from typing import Optional

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

STAGES = ["plan", "geometry", "fast_aero", "mesh", "simulation", "results"]


def sessions_root() -> str:
    return os.environ.get(
        "ASTRA_SESSIONS_DIR",
        os.path.join(_REPO_ROOT, "runs", "design_sessions"))


def _session_file(session_id: str) -> str:
    return os.path.join(sessions_root(), session_id, "session.json")


def _load(session_id: str) -> dict:
    path = _session_file(session_id)
    if not os.path.exists(path):
        raise FileNotFoundError(f"unknown design session: {session_id}")
    with open(path) as f:
        return json.load(f)


def _save(state: dict) -> None:
    path = _session_file(state["session_id"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(state, f, indent=2)


def create_session(label: str = "") -> str:
    sid = uuid.uuid4().hex[:8]
    _save({
        "session_id": sid,
        "label": label,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "stages": {s: {"latest": 0, "valid": False} for s in STAGES},
    })
    return sid


def get_state(session_id: str) -> dict:
    return _load(session_id)


def latest_dir(session_id: str, stage: str) -> Optional[str]:
    """Path of the latest VALID version of a stage, or None."""
    state = _load(session_id)
    info = state["stages"][stage]
    if not info["valid"] or info["latest"] == 0:
        return None
    return os.path.join(sessions_root(), session_id, stage, f"v{info['latest']}")


def new_version_dir(session_id: str, stage: str) -> str:
    """Allocate (and create) the next version directory for a stage.
    Does NOT mark it valid — call record_version() once the stage succeeds."""
    state = _load(session_id)
    n = state["stages"][stage]["latest"] + 1
    path = os.path.join(sessions_root(), session_id, stage, f"v{n}")
    os.makedirs(path, exist_ok=True)
    return path


def record_version(session_id: str, stage: str, summary: dict) -> int:
    """Mark the newest version of `stage` valid and invalidate downstream stages."""
    state = _load(session_id)
    n = state["stages"][stage]["latest"] + 1
    state["stages"][stage] = {"latest": n, "valid": True}
    for later in STAGES[STAGES.index(stage) + 1:]:
        state["stages"][later]["valid"] = False
    _save(state)

    vdir = os.path.join(sessions_root(), session_id, stage, f"v{n}")
    os.makedirs(vdir, exist_ok=True)
    with open(os.path.join(vdir, "manifest.json"), "w") as f:
        json.dump({"stage": stage, "version": n,
                   "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                   **summary}, f, indent=2)
    return n
