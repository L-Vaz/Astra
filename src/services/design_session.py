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
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Optional

from filelock import FileLock, Timeout

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


class SessionIdRejected(ValueError):
    """A session id that would resolve outside the sessions root.

    A ValueError subclass so existing `except ValueError` callers keep working, and a
    distinct type so the HTTP layer can answer 400 rather than 500."""


def session_dir(session_id: str) -> str:
    """Session root, with a containment check (S1-A1, defence in depth).

    `session_id` arrives from an HTTP client. The cockpit validates its shape at the
    boundary, but a store that joins an unchecked string into a path is one missed call
    site away from writing outside the sessions root — probe-confirmed: "../escaped"
    and "/tmp/x" both created stage directories elsewhere on disk. This check is the
    backstop that does not depend on remembering to validate.
    """
    root = os.path.realpath(sessions_root())
    path = os.path.realpath(os.path.join(sessions_root(), session_id))
    if path == root:
        raise SessionIdRejected(
            "session id must name a session, not the sessions root")
    if not path.startswith(root + os.sep):
        raise SessionIdRejected(
            f"session id {session_id!r} resolves outside the sessions root")
    return path


def _session_file(session_id: str) -> str:
    return os.path.join(session_dir(session_id), "session.json")


# ---------------------------------------------------------------------------
# store integrity (S1-B2) — atomic writes and one advisory lock per session
#
# Every artifact write here is a read-modify-write against files a concurrent
# request may be reading. Before this, 12 parallel intake saves produced 5x HTTP 500
# (JSONDecodeError on a half-written file) and silently lost 10 of 12 versions.
#
# Two mechanisms, and both are needed:
#   * atomic_write - a reader never observes a partial file, because the rename is
#     atomic and the file it replaces was complete.
#   * session_lock - a writer never loses an update, because "read the latest version,
#     compute the next, write it" happens under one lock. Atomic writes alone would
#     still let two requests allocate the same version number.
# ---------------------------------------------------------------------------

# A held lock outliving a request is a hang; the timeout converts it into an error the
# caller can report. Generous by default because a generate holds the lock across the
# candidate batch, which is seconds of work.
LOCK_TIMEOUT_S = float(os.environ.get("ASTRA_SESSION_LOCK_TIMEOUT_S", "120"))


class SessionBusy(RuntimeError):
    """Raised when a session's lock could not be acquired within the timeout."""


_lock_registry: dict = {}
_lock_registry_guard = threading.Lock()


def _lock_for(session_id: str) -> FileLock:
    """One FileLock object per (sessions root, session).

    Keyed by root as well as id because tests point ASTRA_SESSIONS_DIR at a fresh
    tmp_path per test; a root-blind cache would hand back a lock on a deleted file.
    """
    key = (os.path.realpath(sessions_root()), session_id)
    with _lock_registry_guard:
        lock = _lock_registry.get(key)
        if lock is None:
            path = os.path.join(session_dir(session_id), ".lock")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            lock = FileLock(path, timeout=LOCK_TIMEOUT_S)
            _lock_registry[key] = lock
        return lock


@contextmanager
def session_lock(session_id: str):
    """Serialise mutations of one session across threads AND processes.

    Reentrant within a thread (filelock counts acquisitions), so a route may hold the
    lock while calling helpers that take it again. The OS releases it if the process
    dies, so unlike a hand-rolled lock file this cannot strand a session.
    """
    lock = _lock_for(session_id)
    try:
        lock.acquire()
    except Timeout:
        raise SessionBusy(
            f"session {session_id} is busy: another write held the lock for more "
            f"than {LOCK_TIMEOUT_S:g}s")
    try:
        yield
    finally:
        lock.release()


def atomic_write(path: str, text: str) -> None:
    """Write `text` to `path` so that no reader can ever see it half-written.

    Same-directory temp file (os.replace is only atomic within a filesystem), fsync
    before the rename, and the temp file is removed on any failure so a crashed write
    leaves the previous version intact rather than a corpse beside it.
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    # pid AND thread id: a ThreadPoolExecutor shares the pid, and two threads writing
    # the same path must not share a temp file.
    tmp = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
    try:
        with open(tmp, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path: str, payload, indent: int = 2) -> None:
    atomic_write(path, json.dumps(payload, indent=indent))


def _load(session_id: str) -> dict:
    path = _session_file(session_id)
    if not os.path.exists(path):
        raise FileNotFoundError(f"unknown design session: {session_id}")
    with open(path) as f:
        return json.load(f)


def _save(state: dict) -> None:
    atomic_write_json(_session_file(state["session_id"]), state)


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
    return os.path.join(session_dir(session_id), stage, f"v{info['latest']}")


def new_version_dir(session_id: str, stage: str) -> str:
    """Allocate (and create) the next version directory for a stage.
    Does NOT mark it valid — call record_version() once the stage succeeds.

    The number it returns is only stable while nobody else is writing this stage, and
    record_version() re-derives it, so a caller that writes files here and then records
    them must hold `session_lock(session_id)` across BOTH calls. Concurrent generates
    otherwise write their artifacts into the same version directory (S1-B2)."""
    state = _load(session_id)
    n = state["stages"][stage]["latest"] + 1
    path = os.path.join(session_dir(session_id), stage, f"v{n}")
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
    first_invalid = STAGES.index(invalidate_from) if invalidate_from \
        else STAGES.index(stage) + 1
    if first_invalid <= STAGES.index(stage):
        raise ValueError(f"invalidate_from '{invalidate_from}' must come after '{stage}'")

    # Read state, allocate the version, and publish it as one indivisible step: two
    # concurrent recorders otherwise both read latest=N and both become N+1, so one
    # version silently overwrites the other (S1-B2).
    with session_lock(session_id):
        state = _load(session_id)
        n = state["stages"][stage]["latest"] + 1
        state["stages"][stage] = {"latest": n, "valid": True}
        for later in STAGES[first_invalid:]:
            state["stages"][later]["valid"] = False
        _save(state)

        vdir = os.path.join(session_dir(session_id), stage, f"v{n}")
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
        atomic_write_json(os.path.join(vdir, "manifest.json"), manifest)
    return n


# ---------------------------------------------------------------------------
# requirements store — first-class, versioned, deliberately NOT a stage:
# saving requirements never touches stage validity. The cockpit/server compare
# the latest requirements against the built geometry and surface staleness
# instead of invalidating anything.
# ---------------------------------------------------------------------------

def _requirements_dir(session_id: str) -> str:
    return os.path.join(session_dir(session_id), "requirements")


def _latest_requirements_version(session_id: str) -> int:
    """Latest saved requirements version, 0 for pre-requirements sessions."""
    idx_path = os.path.join(_requirements_dir(session_id), "index.json")
    if not os.path.exists(idx_path):
        return 0
    with open(idx_path) as f:
        return json.load(f).get("latest", 0)


def save_requirements(session_id: str, payload: dict) -> int:
    """Persist a new requirements version; returns the version number.

    Idempotent (S1-D8): saving requirements identical to the current latest returns
    that version instead of stacking a byte-identical copy. Stage-1 generate re-syncs
    requirements on every run, so without this a user who presses Generate three times
    ends up with three indistinguishable versions and no way to tell which mattered.
    """
    _load(session_id)  # raises on unknown session
    with session_lock(session_id):
        root = _requirements_dir(session_id)
        idx_path = os.path.join(root, "index.json")
        current = _latest_requirements_version(session_id)

        if current:
            existing = latest_requirements(session_id) or {}
            # `created` and `version` are stamps, not content — compare what the caller
            # actually asked to store.
            if {k: v for k, v in existing.items()
                    if k not in ("created", "version")} == payload:
                return current

        n = current + 1
        vdir = os.path.join(root, f"v{n}")
        os.makedirs(vdir, exist_ok=True)
        atomic_write_json(os.path.join(vdir, "requirements.json"),
                          {"created": time.strftime("%Y-%m-%d %H:%M:%S"),
                           "version": n, **payload})
        atomic_write_json(idx_path, {"latest": n})
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
