# Environment and numerical caveats

Read this before changing setup, runtime paths, OpenVSP integration, meshing, OpenFOAM requirements, solver schemes, post-processing, or result thresholds.

## Runtime environment

- Primary runtime: WSL Ubuntu 22.04, Foundation OpenFOAM v10 at `/opt/openfoam10`, and conda env `FoamAgent` on Python 3.12.
- OpenVSP 3.49 uses its Python 3.10-only binding through `/opt/vsp-venv/bin/python` (`VSP_PYTHON` override). Never import it into the Python 3.12 process.
- Source OpenFOAM from `/opt/openfoam10/etc/bashrc` (`FOAM_BASHRC` override).
- Put every OpenFOAM-touched runtime artifact under `~/astra_runs` (`ASTRA_RUNS_DIR` override). The Windows repository path contains spaces and breaks OpenFOAM even through a resolving symlink.

## Mesh routing and conversion

Requirement text must say that a custom mesh is provided and must be used as-is. Calling it a “Gmsh mesh” can flip the LLM router into script-generation mode. `copy_custom_mesh` strips markdown fences, supplies a deterministic `controlDict` fallback, surfaces `gmshToFoam` errors, and converts solid patches from generic `patch` to `wall` before the input writer reads them. Preserve these safeguards.

For BWB farfield meshing, use Gmsh Delaunay (`Algorithm3D 1`) and gate on nonzero cells. Use the watertight OpenVSP STL triangles directly for the boundary, because reparametrization damages the sharp trailing edge. Run Netgen optimization and export MSH v2.2 for `gmshToFoam`.

## Solver stability

The verified tetrahedral setup uses limited corrected laplacian and surface-normal schemes at 0.33, cell-limited gradients, bounded divergence schemes, two SIMPLE non-orthogonal correctors, and relaxation p 0.3 / U 0.5 / k and omega 0.3. Unlimited corrected schemes diverged on the high-non-orthogonality mesh. Treat changes as numerical experiments and rerun verification.

The evaluator injects missing `forceCoeffs` and `yPlus` function objects and recomputes them post-hoc from saved fields. Field rendering must select the latest time directory containing `U`, because function objects can create field-less time directories.

The coarse mesh is a screening tier: it historically under-predicted lift versus VSPAERO. Do not silently tighten plausibility thresholds without higher-fidelity evidence. Sizing rules and acceptance thresholds are engineering judgments isolated in `defaults.py`, `schema.py`, and `evaluate.py`; numerical schemes are documented against OpenFOAM sources. See `reports/verification-2026-07-10/references.md`.
