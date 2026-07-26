"""Run the Node parity checks under tests/js/ as part of the Python suite (S0-4).

The cockpit ships browser-side re-implementations of Python domain maths — the
atmosphere lookup (`static/atmosphere.js`), the aero mirror (`static/aero_mirror.js`),
and the planform/station geometry (`static/cockpit.js`, `static/stage1.js`). Those
copies can drift from `domain_packs/bwb/` silently, because nothing in the browser
imports Python.

`tests/js/atmosphere_parity.mjs` was written to catch exactly that, and then nothing
ever executed it: no reference in pyproject.toml, pytest, verification/, or any doc. An
unrun test is worse than no test, because it looks like coverage.

This module discovers every `tests/js/*.mjs` and runs it under Node, so future parity
fixtures are adopted automatically by dropping a file in that directory. Each script is
expected to exit 0 on success and non-zero with a readable message on failure — the
convention `assert` from `node:assert/strict` already gives.
"""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parent.parent
_JS_DIR = Path(__file__).parent / "js"
_SCRIPTS = sorted(_JS_DIR.glob("*.mjs"))

# Node lives on the Windows host in this project; the WSL FoamAgent env does not have
# it. Skipping is honest — the summary line says so — but it means the parity layer is
# only enforced on the interpreter that can reach Node.
_NODE = shutil.which("node")


def test_js_parity_directory_is_not_empty():
    """Guard the guard: if the fixtures vanish, the parametrized test below would
    silently collect nothing and still report green."""
    assert _SCRIPTS, f"no *.mjs parity fixtures found in {_JS_DIR}"


@pytest.mark.js
@pytest.mark.skipif(_NODE is None, reason="node not on PATH; JS parity not enforced here")
@pytest.mark.parametrize("script", _SCRIPTS, ids=lambda p: p.name)
def test_js_parity_script(script: Path):
    proc = subprocess.run(
        [_NODE, str(script)],
        capture_output=True, text=True, timeout=120,
        cwd=str(script.parent.parent.parent),
    )
    if proc.returncode != 0:
        sys.stdout.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        pytest.fail(
            f"{script.name} failed (exit {proc.returncode}).\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )


def test_planform_fixture_matches_python():
    """The Python side of the one cross-language boundary (S7-I1).

    The .mjs files above assert the JS against a committed fixture. This asserts the
    FIXTURE against schema.py, so a Python change that nobody mirrored into
    planform.js fails here — on the Python side, where the change was made — instead
    of waiting for someone to run node.
    """
    sys.path.insert(0, str(_REPO_ROOT / "verification"))
    import generate_planform_fixture as gen

    with open(gen.FIXTURE_PATH) as f:
        committed = f.read()
    assert committed == gen.serialise(gen.build()), (
        "tests/js/planform_fixture.json no longer matches schema.py. Regenerate with "
        "`python verification/generate_planform_fixture.py --write` and make sure "
        "src/cockpit/static/planform.js mirrors the same change.")
