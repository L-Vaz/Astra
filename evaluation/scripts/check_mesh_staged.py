#!/usr/bin/env python3
"""Run checkMesh on a temporary safe-path copy of an OpenFOAM case."""

import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("case", type=Path)
    parser.add_argument("--log-name", default="independent_checkMesh.log")
    args = parser.parse_args()

    case = args.case.resolve()
    with tempfile.TemporaryDirectory(prefix="foamagent_checkmesh_") as root:
        staged = Path(root) / "case"
        shutil.copytree(case, staged)
        result = subprocess.run(
            ["checkMesh"],
            cwd=staged,
            capture_output=True,
            text=True,
        )
        log = result.stdout + result.stderr
        (case / args.log_name).write_text(log, encoding="utf-8")
        print(log)
        return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
