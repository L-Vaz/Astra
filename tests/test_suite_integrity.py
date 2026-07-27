"""Tests about the test suite itself (S0-1, S0-3, S0-4, S0-12).

Every other module here asserts something about the product. This one asserts that the
suite's own report can be believed, because before Stage 0 it could not: `pytest tests/`
collected **zero** tests — two modules raised on import, pytest aborted collection, and
the run still looked quiet. Quiet read as fine.

Four failure modes are locked here, each of which previously produced a green run:

1. A module stops importing and its tests vanish (`test_collection_reports_no_errors`).
2. A module skips itself for a reason nobody declared, so the loss is invisible
   (`test_no_skip_is_anonymous`).
3. A parity fixture exists but nothing executes it — `tests/js/atmosphere_parity.mjs`
   sat unrun since it was written (`test_every_js_fixture_is_collected`).
4. An `xfail` reason drifts away from the plan it cites, so the backlog encoded in the
   suite stops matching the backlog on paper
   (`test_every_xfail_reason_names_a_plan_item`).

Cost note: the first two shell out to a real `--collect-only` run. On the Windows python
that is a couple of seconds; in the WSL env, collection imports the services layer
(FAISS, ~30 s). It is paid once per session — the fixture is module-scoped.
"""
import ast
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parent.parent
_TESTS_DIR = Path(__file__).parent
_PLAN = (_REPO_ROOT / "docs" / "superpowers" / "plans"
         / "2026-07-25-stage1-hardening.md")

# Every reason a module is allowed to remove itself from the run. A skip outside this
# list is anonymous: the count drops and nothing says why. Adding an entry here is the
# deliberate act of declaring a new environmental gate.
SANCTIONED_SKIP_REASONS = (
    "services layer unavailable",        # conftest.SERVICES_UNAVAILABLE
    "node not on PATH",                  # tests/test_js_parity.py
    "aerosandbox not installed",         # the ASB fast-aero tier (py3.12 env only)
)

# Work-item IDs as they appear in the plan: S<stage>-<letter(s)><number>, plus the
# S7-CAP-<n> capability form.
_PLAN_ID = re.compile(r"S\d-[A-Z]+-?\d+")
# The plan writes items that share a fix as one compound heading — "S6-F9 / F10",
# "S3-E2 / E3" — so the literal string "S6-F10" appears nowhere in the document even
# though the item exists. Expand those before checking membership, or this test rejects
# a perfectly valid citation.
_PLAN_ID_RUN = re.compile(r"S(\d)-([A-Z]+-?\d+)((?:\s*/\s*[A-Z]+-?\d+)*)")


def _plan_item_ids() -> set:
    ids = set()
    for match in _PLAN_ID_RUN.finditer(_PLAN.read_text(encoding="utf-8")):
        stage, first, continuation = match.groups()
        ids.add(f"S{stage}-{first}")
        for extra in re.findall(r"[A-Z]+-?\d+", continuation or ""):
            ids.add(f"S{stage}-{extra}")
    return ids


@pytest.fixture(scope="module")
def collect_run() -> subprocess.CompletedProcess:
    """One real collection, in a subprocess, reused by every check below."""
    return subprocess.run(
        [sys.executable, "-m", "pytest", str(_TESTS_DIR),
         "--collect-only", "-q", "-rs", "-p", "no:cacheprovider"],
        cwd=str(_REPO_ROOT), capture_output=True, text=True, timeout=900,
    )


# --- 1. configuration ---------------------------------------------------------------

def test_pytest_config_declares_xfail_strict(pytestconfig):
    """`xfail_strict` is what makes the xfail backlog a backlog.

    Without it, an xfail that starts passing is reported as XPASS and the run stays
    green — so a fixed defect looks identical to an unfixed one, and the suite silently
    stops being the record of what is broken.
    """
    assert pytestconfig.getini("xfail_strict") is True


def test_pytest_config_rejects_unknown_markers(pytestconfig):
    """`--strict-markers` turns a mistyped marker into an error. Without it,
    `@pytest.mark.wsl_only` is a silent no-op and the test runs where it cannot pass."""
    assert "--strict-markers" in pytestconfig.getini("addopts")


def test_collection_floor_is_set(pytestconfig):
    """Guard the guard: conftest only fails a shrinking run if the floor is non-zero."""
    assert int(pytestconfig.getini("minimum_collected_tests")) > 0


# --- 2. the suite does not silently shrink ------------------------------------------

def test_collection_reports_no_errors(collect_run):
    """A single un-importable module used to take the entire suite down with it."""
    assert collect_run.returncode == 0, (
        "collection failed — a module cannot be imported, so its tests are gone:\n"
        f"{collect_run.stdout}\n{collect_run.stderr}")
    assert " error" not in collect_run.stdout.splitlines()[-1]


def test_no_skip_is_anonymous(collect_run):
    """Every module-level skip must state one of the declared environmental reasons.

    A skip is a silent subtraction from the suite. Naming the reason is what lets the
    run summary distinguish 'this environment cannot run these' from 'these tests
    quietly stopped existing'.
    """
    unexplained = [
        line for line in collect_run.stdout.splitlines()
        if line.startswith("SKIPPED")
        and not any(r in line for r in SANCTIONED_SKIP_REASONS)
    ]
    assert not unexplained, (
        "skip reason(s) not declared in SANCTIONED_SKIP_REASONS — either name the new "
        f"environmental gate there or fix the skip:\n" + "\n".join(unexplained))


# --- 3. no orphaned parity fixture --------------------------------------------------

def test_every_js_fixture_is_collected(collect_run):
    """`tests/js/*.mjs` files must all be adopted by the parametrized runner.

    This is what stops the *next* orphan: `atmosphere_parity.mjs` was written to catch
    browser/Python drift and then never ran, because wiring it in was a separate manual
    step nobody took. Now the wiring is a glob, and this asserts the glob still sees
    every file.
    """
    on_disk = {p.name for p in (_TESTS_DIR / "js").glob("*.mjs")}
    collected = set(re.findall(r"test_js_parity_script\[([^\]]+)\]",
                               collect_run.stdout))
    assert on_disk, "no .mjs parity fixtures found at all"
    assert collected == on_disk, (
        f"parity fixtures not collected: {sorted(on_disk - collected)}; "
        f"collected ids with no file: {sorted(collected - on_disk)}")


# --- 3b. one agent-guidance file, under two names -----------------------------------

def test_agent_guidance_files_are_identical():
    """`CLAUDE.md` and `AGENTS.md` are one document read by two different tools.

    The rule to keep them byte-identical was written in the document itself and enforced
    by nothing; worse, `CLAUDE.md` was gitignored, so a fresh clone had guidance under
    one name only and any drift was invisible to review. Both are tracked now, and this
    turns "please keep them in sync" into a failing build.
    """
    claude = _REPO_ROOT / "CLAUDE.md"
    agents = _REPO_ROOT / "AGENTS.md"
    assert claude.is_file() and agents.is_file(), (
        "both CLAUDE.md and AGENTS.md must exist at the repository root")
    if claude.read_bytes() != agents.read_bytes():
        pytest.fail("CLAUDE.md and AGENTS.md have drifted. They are the same document "
                    "under two names — edit one and copy it over the other, in the "
                    "same commit. Do not resolve this by deleting either.")


def test_no_second_agent_guidance_file():
    """Guidance in a subdirectory is guidance nobody at the root will read, and it is
    how the single file starts to fork. Keep one, at the root."""
    strays = [p.relative_to(_REPO_ROOT).as_posix()
              for name in ("CLAUDE.md", "AGENTS.md")
              for p in _REPO_ROOT.rglob(name)
              if p.parent != _REPO_ROOT
              and "build" not in p.parts and ".git" not in p.parts]
    assert not strays, (
        f"agent guidance must live only at the repository root; found: {strays}")


# --- 3c. one grounding protocol, read by two different agents -----------------------

_CLAUDE_SKILL = _REPO_ROOT / ".claude" / "skills" / "grounding.md"
_CODEX_SKILL = _REPO_ROOT / ".agents" / "skills" / "grounding" / "SKILL.md"

# Each agent reads its hook registration from its own file. The *script* is deliberately
# shared: one behaviour, not two that can drift.
_HOOK_REGISTRATIONS = (
    _REPO_ROOT / ".claude" / "settings.json",   # Claude Code
    _REPO_ROOT / ".codex" / "hooks.json",       # Codex
)


def test_grounding_skill_mirrors_are_identical():
    """The engineering-discipline protocol is one document under two names, for the same
    reason `CLAUDE.md` and `AGENTS.md` are: Claude Code scans `.claude/skills/*.md`,
    Codex scans `.agents/skills/<name>/SKILL.md`. A protocol that says "grade every
    number" in one tool and something older in the other is worse than having it once.
    """
    assert _CLAUDE_SKILL.is_file() and _CODEX_SKILL.is_file(), (
        f"both grounding skill files must exist: {_CLAUDE_SKILL}, {_CODEX_SKILL}")
    if _CLAUDE_SKILL.read_bytes() != _CODEX_SKILL.read_bytes():
        pytest.fail(
            "the Claude and Codex grounding skills have drifted. They are one document "
            "under two names — edit one and copy it over the other, in the same commit. "
            "Do not resolve this by deleting either.")


@pytest.mark.parametrize("registration", _HOOK_REGISTRATIONS,
                         ids=lambda p: p.parent.name)
def test_hook_registration_points_at_an_existing_script(registration):
    """Both agents' `UserPromptSubmit` hooks must name a script that is actually there.

    The hook is the only layer of the protocol that cannot fade mid-session, and it fails
    *silently*: rename the script, update one registration, forget the other, and that
    agent simply stops being reminded. Nothing in a normal run would say so — the same
    quiet-reads-as-fine failure this module exists to catch.
    """
    assert registration.is_file(), f"missing hook registration: {registration}"
    events = json.loads(registration.read_text(encoding="utf-8"))["hooks"]
    commands = [entry["command"]
                for group in events["UserPromptSubmit"]
                for entry in group["hooks"]]
    assert commands, f"{registration} registers no UserPromptSubmit command"
    for command in commands:
        script = _REPO_ROOT / command.split()[-1]
        assert script.is_file(), (
            f"{registration.relative_to(_REPO_ROOT)} runs {command!r}, but "
            f"{script.relative_to(_REPO_ROOT)} does not exist")


# --- 4. the xfail backlog cannot drift from the plan --------------------------------

def _xfail_markers():
    """(file, lineno, reason) for every xfail marker in the suite, via AST.

    Static parsing rather than collection introspection so that markers inside
    `pytest.param(..., marks=...)` and modules skipped in this environment are covered
    too — the backlog must be checked everywhere, not only where it can run.
    """
    out = []
    for path in sorted(_TESTS_DIR.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr == "xfail":
                reason = next(
                    (kw.value.value for kw in node.keywords
                     if kw.arg == "reason" and isinstance(kw.value, ast.Constant)),
                    None)
                out.append((path.name, node.lineno, reason))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for dec in node.decorator_list:
                    # bare `@pytest.mark.xfail` — no call, so no reason
                    if isinstance(dec, ast.Attribute) and dec.attr == "xfail":
                        out.append((path.name, dec.lineno, None))
    return out


def test_the_suite_has_an_xfail_backlog():
    """If this reaches zero, either every defect is fixed or the backlog was deleted.
    Both deserve a failing test and a conversation."""
    assert _xfail_markers(), "no xfail markers found — the encoded backlog is gone"


@pytest.mark.parametrize("marker", _xfail_markers(),
                         ids=lambda m: f"{m[0]}:{m[1]}")
def test_every_xfail_reason_names_a_plan_item(marker):
    """`reason` must start with the work-item ID that will flip this marker.

    That string is the join between the suite and the plan: `pytest -rx` prints the live
    backlog, and each line points at the item whose landing turns it green. An xfail
    without one is a defect nobody owns.
    """
    filename, lineno, reason = marker
    where = f"{filename}:{lineno}"
    assert reason, f"{where}: xfail has no reason — it must name a plan item"

    head, sep, rest = reason.partition(":")
    assert sep and rest.strip(), (
        f"{where}: reason must read '<plan item ID>: <one line>', got {reason!r}")
    ids = _PLAN_ID.findall(head)
    assert ids, (
        f"{where}: reason must START with a plan item ID like 'S4-C1', got {head!r}")

    known = _plan_item_ids()
    missing = [i for i in ids if i not in known]
    assert not missing, (
        f"{where}: {missing} appear(s) in no plan item — the suite and "
        f"{_PLAN.name} have drifted apart. Either cite the right item or add it "
        f"to the plan; do not delete the marker.")
