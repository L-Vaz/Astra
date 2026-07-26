"""Shared pytest configuration for the Astra suite.

Three jobs, all of them about making the suite's own report trustworthy (plan
stage S0):

1. Keep manual scripts out of collection. `tests/test_lid_driven_cavity_*.py` are
   hand-run scripts with a `main()` and ZERO test functions; one of them imports the
   credential-gated `services` layer, which made collection abort and took the whole
   suite down with it.
2. Fail when the suite silently shrinks. A collection error, a bad conftest, or a
   stray skip can drop dozens of tests while the run still prints green.
3. Provide the one API-layer fixture the cockpit tests share.
"""
import os
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parent.parent
for _p in (_REPO_ROOT, _REPO_ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# --- 1. manual scripts, not tests -------------------------------------------------
# Named test_*.py but containing no test functions. Left in place (they are documented
# manual reproducers) and simply not collected.
collect_ignore = [
    "test_lid_driven_cavity_services.py",
    "test_lid_driven_cavity_mcp.py",
]


# --- 2. collection-count floor ----------------------------------------------------

SERVICES_UNAVAILABLE = "services layer unavailable"


def pytest_addoption(parser):
    """Register the ini keys used by the floor check.

    Registration is required because pyproject.toml sets --strict-config, which
    rejects unknown ini options.
    """
    parser.addini(
        "minimum_collected_tests",
        help="Floor for the environment-independent core (the tests that collect "
             "even when the services layer is unavailable).",
        default="0",
    )


def _is_full_run(config) -> bool:
    """True when the whole tests/ tree was requested without a filter.

    A targeted `pytest tests/test_x.py` or a `-k`/`-m` filter legitimately collects
    fewer tests; the floor must not fire on those.
    """
    if config.option.keyword or config.option.markexpr:
        return False
    if len(config.args) != 1:
        return False
    return Path(config.args[0]).resolve() == (_REPO_ROOT / "tests").resolve()


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """A shrinking suite must not be indistinguishable from a passing one.

    Two mechanisms, because environments differ legitimately:

    1. ALWAYS print what was collected and which modules opted out. The available
       environment varies (no services / services but no LLM credentials / both), so a
       single number cannot be asserted across all of them — but an unexplained drop is
       obvious the moment the count and the skip reasons are on screen every run.
    2. Enforce a floor on the environment-independent core. Those tests collect
       everywhere, so if they vanish something is genuinely broken.

    Reported at the END rather than during collection, so a floor breach never prevents
    the tests that DID collect from running — it only fails the run.
    """
    if not _is_full_run(config):
        return
    collected = terminalreporter._numcollected
    # A module-level skip has nodeid "tests/test_x.py"; a fixture-level skip has
    # "tests/test_x.py::test_y". Report files, not individual tests.
    opted_out = sorted({
        Path(str(r.nodeid).split("::", 1)[0]).name
        for r in terminalreporter.stats.get("skipped", [])
        if SERVICES_UNAVAILABLE in str(getattr(r, "longrepr", ""))
    })

    terminalreporter.write_sep("-", "suite size")
    terminalreporter.write_line(f"collected {collected} tests")
    if opted_out:
        terminalreporter.write_line(
            f"{len(opted_out)} file(s) skipped — services layer unavailable: "
            + ", ".join(opted_out))
        terminalreporter.write_line(
            "their tests are NOT in the count above. Causes differ — the services "
            "layer needs the WSL FoamAgent env, and some modules additionally need "
            "LLM credentials. Run verification/doctor.py to see which is missing.")

    floor = int(config.getini("minimum_collected_tests") or 0)
    if floor and collected < floor:
        terminalreporter.write_sep("=", "SUITE SHRANK", red=True)
        terminalreporter.write_line(
            f"collected {collected} tests but the core floor is {floor}.\n"
            "Tests disappeared — look for a collection error, an over-broad skip, or a "
            "bad conftest. If the drop is intentional, lower "
            "minimum_collected_tests in [tool.pytest.ini_options] (pyproject.toml)."
        )
        # Fail the run without hiding the results of the tests that did execute.
        terminalreporter._session.exitstatus = pytest.ExitCode.TESTS_FAILED


# --- 3. shared API-layer fixture --------------------------------------------------

@pytest.fixture()
def cockpit_client(tmp_path, monkeypatch):
    """TestClient over the cockpit app against a throwaway session store.

    Hoisted from tests/test_requirements_api.py so every API test uses one harness.
    WSL only: importing cockpit.server loads FAISS (~30 s). Mark such tests @pytest.mark.wsl.
    """
    monkeypatch.setenv("ASTRA_SESSIONS_DIR", str(tmp_path / "sessions"))
    try:
        from fastapi.testclient import TestClient

        from cockpit import server as cockpit_server
    except (FileNotFoundError, ImportError) as exc:
        # Same environmental gate as the module-level guards, but the cockpit import
        # happens here rather than at import time, so it must be caught here too —
        # otherwise every API test ERRORS instead of skipping.
        pytest.skip(f"{SERVICES_UNAVAILABLE} ({type(exc).__name__}: {exc})")
    return TestClient(cockpit_server.app)


@pytest.fixture()
def stage1_session(cockpit_client):
    """(client, session_id) for a session that exists and has a plan (S0-11).

    /api/bootstrap with no session creates the deterministic demo session, which is the
    cheapest way to get a real session id without an LLM or OpenVSP. Stage-1 tests need
    an id they can name explicitly, because mutating POSTs no longer accept a missing
    one (S1-A2).
    """
    response = cockpit_client.get("/api/bootstrap")
    assert response.status_code == 200, response.text
    return cockpit_client, response.json()["session_id"]


@pytest.fixture()
def sessions_root(tmp_path, monkeypatch) -> Path:
    """Isolated ASTRA_SESSIONS_DIR for tests that touch the store directly.

    Never let a test write into the developer's real runs/design_sessions.
    """
    root = tmp_path / "sessions"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ASTRA_SESSIONS_DIR", str(root))
    return root
