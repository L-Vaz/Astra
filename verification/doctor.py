"""Astra doctor — deterministic environment preflight.

Turns the "Critical caveats" of AGENTS.md into enforced checks: run paths
OpenFOAM can accept, the two-Python split (FoamAgent env vs the OpenVSP
3.10 venv), a sourced OpenFOAM environment, prebuilt FAISS indices, and LLM
credentials. No LLM calls, no solver runs; every check is a file/subprocess
probe that finishes in seconds.

Usage (WSL, FoamAgent conda env — the same shell you run the pipeline from):
    python verification/doctor.py

Solver-side checks (OpenVSP venv, OpenFOAM) SKIP on Windows: they diagnose
the WSL runtime, not the editing host.
"""
import importlib
import importlib.util
import os
import shutil
import subprocess
import sys

from _common import REPO_ROOT, RUNS_DIR, Table

_IS_POSIX = os.name == "posix"

# Pipeline imports worth probing (module name, why it matters).
_REQUIRED_IMPORTS = [
    ("pydantic", "schemas and gates"),
    ("fastapi", "cockpit server"),
    ("langgraph", "inherited CFD graph"),
    ("langchain", "LLM plumbing"),
    ("faiss", "RAG indices"),
    ("numpy", "numerics"),
    ("gmsh", "Astra farfield meshing"),
]

_FAISS_INDICES = ["openfoam_allrun_scripts", "openfoam_command_help",
                  "openfoam_tutorials_details", "openfoam_tutorials_structure"]


def _writable(path: str) -> bool:
    probe = os.path.join(path, ".astra_doctor_probe")
    try:
        os.makedirs(path, exist_ok=True)
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
        return True
    except OSError:
        return False


def _check_paths(t: Table) -> None:
    if " " in REPO_ROOT:
        t.add("paths: repo location", True,
              "path contains spaces — OpenFOAM artifacts must divert to the runs dir")
    else:
        t.add("paths: repo location", True, REPO_ROOT)
    ok = " " not in RUNS_DIR and _writable(RUNS_DIR)
    t.add("paths: runs dir (ASTRA_RUNS_DIR)", ok,
          RUNS_DIR + ("" if ok else " — must be a writable, space-free path"))

    try:
        sroot = _sessions_root()
    except Exception as exc:
        t.add("paths: sessions dir (ASTRA_SESSIONS_DIR)", False,
              f"cannot resolve — {exc}")
        return
    t.add("paths: sessions dir (ASTRA_SESSIONS_DIR)", _writable(sroot), sroot)


def _sessions_root() -> str:
    # Load design_session.py by file path: importing it through the services
    # package pulls in utils.py, whose import loads FAISS and the embedding
    # model — too heavy for a preflight and impossible on the editing host.
    path = os.path.join(REPO_ROOT, "src", "services", "design_session.py")
    spec = importlib.util.spec_from_file_location("_astra_design_session", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.sessions_root()


def _check_python_env(t: Table) -> None:
    v = sys.version_info
    t.add("python: interpreter", v >= (3, 10),
          f"{sys.executable} ({v.major}.{v.minor}.{v.micro})")
    for mod, why in _REQUIRED_IMPORTS:
        try:
            importlib.import_module(mod)
            t.add(f"python: import {mod}", True, why)
        except Exception as exc:  # ImportError or native-lib load failure
            t.add(f"python: import {mod}", False, f"{why} — {exc}")


def _check_openvsp(t: Table) -> None:
    vsp_python = os.environ.get("VSP_PYTHON", "/opt/vsp-venv/bin/python")
    if not _IS_POSIX:
        t.add("openvsp: venv", None, "WSL-side check — run doctor inside WSL")
        return
    if not os.path.exists(vsp_python):
        t.add("openvsp: venv", False, f"{vsp_python} not found (VSP_PYTHON)")
        return
    try:
        out = subprocess.run(
            [vsp_python, "-c",
             "import sys, openvsp; print('%d.%d' % sys.version_info[:2])"],
            capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        t.add("openvsp: venv", False, f"{vsp_python} timed out importing openvsp")
        return
    if out.returncode != 0:
        t.add("openvsp: venv", False,
              (out.stderr.strip().splitlines() or ["import openvsp failed"])[-1])
        return
    pyver = out.stdout.strip()
    t.add("openvsp: venv", pyver == "3.10",
          f"{vsp_python} is Python {pyver}"
          + ("" if pyver == "3.10" else " — OpenVSP bindings require 3.10"))


def _check_openfoam(t: Table) -> None:
    if not _IS_POSIX:
        t.add("openfoam: FOAM_BASHRC", None, "WSL-side check — run doctor inside WSL")
        t.add("openfoam: sourced environment", None,
              "WSL-side check — run doctor inside WSL")
        return
    # Astra's mesh stage (services/bwb_mesh._foam) self-sources this file.
    bashrc = os.environ.get("FOAM_BASHRC", "/opt/openfoam10/etc/bashrc")
    t.add("openfoam: FOAM_BASHRC", os.path.exists(bashrc),
          bashrc + ("" if os.path.exists(bashrc)
                    else " not found — Astra mesh stage cannot source OpenFOAM"))
    # The inherited graph (utils.run_command) prefers a sourced env and
    # falls back to FOAM_BASHRC when the shell is unsourced.
    wm = os.environ.get("WM_PROJECT_DIR", "")
    if wm and os.path.isdir(wm):
        t.add("openfoam: sourced environment", True, f"WM_PROJECT_DIR={wm}")
    elif shutil.which("simpleFoam"):
        t.add("openfoam: sourced environment", True,
              f"simpleFoam on PATH ({shutil.which('simpleFoam')})")
    elif os.path.exists(bashrc):
        t.add("openfoam: sourced environment", True,
              "WM_PROJECT_DIR unset — ok: run_command falls back to FOAM_BASHRC")
    else:
        t.add("openfoam: sourced environment", False,
              "WM_PROJECT_DIR unset and FOAM_BASHRC missing — CFD cannot run")


def _check_faiss_db(t: Table) -> None:
    from config import Config  # class attrs only: __post_init__ (and its prints) never runs
    model = os.environ.get("FOAMAGENT_EMBEDDING_MODEL", "").strip() or Config.embedding_model
    # same sanitization as utils.load_faiss_dbs
    droot = os.path.join(REPO_ROOT, "database", "faiss",
                         model.replace("/", "_").replace(":", "_"))
    missing = [ix for ix in _FAISS_INDICES
               if not os.path.isdir(os.path.join(droot, ix))]
    t.add("rag: faiss indices", not missing,
          droot if not missing else
          f"{droot} missing {', '.join(missing)} — run init_database.py or fix the embedding model")


def _check_llm_credentials(t: Table) -> None:
    from config import Config
    provider = os.environ.get("FOAMAGENT_MODEL_PROVIDER", "").strip() or Config.model_provider
    key_var = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}.get(provider)
    if key_var is None:
        t.add("llm: credentials", None, f"provider '{provider}' — no env-key check defined")
    elif os.environ.get(key_var):
        t.add("llm: credentials", True, f"{key_var} set (provider {provider})")
    else:
        t.add("llm: credentials", None,
              f"{key_var} unset — LLM runs unavailable; --no-llm paths unaffected")


def main() -> int:
    t = Table("ASTRA DOCTOR")
    _check_paths(t)
    _check_python_env(t)
    _check_openvsp(t)
    _check_openfoam(t)
    _check_faiss_db(t)
    _check_llm_credentials(t)
    return t.finish()


if __name__ == "__main__":
    sys.exit(main())
