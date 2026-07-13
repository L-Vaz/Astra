"""Simulation stage service — runs the EXISTING Foam-Agent graph, unchanged.

Mechanism: the approved mesh (.msh from the mesh stage) is handed to
src/main.py via --custom_mesh_path (the tandem-wing precedent: the meshing
node converts it with gmshToFoam, the mesh is locked, the input writer reads
the real patch names). The requirement text is the intent stage's enriched
requirement plus a mesh/flow paragraph pinned to the approved mesh and the
VSPAERO trim condition. Nothing in the graph is modified.
"""
import math
import os
import subprocess
import sys
from typing import Optional

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_MAIN = os.path.join(_REPO_ROOT, "src", "main.py")
FOAM_BASHRC = os.environ.get("FOAM_BASHRC", "/opt/openfoam10/etc/bashrc")


def compose_requirement(enriched_requirement: str, cruise_speed_ms: float,
                        alpha_deg: float, iterations: int = 600) -> str:
    ux = cruise_speed_ms * math.cos(math.radians(alpha_deg))
    uz = cruise_speed_ms * math.sin(math.radians(alpha_deg))
    return enriched_requirement + f"""
Mesh (IMPORTANT): a custom mesh file is provided to this workflow - use the
provided custom mesh file as-is. Do NOT generate, re-create, or modify the
mesh, and do NOT write any mesh-generation script. The converted mesh has
exactly these patches: inlet, outlet, farfield, aircraft (the aircraft patch
is already type wall). The run script should run checkMesh and then the
solver.

Flow setup: freestream at {alpha_deg:.2f} deg angle of attack, i.e. inlet
velocity ({ux:.4f} 0 {uz:.4f}) m/s. Boundary conditions: inlet fixedValue
velocity with zeroGradient pressure; outlet fixedValue pressure 0 with
zeroGradient velocity (inletOutlet for turbulence fields); farfield slip;
aircraft no-slip wall with nutkWallFunction / kqRWallFunction /
omegaWallFunction wall functions.

Numerics (IMPORTANT - the mesh is an unstructured tetrahedral mesh with max
non-orthogonality near 85 degrees, so limited and bounded schemes are
mandatory or the run will diverge): laplacianSchemes default Gauss linear
limited corrected 0.33; snGradSchemes default limited corrected 0.33;
gradSchemes cellLimited Gauss linear 1 for grad(U), grad(k), grad(omega);
divSchemes: div(phi,U) bounded Gauss linearUpwind grad(U), div(phi,k)
bounded Gauss upwind, div(phi,omega) bounded Gauss upwind. SIMPLE with
nNonOrthogonalCorrectors 2, relaxation factors p 0.3, U 0.5, k 0.3,
omega 0.3.

Run control: steady SIMPLE for {iterations} iterations (endTime {iterations},
deltaT 1, writeInterval {iterations}). The forceCoeffs function object must
use liftDir (-sin({alpha_deg:.2f} deg) 0 cos({alpha_deg:.2f} deg)), dragDir
(cos({alpha_deg:.2f} deg) 0 sin({alpha_deg:.2f} deg)), pitchAxis (0 1 0),
and magUInf {cruise_speed_ms:.2f}. Write forceCoeffs, residuals and yPlus
function-object output every 10 iterations.
"""


class SimulationFailed(Exception):
    def __init__(self, message: str, case_dir: Optional[str] = None,
                 log_tail: str = ""):
        super().__init__(message)
        self.case_dir = case_dir
        self.log_tail = log_tail


def run_simulation(requirement_text: str, msh_path: str, workdir: str,
                   timeout_s: int = 10800) -> dict:
    """Invoke the existing Foam-Agent graph (src/main.py) as a subprocess with
    OpenFOAM sourced. Returns {case_dir, success, log_path}. Raises
    SimulationFailed when the graph reports failure (honest exit status)."""
    os.makedirs(workdir, exist_ok=True)
    case_dir = os.path.join(workdir, "case")
    req_path = os.path.join(workdir, "requirement.txt")
    with open(req_path, "w", encoding="utf-8") as f:
        f.write(requirement_text)

    log_path = os.path.join(workdir, "foam_agent.log")
    cmd = (f"source {FOAM_BASHRC} >/dev/null 2>&1; "
           f"cd {_quote(os.path.join(_REPO_ROOT, 'src'))} && "
           f"{_quote(sys.executable)} main.py "
           f"--prompt_path {_quote(req_path)} "
           f"--output_dir {_quote(case_dir)} "
           f"--custom_mesh_path {_quote(msh_path)}")
    with open(log_path, "w") as logf:
        proc = subprocess.run(["bash", "-lc", cmd], stdout=logf,
                              stderr=subprocess.STDOUT, timeout=timeout_s)

    with open(log_path, encoding="utf-8", errors="replace") as f:
        tail = f.read()[-3000:]
    if proc.returncode != 0:
        raise SimulationFailed(
            f"Foam-Agent graph reported failure (exit {proc.returncode})",
            case_dir=case_dir, log_tail=tail)
    return {"case_dir": case_dir, "success": True, "log_path": log_path}


def _quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"
