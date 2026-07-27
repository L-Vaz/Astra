#!/usr/bin/env python3
"""Run one existing OpenFOAM case through Foam-Agent's local-run service."""

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("case_dir")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()

    sys.path.insert(0, str(Path(args.repo) / "src"))
    from services.run_local import run_allrun_and_collect_errors

    errors = run_allrun_and_collect_errors(args.case_dir, timeout=args.timeout)
    print("RESULT_JSON=" + json.dumps(errors))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
