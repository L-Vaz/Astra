"""Design-session store — versioned per-stage artifacts with rollback.

Layout:  runs/design_sessions/<session_id>/<stage>/v<N>/...
         runs/design_sessions/<session_id>/session.json

Claude Code (the MCP client) is stateless between calls, so this store is the
single source of truth for a design session. Creating a new version of stage k
invalidates every stage after k; old versions stay on disk for comparison.
"""
import hashlib
import json
import os
import platform
import subprocess
import time
import uuid
from typing import Optional

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

STAGES = ["plan", "geometry", "fast_aero", "mesh", "simulation", "results"]

# Manifest inventory limits — big meshes/CFD outputs are recorded by size only,
# and a runaway directory can't stall version recording. Truncation is flagged
# in the manifest, never silent.
_HASH_CAP_BYTES = 32 * 1024 * 1024
_INVENTORY_CAP_FILES = 500


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


_git_commit_cache: Optional[tuple] = None


def _git_commit() -> Optional[str]:
    """Repo HEAD at first use, cached; None when git is unavailable."""
    global _git_commit_cache
    if _git_commit_cache is None:
        try:
            out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=_REPO_ROOT,
                                 capture_output=True, text=True, timeout=5)
            _git_commit_cache = (out.stdout.strip() if out.returncode == 0 else None,)
        except Exception:
            _git_commit_cache = (None,)
    return _git_commit_cache[0]


def _artifact_inventory(vdir: str) -> dict:
    """Inventory of the stage version's files: relative path, size, and sha256
    for anything small enough to hash. The registry (roadmap phase 4) ingests
    this instead of re-scanning disks."""
    files, truncated = [], False
    for root, _dirs, names in os.walk(vdir):
        for name in sorted(names):
            if root == vdir and name == "manifest.json":
                continue
            if len(files) >= _INVENTORY_CAP_FILES:
                truncated = True
                break
            path = os.path.join(root, name)
            size = os.path.getsize(path)
            entry = {"path": os.path.relpath(path, vdir).replace(os.sep, "/"),
                     "bytes": size}
            if size <= _HASH_CAP_BYTES:
                h = hashlib.sha256()
                with open(path, "rb") as f:
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        h.update(chunk)
                entry["sha256"] = h.hexdigest()
            files.append(entry)
        if truncated:
            break
    return {"files": files, "truncated": truncated}


def record_version(session_id: str, stage: str, summary: dict,
                   invalidate_from: Optional[str] = None) -> int:
    """Mark the newest version of `stage` valid and invalidate downstream stages.

    Besides the caller's summary, the manifest records machine-readable
    provenance: which upstream stage versions and requirements version were
    current, an artifact inventory with hashes, and tool identity. That makes
    each version directory self-describing evidence a future DOE/provenance
    registry can ingest without re-running anything.

    invalidate_from: first stage to invalidate; defaults to the stage right
    after `stage` (the historical behaviour). Passing a later stage keeps the
    ones in between valid — e.g. record_version(sid, "plan",
    invalidate_from="fast_aero") records a new mission WITHOUT orphaning the
    built geometry (a mission change alters the flight condition, not the
    already-built shape; geometry staleness vs requirements is tracked
    separately by the requirements store below)."""
    state = _load(session_id)
    n = state["stages"][stage]["latest"] + 1
    state["stages"][stage] = {"latest": n, "valid": True}
    first_invalid = STAGES.index(invalidate_from) if invalidate_from \
        else STAGES.index(stage) + 1
    if first_invalid <= STAGES.index(stage):
        raise ValueError(f"invalidate_from '{invalidate_from}' must come after '{stage}'")
    for later in STAGES[first_invalid:]:
        state["stages"][later]["valid"] = False
    _save(state)

    vdir = os.path.join(sessions_root(), session_id, stage, f"v{n}")
    os.makedirs(vdir, exist_ok=True)
    upstream = {s: state["stages"][s]["latest"]
                for s in STAGES[:STAGES.index(stage)]
                if state["stages"][s]["valid"]}
    manifest = {
        "stage": stage, "version": n,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "inputs": {"upstream": upstream,
                   "requirements_version": _latest_requirements_version(session_id)},
        "artifacts": _artifact_inventory(vdir),
        "tools": {"python": platform.python_version(),
                  "platform": platform.platform(),
                  "git_commit": _git_commit()},
        **summary,
    }
    with open(os.path.join(vdir, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)
    return n


# ---------------------------------------------------------------------------
# requirements store — first-class, versioned, deliberately NOT a stage:
# saving requirements never touches stage validity. The cockpit/server compare
# the latest requirements against the built geometry and surface staleness
# instead of invalidating anything.
# ---------------------------------------------------------------------------

def _requirements_dir(session_id: str) -> str:
    return os.path.join(sessions_root(), session_id, "requirements")


def _latest_requirements_version(session_id: str) -> int:
    """Latest saved requirements version, 0 for pre-requirements sessions."""
    idx_path = os.path.join(_requirements_dir(session_id), "index.json")
    if not os.path.exists(idx_path):
        return 0
    with open(idx_path) as f:
        return json.load(f).get("latest", 0)


def save_requirements(session_id: str, payload: dict) -> int:
    """Persist a new requirements version; returns the version number."""
    _load(session_id)  # raises on unknown session
    root = _requirements_dir(session_id)
    idx_path = os.path.join(root, "index.json")
    latest = 0
    if os.path.exists(idx_path):
        with open(idx_path) as f:
            latest = json.load(f).get("latest", 0)
    n = latest + 1
    vdir = os.path.join(root, f"v{n}")
    os.makedirs(vdir, exist_ok=True)
    with open(os.path.join(vdir, "requirements.json"), "w") as f:
        json.dump({"created": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "version": n, **payload}, f, indent=2)
    with open(idx_path, "w") as f:
        json.dump({"latest": n}, f, indent=2)
    return n


def latest_requirements(session_id: str) -> Optional[dict]:
    """Latest saved requirements payload, or None for pre-requirements sessions."""
    root = _requirements_dir(session_id)
    idx_path = os.path.join(root, "index.json")
    if not os.path.exists(idx_path):
        return None
    with open(idx_path) as f:
        n = json.load(f).get("latest", 0)
    if n == 0:
        return None
    path = os.path.join(root, f"v{n}", "requirements.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)
