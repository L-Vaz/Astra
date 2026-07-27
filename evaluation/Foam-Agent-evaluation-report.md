# Foam-Agent v2.1.0 reliability evaluation

**Verdict:** Foam-Agent can generate a physically credible simple cavity case when placed in a controlled, space-free Linux path, but the unmodified release is **not reliable enough to serve as the verification foundation** for the proposed CFD design system. Its execution verification, automated tests, reviewer gating, packaging, and visualization checks require hardening first.

## 1. Environment and versions

| Item | Tested value |
|---|---|
| Foam-Agent release | `v2.1.0` |
| Tested commit | `bfb7f791f77438ff90c1a39ff398739ee97bcc67` |
| Remote `main` during final check | `cfde3847be548e4264a1455dd77774794e00ee81` (not tested) |
| Repository status | Clean after evaluation; no Foam-Agent source changes |
| OpenFOAM | Foundation OpenFOAM 10, apt package `openfoam10 20230119`, build `10-c4cf895ad8fa` |
| Solver | `icoFoam` from OpenFOAM 10 |
| OS | Windows 11 build 26200; WSL2 Ubuntu 22.04.5, kernel 6.6.87.2 |
| Hardware available to WSL | 8 logical CPUs, 7.6 GiB RAM; host i5-11300H, 16 GB RAM |
| Python / Conda | Python 3.12.9; Conda 26.3.2 |
| Key libraries | LangGraph 0.2.71, LangChain 0.3.18, FastMCP 2.13.0.1, PyVista 0.44.2, VTK 9.3.1 |
| Model/provider | `openai-codex / gpt-5.5`; repository default temperature 1 |

The current release is [`v2.1.0`](https://github.com/csml-rpi/Foam-Agent/releases/tag/v2.1.0), but `pyproject.toml` and the MCP server still identify themselves as 2.0.0. The advertised `gpt-5.3-codex` model was rejected by the subscription endpoint; `gpt-5.5`, the locally configured Codex model, was used instead.

The [official Docker repository](https://hub.docker.com/r/leoyue123/foamagent) exposed only `latest` at digest `sha256:5c8bd752884b95b45c02d74a4b5e449fbf22aea8fa13a0eb1a40be851350ff1e` and reported size 8,208,992,645 bytes. `v2.0.0` and `v2.1.0` tags were unavailable. The largest layer failed extraction twice and exhausted the original WSL VHD, so the final tests used the repository's exact Conda environment and the official OpenFOAM 10 apt package. This is a strong reason for deviating from Docker, but it is also a release-reproducibility failure.

## 2. Commands used

The complete installation, test, execution, and validation command transcript is in [commands-used.md](commands-used.md). Important entry points were:

```bash
python -m pytest tests -v
python tests/test_lid_driven_cavity_services.py
python -m src.mcp.fastmcp_server --transport http --host 0.0.0.0 --port 8080
python tests/test_lid_driven_cavity_mcp.py
python foambench_main.py --output /opt/foamagent-eval/run1 --prompt_path /opt/Foam-Eval/prompts/cavity_re100.txt
checkMesh -case . -allGeometry -allTopology
```

## 3. Test-case definition

The complete prompt is preserved at [cavity_re100.txt](prompts/cavity_re100.txt). It requested a 1×1×0.01 m two-dimensional cavity, 40×40×1 structured mesh, `U_lid=1 m/s`, `nu=0.01 m²/s`, `Re=100`, no-slip side/bottom walls, empty front/back, `dt=0.01 s`, `endTime=50 s`, explicit `checkMesh`, error repair, and final velocity/streamline visualization.

## 4. Generated OpenFOAM configuration

Both clean runs generated the expected case structure:

```text
0/{U,p}
constant/physicalProperties
system/{blockMeshDict,controlDict,fvSchemes,fvSolution}
Allrun
visualization.py
```

- Geometry and patch faces are correct: `movingWall` is the top; `fixedWalls` combines left, right, and bottom; `frontAndBack` contains both z-normal faces.
- `U`: moving wall `(1 0 0)`, fixed walls `noSlip`, front/back `empty`.
- `p`: `zeroGradient` on solid walls, `empty` front/back; `pRefCell=0`, `pRefValue=0` in PISO.
- `physicalProperties`: constant `nu=0.01`.
- `controlDict`: `icoFoam`, 0–50 s, `deltaT=0.01`, writes every 5 s.
- Run 1 used `CrankNicolson 0.9`; run 2 used `backward`. Both are second order. Other differences were `scale` versus `convertToMeters` and orthogonal versus corrected operators on a perfectly orthogonal mesh.
- **Defect:** `Allrun` contained only `blockMesh` and `icoFoam`. Foam-Agent ignored the explicit requirement to run `checkMesh`.

## 5. Execution results

| Execution | Result |
|---|---|
| `pytest tests -v` | **Fail:** zero tests collected, exit 5, 50.47 s |
| Service cavity script | **Fail:** stale `Config.file_dependency_threshold` access at line 96 after successful planning, 61.35 s |
| MCP cavity script | Script reported 5/5 pass, but **actual execution failed**: unquoted workspace path produced `bash: /mnt/e/cursor: No such file or directory`; no mesh or time directories existed |
| Requested run 1 | Completed; agent workflow 249.19 s; solver 12.8469 s |
| Requested run 2, clean directory | Completed; agent workflow 198.55 s; solver 7.1675 s |

Requested-run state transitions were:

```text
planner → standard-mesh router → input_writer → local-run router
→ runner → visualization → end
```

There were **zero reviewer/repair iterations** in both runs (`review.log` is empty). Foam-Agent made three recorded LLM calls per run, with 1,599 recorded tokens, and reported no retries.

Run 1 final solver values:

- `Ux` initial residual `1.31604e-7`; `Uy` `8.2615e-8`.
- Pressure final solve: initial `1.05811e-6`, final `8.73613e-7`.
- Final continuity: local `3.36414e-10`, global `5.26459e-20`, cumulative `8.11807e-18`.
- `log.icoFoam` ends with `End`; output times 0, 5, …, 50 exist.

## 6. Independent physical validation

These checks were not performed by Foam-Agent.

### Mesh and conservation

Independent `checkMesh -allGeometry -allTopology` passed both runs:

- 1,600 hexahedral cells, 3,362 points, 6,480 faces.
- Bounds `(0 0 0)` to `(1 1 0.01)`; total volume 0.01 m³.
- 40 moving-wall faces, 120 fixed-wall faces, 3,200 empty faces.
- Maximum non-orthogonality 0; maximum skewness `3.55e-14`; one region; `Mesh OK`.

Independent `patchFlowRate` at 50 s returned exactly zero for both `movingWall` and `fixedWalls`. `frontAndBack` is empty and therefore has no flux faces. The largest absolute global continuity error in the run was below `6.60e-19`.

### Steadiness and physical plausibility

| Metric | Run 1 | Run 2 |
|---|---:|---:|
| Relative L2 change, 45→50 s | `1.731e-7` | `1.037e-7` |
| Maximum velocity change, 45→50 s | `1.013e-6` | `9.984e-7` |
| Approx. primary-vortex centre | `(0.6167, 0.7417)` | `(0.6167, 0.7417)` |
| Centreline `u` range | `[-0.20955, 1.0]` | `[-0.20956, 1.0]` |
| Centreline `v` range | `[-0.24895, 0.17595]` | `[-0.24896, 0.17595]` |

The flow is clockwise, contains the expected primary vortex and lower-corner secondary motion, respects the lid speed, and is effectively steady.

### Ghia et al. comparison

Against the Re=100 centreline tables from Ghia, Ghia and Shin, *Journal of Computational Physics* 48 (1982), [DOI 10.1016/0021-9991(82)90058-4](https://doi.org/10.1016/0021-9991(82)90058-4):

| Error | Run 1 | Run 2 |
|---|---:|---:|
| Vertical-centreline `u` RMSE | `0.001552` | `0.001555` |
| `u` maximum absolute error | `0.003191` | `0.003200` |
| Horizontal-centreline `v` RMSE | `0.003448` | `0.003451` |
| `v` maximum absolute error | `0.007293` | `0.007302` |

Plots and raw values are in [artifacts/run1](artifacts/run1) and [artifacts/run2](artifacts/run2). Foam-Agent's own `visualization.png` is incorrect for the prompt: it plots pressure `p` in an isometric view, not velocity magnitude or vectors/streamlines.

## 7. Pass/fail table

| Criterion | Result | Verification owner |
|---|---|---|
| Correct release SHA recorded | Pass | Independent |
| Official Docker setup usable and pinned | Fail | Independent |
| Pytest suite | Fail: zero tests | Repository tooling |
| Service cavity test | Fail: stale API | Repository tooling |
| MCP cavity test | Fail despite reported pass | Foam-Agent + independent audit |
| Natural-language planning | Pass | Foam-Agent output inspected independently |
| Geometry and boundary names | Pass | Independent file and mesh inspection |
| Boundary conditions and viscosity | Pass | Independent file inspection |
| Mesh generation | Pass on clean Linux path | OpenFOAM output |
| Agent executes `checkMesh` | Fail | `Allrun` inspection |
| Independent `checkMesh` | Pass | Independent OpenFOAM run |
| Solver reaches 50 s and writes fields | Pass | Independent log/time inspection |
| Residuals and mass conservation | Pass | Independent parsing/post-processing |
| Physically plausible flow | Pass | Independent field sampling |
| Ghia Re=100 profiles | Pass | Independent comparison |
| Requested velocity visualization | Fail | Independent image/script inspection |
| Error detection and repair | Fail on MCP path error; not exercised in clean runs | Independent audit |
| Clean rerun approximately agrees | Pass | Independent field comparison |
| No manual intervention | Fail | Environment and validation record |

## 8. Reproducibility assessment

The second run used a completely clean output directory. The final velocity fields differed by relative L2 `6.112e-4`, with maximum cell-velocity difference `0.002853`. Core BC, physical-property, control, and runner files were byte-identical; second-order discretization choices varied stochastically. This is good numerical reproducibility for the simple case but not byte-level case reproducibility.

Reproduction currently depends on four undocumented or manual controls: a space-free Linux path, a compatible OAuth schema adapter, selection of an actually supported Codex model, and independent verification outside Foam-Agent.

## 9. Weaknesses discovered

1. `run_command` does not enforce the subprocess return code. Error detection scans `log*` files but not `Allrun.err`; a failed run with no solver log is classified as success.
2. The MCP test proves this false-positive path and still marks zero visualization artifacts as a pass.
3. `pytest` provides no automated coverage; the service script is stale and fails before case generation.
4. The current Codex Desktop `auth.json` stores credentials under `tokens`, but v2.1.0 only understands older top-level/Clawdbot shapes.
5. The documented default `gpt-5.3-codex` was unusable for the tested ChatGPT account.
6. The official image is extremely large, lacks immutable release tags, and its release entrypoint can update source unless explicitly disabled.
7. Explicit `checkMesh` and requested visualization semantics are not enforced.
8. The reviewer is bypassed whenever the weak runner declares success. It performs no residual, conservation, steadiness, or reference-profile validation.
9. The runner warns on the word `error` in normal `time step continuity errors`, showing brittle log heuristics.
10. Generation is stochastic at temperature 1; discretization changes between otherwise identical clean runs.

These findings are consistent with the distinction made in the [Foam-Agent paper](https://arxiv.org/abs/2509.18178) between executability and broader capability, and with the [CFDLLMBench/FoamBench evaluation](https://arxiv.org/abs/2509.20374): for the reported Foam-Agent + RAG + reviewer configuration, executable success substantially exceeded physical-validation success. A successful process exit is not a sufficient CFD acceptance criterion.

## 10. Recommendation

**Do not build the CAD, convergence, surrogate, or optimization layers on unmodified Foam-Agent yet.** The simple CFD generation core is promising—the controlled cavity runs are accurate—but the system's own reliability signal is demonstrably unsafe.

Foam-Agent is viable as a **prototype code-generation component** only after a focused hardening phase:

1. Make runner exit status, stderr, expected logs, final time, and `End` markers mandatory gates.
2. Require `checkMesh` and structured residual/continuity validation before reviewer success.
3. Replace the scripts with real isolated pytest tests, including deliberate failing-run cases.
4. Fix current Codex auth/model compatibility and publish immutable release images.
5. Validate visualization field/type and add deterministic, domain-specific physical checks.

No requested future extensions were implemented, and Foam-Agent source was not refactored or patched.
