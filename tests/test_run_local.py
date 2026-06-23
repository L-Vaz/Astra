import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from services.run_local import run_allrun_and_collect_errors
from utils import run_command


def _configure_fake_openfoam(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    openfoam_dir = tmp_path / "fake openfoam"
    bashrc = openfoam_dir / "etc" / "bashrc"
    bashrc.parent.mkdir(parents=True)
    bashrc.write_text("# test OpenFOAM environment\n", encoding="utf-8")
    monkeypatch.setenv("WM_PROJECT_DIR", str(openfoam_dir))


def _write_allrun(case_dir: Path, content: str) -> Path:
    allrun = case_dir / "Allrun"
    allrun.write_text(content, encoding="utf-8", newline="\n")
    return allrun


def test_run_command_handles_paths_with_spaces(monkeypatch, tmp_path):
    _configure_fake_openfoam(monkeypatch, tmp_path)
    case_dir = tmp_path / "case with spaces"
    case_dir.mkdir()
    allrun = _write_allrun(case_dir, "#!/usr/bin/env bash\necho executed\n")
    out_file = case_dir / "Allrun.out"
    err_file = case_dir / "Allrun.err"

    return_code = run_command(
        str(allrun),
        str(out_file),
        str(err_file),
        str(case_dir),
        10,
    )

    assert return_code == 0
    assert out_file.read_text(encoding="utf-8").strip() == "executed"
    assert err_file.read_text(encoding="utf-8") == ""


def test_run_command_supports_allrun_self_cd_in_path_with_spaces(
    monkeypatch,
    tmp_path,
):
    _configure_fake_openfoam(monkeypatch, tmp_path)
    case_dir = tmp_path / "case with spaces"
    case_dir.mkdir()
    allrun = _write_allrun(
        case_dir,
        "#!/usr/bin/env bash\n"
        "cd ${0%/*} || exit 1\n"
        "echo executed\n",
    )
    out_file = case_dir / "Allrun.out"
    err_file = case_dir / "Allrun.err"

    return_code = run_command(
        str(allrun),
        str(out_file),
        str(err_file),
        str(case_dir),
        10,
    )

    assert return_code == 0
    assert out_file.read_text(encoding="utf-8").strip() == "executed"
    assert err_file.read_text(encoding="utf-8") == ""


def test_nonzero_allrun_exit_reports_stderr(monkeypatch, tmp_path):
    _configure_fake_openfoam(monkeypatch, tmp_path)
    case_dir = tmp_path / "failing case"
    case_dir.mkdir()
    _write_allrun(
        case_dir,
        "#!/usr/bin/env bash\necho 'deliberate failure' >&2\nexit 7\n",
    )

    errors = run_allrun_and_collect_errors(str(case_dir), timeout=10)

    assert errors
    assert errors[0]["file"] == "Allrun.err"
    assert errors[0]["error_type"] == "execution_failure"
    assert errors[0]["return_code"] == 7
    assert "deliberate failure" in errors[0]["error_content"]


def test_missing_allrun_is_reported(tmp_path):
    errors = run_allrun_and_collect_errors(str(tmp_path), timeout=10)

    assert errors
    assert "Allrun script not found" in str(errors[0])


def test_zero_exit_without_openfoam_outputs_is_failure(monkeypatch, tmp_path):
    _configure_fake_openfoam(monkeypatch, tmp_path)
    case_dir = tmp_path / "empty successful command"
    case_dir.mkdir()
    _write_allrun(case_dir, "#!/usr/bin/env bash\nexit 0\n")

    errors = run_allrun_and_collect_errors(str(case_dir), timeout=10)

    assert errors
    assert errors[0]["error_type"] == "missing_mesh"


def test_zero_exit_with_mesh_but_no_result_time_is_failure(monkeypatch, tmp_path):
    _configure_fake_openfoam(monkeypatch, tmp_path)
    case_dir = tmp_path / "mesh only case"
    case_dir.mkdir()
    _write_allrun(
        case_dir,
        "#!/usr/bin/env bash\n"
        "mkdir -p constant/polyMesh\n"
        "mkdir -p 0\n"
        "exit 0\n",
    )

    errors = run_allrun_and_collect_errors(str(case_dir), timeout=10)

    assert errors
    assert errors[0]["error_type"] == "missing_results"


def test_case_with_spaces_is_staged_and_results_are_copied_back(
    monkeypatch,
    tmp_path,
):
    _configure_fake_openfoam(monkeypatch, tmp_path)
    case_dir = tmp_path / "openfoam rejects spaces"
    case_dir.mkdir()
    _write_allrun(
        case_dir,
        "#!/usr/bin/env bash\n"
        "case \"$PWD\" in\n"
        "  *' '*) echo 'invalid OpenFOAM case path' >&2; exit 9 ;;\n"
        "esac\n"
        "mkdir -p constant/polyMesh\n"
        "mkdir -p 1\n"
        "printf 'End\\n' > log.solver\n",
    )

    errors = run_allrun_and_collect_errors(str(case_dir), timeout=10)

    assert errors == []
    assert (case_dir / "constant" / "polyMesh").is_dir()
    assert (case_dir / "1").is_dir()
    assert (case_dir / "log.solver").read_text(encoding="utf-8") == "End\n"


def test_result_time_must_reach_control_dict_end_time(monkeypatch, tmp_path):
    _configure_fake_openfoam(monkeypatch, tmp_path)
    case_dir = tmp_path / "incomplete case"
    case_dir.mkdir()
    control_dict = case_dir / "system" / "controlDict"
    control_dict.parent.mkdir()
    control_dict.write_text("endTime 2;\n", encoding="utf-8")
    _write_allrun(
        case_dir,
        "#!/usr/bin/env bash\n"
        "mkdir -p constant/polyMesh\n"
        "mkdir -p 1\n"
        "printf 'End\\n' > log.solver\n",
    )

    errors = run_allrun_and_collect_errors(str(case_dir), timeout=10)

    assert errors
    assert errors[0]["error_type"] == "incomplete_results"
    assert errors[0]["expected_end_time"] == 2.0
    assert errors[0]["latest_time"] == 1.0
