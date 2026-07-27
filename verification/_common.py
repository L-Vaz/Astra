"""Shared helpers for Astra verification scripts (V1 golden / V2 revisions / V3 gates)."""
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (REPO_ROOT, os.path.join(REPO_ROOT, "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

# OpenFOAM rejects paths containing spaces (fileName::stripInvalid), and a symlink
# does not save you — Python's getcwd() resolves it to the physical path. So when the
# checkout sits on a space-containing path, everything OpenFOAM touches goes to
# ~/astra_runs instead. Override either case with ASTRA_RUNS_DIR.
RUNS_DIR = os.environ.get("ASTRA_RUNS_DIR") or (
    os.path.join(REPO_ROOT, "runs") if " " not in REPO_ROOT
    else os.path.expanduser("~/astra_runs"))
os.makedirs(RUNS_DIR, exist_ok=True)


class Table:
    """Collects stage results, prints a PASS/FAIL table, computes exit code."""

    def __init__(self, title: str):
        self.title = title
        self.rows = []  # (stage, status, detail)

    def add(self, stage: str, ok, detail: str = ""):
        status = {True: "PASS", False: "FAIL", None: "SKIP"}[ok]
        self.rows.append((stage, status, detail))
        print(f"  [{status}] {stage}" + (f" — {detail}" if detail else ""))

    def finish(self) -> int:
        failed = [r for r in self.rows if r[1] == "FAIL"]
        print("\n" + "=" * 72)
        print(f"{self.title}: {'PASS' if not failed else 'FAIL'} "
              f"({sum(1 for r in self.rows if r[1] == 'PASS')} passed, "
              f"{len(failed)} failed, "
              f"{sum(1 for r in self.rows if r[1] == 'SKIP')} skipped/phase-2-pending)")
        for stage, status, detail in self.rows:
            print(f"  {status:4}  {stage}" + (f" — {detail}" if detail else ""))
        print("=" * 72)
        return 1 if failed else 0
