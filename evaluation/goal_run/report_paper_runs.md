# Paper-prompt reruns of tiers 3 & 4 — 2026-07-07

> **Update, same evening — tier 2 + T5 mini-benchmark (see §T5 below): 10/11 pass.**

Two consecutive passes of the paper's Appendix B.3 (cylinder / Gmsh / pisoFoam) and
B.2 (tandem wing / external .msh / simpleFoam + Spalart-Allmaras) prompts, run
verbatim, after the fix series `7f17e26..0a22956`. Tier 1 (cavity) was not rerun
(passed and Ghia-validated 2026-06-28); tier 2 (pitzDaily) deliberately skipped.

Model: `anthropic / claude-sonnet-4-6`, temperature 0, `max_loop=10` (paper M=10).
Embeddings: local Qwen3-Embedding-0.6B. OpenFOAM 10 (Foundation), WSL2 Ubuntu 22.04.

## Results (all independently verified, not just exit codes)

| Run | Tier | Result | Reviewer loops | Evidence |
|---|---|---|---|---|
| 1 | 3 cylinder | Pass | 0 | time dirs 0→2.0 s (write interval 0.1 s as requested); patches exactly inlet/outlet/topWall/bottomWall/cylinder/frontAndBack; visualization.png |
| 1 | 4 tandem wing | Pass | 5 | 425,080 cells, checkMesh "Mesh OK"; simpleFoam Time=1→10 to `End`, converged; mid-Z velocity PNG |
| 2 | 3 cylinder | Pass | 6 | time dirs to 2.0 s; same 6 patches; solver `End`; visualization.png |
| 2 | 4 tandem wing | Pass | 3 | same 425,080-cell mesh; solver `End` at t=10; visualization.png |

June 28 baseline for the same two cases: 0/2 (mesh with unnamed `patch0`;
external mesh wiped to zero cells by a reviewer-added `fluentMeshToFoam`).

Run roots (WSL): `~/foamagent-goal/paper_run1_20260707_192354`,
`~/foamagent-goal/paper_run2_20260707_193902`. Logs: `logs_paper_run1/`,
`logs_paper_run2/` (plus `logs_paper_run1_interrupted/` — PC shutdown mid-run,
kept because its blind-reviewer trace motivated commit 0a22956).
Artifacts copied to `artifacts/tier{3,4}_paper_run1_visualization.png`.

## What fixed it (commits, this order matters)

1. `7f17e26` — Anthropic `max_tokens=16000`; structured-output retry + JSON
   fallback; temperature 0.
2. `b46a62b` — honest failure status (`<workflow_failed>` + exit 1 when
   error_logs survive); skip visualization on max-loop failure; **meshing node
   passed the whole GraphState dict as the "user requirement" to every Gmsh
   prompt** (root cause of the patch0 mesh) — now passes the requirement text;
   inject real `constant/polyMesh/boundary` patch names into all file
   generation/rewrite prompts; ban mesh regeneration in Allrun for pre-meshed
   cases; `max_loop` 8→10.
3. `0a22956` — forward `check_foam_errors()` + `Allrun.out` tail to the reviewer
   on nonzero Allrun exit (it previously saw only the empty `Allrun.err` and
   looped blind); hard code-level rejection of Allrun rewrites containing
   `gmsh`/`blockMesh`/`snappyHexMesh`/`*ToFoam` when the mesh is locked; give the
   reviewer the mesh-locked context + actual patch list.

## §T5 — Tier 2 + 10-case mini-benchmark (t2t5_run1, same code)

The paper's 110 benchmark prompts are unpublished, so the T5 set was authored in
the paper's described prompt style (scenario, geometry, named boundaries, solver,
physics, time controls), one case per paper physics category, all blockMesh
geometries, solvers verified present in OpenFOAM 10. Prompts:
`prompts/t5_01..t5_10`. Harness: `run_t2_t5.sh`. Logs: `logs_t2t5_run1/`.
Run root (WSL): `~/foamagent-goal/t2t5_run1_20260707_230416`.

| Case | Category / solver | Result | Loops | Wall | Verified |
|---|---|---|---|---|---|
| tier2 | pitzDaily, simpleFoam k-epsilon | Pass | 4 | 4:46 | 1000 iters, End, viz PNG |
| t5_01 | laminar channel, icoFoam | Pass | 0 | 1:15 | t=5, End |
| t5_02 | square obstacle (multi-block hole), pimpleFoam | Pass | 1 | 2:09 | t=10, End |
| t5_03 | flat plate, simpleFoam kOmegaSST | Pass | 3 | 2:43 | 500 iters, End |
| t5_04 | 3D duct, simpleFoam k-epsilon | Pass | 2 | 2:51 | 300 iters, End |
| t5_05 | heat conduction, laplacianFoam | Pass | 0 | 0:59 | t=2000, End |
| t5_06 | buoyant cavity, buoyantFoam | Pass | 0 | 1:38 | t=2, End |
| t5_07 | shallow water basin, shallowWaterFoam | **Fail** | 9 | 4:48 | FPE at t=4.6/20 |
| t5_08 | dam break VOF, interFoam | Pass | 2 | 2:19 | t=1, End |
| t5_09 | Sod shock tube, rhoCentralFoam | Pass | 0 | 1:27 | t=0.007, End |
| t5_10 | counterflow flame, reactingFoam | Pass | 6 | 5:48 | t=0.05, End |

**T5 executable success: 9/10 (90%)** vs the paper's 88.2% over 110 cases.
Tier 2 — the last previously-failing tier — passed in 4 reviewer loops (June:
unfixable; the reviewer never saw the blockMesh error text).

t5_07 failure anatomy (a *quality* failure, not a blind one): the reviewer
sequentially fixed three config errors (gravitationalProperties `rotating`,
fvSchemes `div(phiv,hU)`, fvSolution `hUFinal`), the solver then ran to t=4.6 of
20 s and died of a floating-point exception (Courant 6.5 — the prompt's fixed
dt=0.1 s became unstable as the inflow piled water up). The reviewer is
correctly forbidden from changing prompt-declared parameters, so it could not
reduce dt. Numerical-stability failures like this are the same class that makes
up the paper's own ~12% miss rate.

## Known deviations / follow-ups

- The *initial* Allrun for gmsh cases still re-meshes inside the script
  (gmsh → gmshToFoam) despite the prompt constraint. It regenerated a correct
  mesh in all passes, and the destructive path (reviewer rewrites) is hard-
  guarded — but extending the hard guard to initial generation would restore the
  "meshing node owns the mesh" contract.
- `pytest tests/` fails at collection: importing the module chain instantiates
  the default `openai-codex` provider, which demands Codex auth. Pre-existing.
- Tier 2 (pitzDaily blockMeshDict topology) remains unfixed and untested today.
- LLM token statistics are wrong for multi-node runs (counts only a subset of
  calls); do not use them for cost accounting.
- Run-to-run reviewer-loop variance is real even at temperature 0 (0 vs 6 loops
  for the same tier3 prompt); judge by success rate over repeats, not one run.
