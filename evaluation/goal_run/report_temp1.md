# Foam-Agent Complete Test Plan Report

Run date: 2026-06-25

Runtime:
- WSL output root: `/home/lindonlv/foamagent-goal/20260625_153609`
- Workspace logs: `E:\cursor projects\Astra\evaluation\goal_run\logs`
- Prompts: `E:\cursor projects\Astra\evaluation\goal_run\prompts`
- Model settings loaded from `~/.foam.env`: `FOAMAGENT_MODEL_PROVIDER=anthropic`, `FOAMAGENT_MODEL_VERSION=claude-sonnet-4-6`
- OpenFOAM: Foundation OpenFOAM v10 commands available from `/opt/openfoam10`
- Python: `/opt/conda/envs/FoamAgent/bin/python`, Python 3.12.9
- `max_loop`: 8

## Result Summary

| Tier | Case | Exit | Wall time | Reviewer loops | End-to-end result |
|---|---|---:|---:|---:|---|
| 1 | Lid-driven cavity, `blockMesh`, `icoFoam` | 0 | 2:18 | 0 | Pass |
| 2 | pitzDaily RAS, `blockMesh`, `simpleFoam` | 1 | 3:45 | 3 | Fail |
| 3 | Cylinder, generated Gmsh, `pisoFoam` | 1 | 3:04 | 1 | Fail |
| 4 | Tandem wing, external `.msh`, `simpleFoam` SA | 1 | 3:01 | 1 | Fail |

Total elapsed wall time: about 12:10.

## Tier Details

### Tier 1 - Lid-Driven Cavity

Passed end to end.

Evidence:
- Planner selected `icoFoam`, standard mesh, local run, visualization.
- `Allrun` completed with exit code 0.
- Solver wrote final time `10`.
- Independent verification from a clean WSL cwd: `checkMesh` exit code 0, `400` cells, `Mesh OK`, `End`.
- Visualization artifact exists: `E:\cursor projects\Astra\evaluation\goal_run\artifacts\tier1_visualization.png`.
- LLM stats printed: 3 calls, 0 failed calls, 0 retries, 1,170 prompt tokens, 11 completion tokens, 1,181 total estimated tokens.

### Tier 2 - pitzDaily RAS

Failed in the error-fix loop.

What worked:
- Planner selected `simpleFoam`, RAS, standard mesh, local run, visualization.
- Initial file generation completed for `system`, `constant`, and `0` files.
- Reviewer loop engaged after the first failed run.

Failure evidence:
- Final `blockMesh` log failed with: `Inconsistent number of faces between block pair 0 and 1`.
- No `constant/polyMesh` was produced, so independent `checkMesh` was skipped.
- `simpleFoam` then failed because no mesh points existed.
- On loop 3, `input_writer` crashed on a structured output validation error: `FoamPydantic.list_foamfile` missing.

Interpretation:
- The reviewer identified and corrected an invalid shell comment in `Allrun`, but did not converge on a valid pitzDaily `blockMeshDict`.
- The hard crash is not a CFD failure; it is a structured-output robustness failure in the rewrite path.

### Tier 3 - Cylinder Gmsh

Failed before producing the requested Gmsh mesh.

What worked:
- Planner routed to `gmsh_mesh`.
- Input writer later generated basic solver files.
- Reviewer loop engaged after runner failure.

Failure evidence:
- The meshing node hit three `GMSHPythonCode` validation errors: missing `python_code`, `mesh_type`, and `geometry_type`.
- Despite the Gmsh meshing failure, the workflow continued to input writing.
- Generated `Allrun` ran `blockMesh` and `pisoFoam`.
- `blockMesh` failed with: `Cannot find file "system/blockMeshDict"`.
- `pisoFoam` failed with: `Cannot find file "points" in directory "polyMesh"`.
- On reviewer loop 1, `input_writer` crashed on `FoamPydantic.list_foamfile` missing.

Interpretation:
- The Gmsh path did not produce `generate_mesh.py`, `geometry.msh`, or `polyMesh`.
- The workflow should stop or retry inside the meshing node when structured Gmsh generation fails, rather than falling through to a solver run.

### Tier 4 - Tandem Wing External Mesh

Failed before importing/using the external mesh.

What worked:
- Planner routed to `custom_mesh`.
- The 62 MB mesh was copied into the case as `geometry.msh`.
- Input writer generated simpleFoam/Spalart-Allmaras fields.
- Reviewer loop engaged after runner failure.

Failure evidence:
- No `constant/polyMesh` existed after the run.
- Generated `Allrun` ignored the imported mesh and ran `blockMesh`, `checkMesh`, then `simpleFoam`.
- `blockMesh` failed with: `Cannot find file "system/blockMeshDict"`.
- `checkMesh` and `simpleFoam` failed with missing `polyMesh/points`.
- On reviewer loop 1, `input_writer` crashed on `FoamPydantic.list_foamfile` missing.

Interpretation:
- The external mesh path is not working as a reliable end-to-end path in this run.
- This did not reach the RAM-stress phase; failure happened before a real imported-mesh solve.

## Cost And Token Reporting

Foam-Agent only printed LLM token statistics for the successful Tier 1 run. The three failed runs exited through exceptions before `LLMService.print_statistics()` ran, so their token totals and dollar costs were not persisted in the logs.

Known token evidence:
- Tier 1: 1,181 estimated total tokens.

Dollar cost:
- Not computed from logs. The local code does not persist provider billing data, and failed tiers did not print final token totals.

## Capability Report Card

| Capability | Result | Notes |
|---|---|---|
| Planning | Partial pass | All four tiers produced plausible plans and routing decisions. |
| `blockMesh` built mesh | Partial pass | Tier 1 valid; Tier 2 invalid pitzDaily block topology. |
| Gmsh generated mesh | Fail | Tier 3 structured Gmsh code generation returned invalid tool output. |
| External mesh import | Fail | Tier 4 copied the mesh but did not produce/use `polyMesh`. |
| Input writer initial generation | Partial pass | Initial files were written in all tiers, but failed tiers had invalid run/mesh behavior. |
| Local solving | Partial pass | Tier 1 solved; Tiers 2-4 did not reach a valid solve. |
| Reviewer/error-fix loop | Fail for robustness | It engaged, but rewrite crashed on missing structured fields before max_loop. |
| Visualization | Partial pass | Tier 1 produced a valid PNG; failed tiers never reached visualization. |

## Recommendation

Foam-Agent is not yet a known-good base for Astra beyond the simplest cavity path. The cavity workflow is real and verified, but the broader multi-agent pipeline is currently blocked by three structural issues:

1. Runner errors are too lossy. `Allrun.err` is often empty while `log.blockMesh`, `log.simpleFoam`, or `log.pisoFoam` contain the real fatal error. The reviewer needs those log tails directly.
2. Structured-output failures need retry/repair handling. `FoamPydantic` and `GMSHPythonCode` validation failures currently crash the graph instead of being treated as recoverable LLM output errors.
3. Mesh-mode contracts need enforcement. Gmsh failure should not fall through to `blockMesh`, and custom mesh runs should not generate `Allrun` scripts that call `blockMesh`.

I would fix those before building Astra features on top of the full pipeline.
