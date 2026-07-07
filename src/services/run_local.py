import os
import re
import shutil
import tempfile
from typing import List, Any
from models import RunIn, RunOut
from utils import remove_files, remove_file, remove_numeric_folders, run_command, check_foam_errors

def _run_allrun_in_directory(
    case_dir: str,
    timeout: int = 3600,
    max_retries: int = 1
) -> List[str]:
    """
    Execute the Allrun script and collect any error logs from the simulation.
    
    This function runs the Allrun script in the specified case directory,
    captures the output and error streams, and parses the results to identify
    any OpenFOAM errors that occurred during execution.
    
    Args:
        case_dir (str): Directory path containing the OpenFOAM case and Allrun script
        timeout (int, optional): Maximum execution time in seconds. Defaults to 3600.
        max_retries (int, optional): Maximum number of retry attempts. Defaults to 3.
    
    Returns:
        List[str]: List of error messages found in the simulation logs.
                 Empty list indicates successful execution with no errors.
    
    Raises:
        FileNotFoundError: If Allrun script does not exist in case_dir
        RuntimeError: If Allrun script execution fails repeatedly
        TimeoutError: If execution exceeds timeout limit
    
    Example:
        >>> errors = run_allrun_and_collect_errors(
        ...     case_dir="/path/to/case",
        ...     timeout=1800,
        ...     max_retries=2
        ... )
        >>> if not errors:
        ...     print("Simulation completed successfully")
        >>> else:
        ...     print(f"Found {len(errors)} errors")
    """
    allrun_file_path = os.path.join(case_dir, "Allrun")
    if not os.path.exists(allrun_file_path):
        return [f"Allrun script not found at {allrun_file_path}"]
    
    out_file = os.path.join(case_dir, "Allrun.out")
    err_file = os.path.join(case_dir, "Allrun.err")

    # Cleanup
    remove_files(case_dir, prefix="log")
    remove_file(err_file)
    remove_file(out_file)
    remove_numeric_folders(case_dir)

    last_error_logs = []

    # Run with retries
    for attempt in range(1, max_retries + 1):
        print(f"Running Allrun (attempt {attempt}/{max_retries})")
        return_code = run_command(
            allrun_file_path,
            out_file,
            err_file,
            case_dir,
            timeout,
        )

        # A nonzero process status is authoritative. OpenFOAM may never have
        # started, so log-file scanning alone cannot detect this failure.
        if return_code != 0:
            try:
                with open(err_file, 'r', encoding='utf-8', errors='replace') as f:
                    error_content = f.read().strip()
            except OSError as exc:
                error_content = f"Could not read Allrun.err: {exc}"

            if not error_content:
                error_content = "Allrun failed without producing stderr output."

            error_logs = [{
                "file": "Allrun.err",
                "error_type": "execution_failure",
                "return_code": return_code,
                "error_content": error_content,
            }]

            # OpenFOAM writes fatal errors to per-command log files and
            # stdout, not stderr; the exit code alone gives the reviewer
            # nothing actionable. Forward the real failure detail too.
            error_logs.extend(check_foam_errors(case_dir))
            if len(error_logs) == 1:
                try:
                    with open(out_file, 'r', encoding='utf-8', errors='replace') as f:
                        out_tail = f.read()[-3000:].strip()
                except OSError:
                    out_tail = ""
                if out_tail:
                    error_logs.append({
                        "file": "Allrun.out",
                        "error_type": "execution_failure_context",
                        "error_content": f"Tail of Allrun.out:\n{out_tail}",
                    })
        else:
            error_logs = check_foam_errors(case_dir)

        # A zero return code is not sufficient if no CFD outputs exist.
        if not error_logs:
            mesh_dir = os.path.join(case_dir, "constant", "polyMesh")
            if not os.path.isdir(mesh_dir):
                error_logs = [{
                    "file": "constant/polyMesh",
                    "error_type": "missing_mesh",
                    "error_content": (
                        "Allrun returned exit code 0, but no OpenFOAM mesh "
                        "directory was generated."
                    ),
                }]

        if not error_logs:
            result_times = []
            for name in os.listdir(case_dir):
                path = os.path.join(case_dir, name)
                if not os.path.isdir(path):
                    continue
                try:
                    time_value = float(name)
                except ValueError:
                    continue
                if time_value > 0:
                    result_times.append(time_value)

            if not result_times:
                error_logs = [{
                    "file": case_dir,
                    "error_type": "missing_results",
                    "error_content": (
                        "Allrun returned exit code 0, but no simulation "
                        "timestep greater than zero was generated."
                    ),
                }]
            else:
                control_dict = os.path.join(case_dir, "system", "controlDict")
                if os.path.isfile(control_dict):
                    with open(
                        control_dict,
                        'r',
                        encoding='utf-8',
                        errors='replace',
                    ) as f:
                        control_content = f.read()

                    end_time_match = re.search(
                        r"^\s*endTime\s+"
                        r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)"
                        r"\s*;",
                        control_content,
                        re.MULTILINE,
                    )
                    if end_time_match:
                        expected_end_time = float(end_time_match.group(1))
                        latest_time = max(result_times)
                        tolerance = max(1e-9, abs(expected_end_time) * 1e-9)
                        if latest_time + tolerance < expected_end_time:
                            error_logs = [{
                                "file": "system/controlDict",
                                "error_type": "incomplete_results",
                                "expected_end_time": expected_end_time,
                                "latest_time": latest_time,
                                "error_content": (
                                    f"Latest result time {latest_time} did not "
                                    f"reach requested endTime {expected_end_time}."
                                ),
                            }]

        if len(error_logs) == 0:
            return []

        last_error_logs = error_logs
        if attempt < max_retries:
            print("Allrun reported errors; retrying after cleanup...")
            remove_files(case_dir, prefix="log")
            remove_file(err_file)
            remove_file(out_file)
            remove_numeric_folders(case_dir)

    return last_error_logs


def run_allrun_and_collect_errors(
    case_dir: str,
    timeout: int = 3600,
    max_retries: int = 1,
) -> List[str]:
    """Run a case, staging it when OpenFOAM cannot accept its path.

    Foundation OpenFOAM rejects case directory names containing whitespace.
    Foam-Agent still accepts those user-facing paths: it runs a temporary copy
    from a safe generated directory and copies all logs and results back.
    """
    case_dir = os.path.abspath(case_dir)
    allrun_file_path = os.path.join(case_dir, "Allrun")
    if not os.path.isfile(allrun_file_path):
        return [f"Allrun script not found at {allrun_file_path}"]

    if not any(character.isspace() for character in case_dir):
        return _run_allrun_in_directory(case_dir, timeout, max_retries)

    # Remove stale run products before creating the staged input copy.
    remove_files(case_dir, prefix="log")
    remove_file(os.path.join(case_dir, "Allrun.err"))
    remove_file(os.path.join(case_dir, "Allrun.out"))
    remove_numeric_folders(case_dir)

    staging_root = tempfile.mkdtemp(prefix="foamagent_case_")
    staged_case_dir = os.path.join(staging_root, "case")
    shutil.copytree(case_dir, staged_case_dir)
    print(f"Staging OpenFOAM case at {staged_case_dir}")

    try:
        return _run_allrun_in_directory(
            staged_case_dir,
            timeout,
            max_retries,
        )
    finally:
        # Preserve both successful results and failure evidence for review.
        shutil.copytree(staged_case_dir, case_dir, dirs_exist_ok=True)
        shutil.rmtree(staging_root, ignore_errors=True)


def run_simulation_local(
    case_id: str,
    case_dir: str,
    timeout: int = 3600,
    max_retries: int = 1
) -> RunOut:
    """
    Run OpenFOAM simulation locally and return execution status.
    
    This function executes the Allrun script in the specified case directory
    and returns the execution status along with any job information.
    For local execution, job_id is always None.
    
    Args:
        case_id (str): Unique identifier for the case
        case_dir (str): Directory path containing the OpenFOAM case
        timeout (int, optional): Maximum execution time in seconds. Defaults to 3600.
        max_retries (int, optional): Maximum number of retry attempts. Defaults to 3.
    
    Returns:
        RunOut: Contains:
            - job_id (None): Always None for local execution
            - status (str): Execution status ("completed" or "failed")
    
    Raises:
        FileNotFoundError: If case directory or Allrun script does not exist
        RuntimeError: If simulation execution fails
    
    Example:
        >>> result = run_simulation_local(
        ...     case_id="test_case",
        ...     case_dir="/path/to/case",
        ...     timeout=1800
        ... )
        >>> print(f"Simulation status: {result.status}")
    """
    errors = run_allrun_and_collect_errors(case_dir, timeout, max_retries)
    status = "completed" if len(errors) == 0 else "failed"
    return RunOut(job_id=None, status=status)


