# Task → file maps

Purpose: when planning or implementing a change, read the section for your task, open exactly the files listed, and skip repo-wide discovery. Live code is authoritative — if a listed name has moved, trust the code and update this map.

## How the loop hangs together (read this once)

- The six Astra design stages are `plan → geometry → fast_aero → mesh → simulation → results` (`STAGES` in `src/services/design_session.py`). Stages 1–4 and 6 are **MCP tools** in `src/mcp/fastmcp_server.py`, **not** LangGraph nodes. State lives on disk in versioned session dirs (`runs/design_sessions/<sid>/<stage>/v<N>/`), not in `GraphState`.
- Stage 5 (`run_simulation`) is the only bridge into the inherited Foam-Agent LangGraph graph: `src/services/simulate.py` shells out to `src/main.py --custom_mesh_path <mesh>/bwb.msh`.
- Inherited graph topology (`src/main.py`): `START → planner → {meshing | input_writer} → {hpc_runner | local_runner} → {reviewer | visualization | END}`; `reviewer → input_writer` (error loop, capped by `config.max_loop`).
- Two Python environments: FoamAgent conda env (py3.12) runs everything except OpenVSP; `vsp_builder.py` and `vsp_aero.py` import `openvsp` and must run under `VSP_PYTHON` (default `/opt/vsp-venv/bin/python`, py3.10) as subprocesses. The exchange contract is files (`params.json` in, `geometry_report.json`/`fast_aero.json` out).

---

## 1. Mission intent / requirements parsing (stage 1, `plan_design`)

- `src/services/intent.py` — NL request → structured physics. `run_intent(user_prompt, llm_service, revision_note)` → `IntentResult{mission_derived, enriched_requirement}`; `parse_mission` (the only LLM step), `build_enriched_requirement` (deterministic OpenFOAM-v10/simpleFoam requirement text).
- `domain_packs/bwb/defaults.py` — deterministic mission→physics sizing, no LLM. `derive(mission) → MissionDerived`; `isa_density`, `_planform_from`. Constants: `WING_LOADING`, `DEFAULT_AR=5.5`, `PAYLOAD_FRACTION=0.30`, `MAX_CRUISE_CL=0.75`. Every default lands in `assumptions`.
- `domain_packs/bwb/schema.py` — `MissionSpec`, `MissionDerived` (pydantic; field ranges are the validation gate).
- Wiring: MCP tool `plan_design` in `src/mcp/fastmcp_server.py`. Artifacts: `plan/vN/mission_derived.json` + `enriched_requirement.txt` — the contract every downstream stage reads.
- Caveat: `parse_mission` is backstopped by deterministic `derive()` + pydantic validation; keep it that way (no load-bearing LLM).

## 2. Drone geometry / parametric changes (stage 2, `build_geometry`)

- `domain_packs/bwb/schema.py` — `BWBParams` is the parametric planform (12 fields). Field ranges + `@model_validator cross_checks` (tip chord ≥ 0.04 m, tip thinner than root, AR ∈ [2.5,10], no wash-in) ARE the geometry gates. Derived: `chords()`, `planform_area_m2()`, `mean_aero_chord_m()`, `aspect_ratio()`. Also `GeometryReport`, `GateResult`.
- `domain_packs/bwb/vsp_builder.py` — OpenVSP builder CLI (**py3.10 venv only**). `build_bwb` builds a 2-panel WING geom; exports `bwb.vsp3`/`bwb.stl`/`bwb_degen.csv`; `watertight_check`; `render_views` (4 PNGs). Gates: `param_ranges`, `vsp_build`, `stl_watertight`, `bbox_sanity`. Writes `geometry_report.json`; exit 0 iff all pass.
- `src/services/geometry.py` — py3.12 subprocess wrapper. `build_geometry(params, outdir, timeout_s=600) → GeometryReport`; `apply_overrides` (re-validates); raises `GeometryRejected`. `VSP_PYTHON` env, `_BUILDER` path.
- `domain_packs/bwb/defaults.py` — `_planform_from` produces the initial `BWBParams` proposal.
- Wiring: MCP tool `build_geometry` (param source: latest geometry `params.json`, else plan's `mission_derived.json`). Cockpit `POST /api/revision` also rebuilds via `build_geometry_svc`.
- Caveats: never import `openvsp` into the py3.12 env. Watertight STL matters — gmsh (task 4) and VSPAERO (task 3) both consume it. `vsp_builder.py` imports `bwb.schema` by path; keep repo layout intact.

## 3. Fast aero / VSPAERO (stage 3, `run_fast_aero`)

- `domain_packs/bwb/vsp_aero.py` — VSPAERO vortex-lattice CLI (**py3.10 venv**). `VSPAEROComputeGeometry` (thin surface only) then `VSPAEROSweep`; parses the `.polar` file by column name (stable contract across OpenVSP versions). Computes `cl_alpha_per_deg`, `x_np_m`, `alpha_trim_deg`, `cd_at_trim`, `ld_at_trim`. Gates: `polar_present`, `lift_slope_sane` (0.02–0.15/deg), `trim_in_sweep`. Writes `fast_aero.json`.
- `src/services/vspaero.py` — subprocess wrapper. `run_fast_aero(geometry_report, derived, outdir, timeout_s=900) → dict`; raises `FastAeroRejected`.
- Wiring: MCP tool `run_fast_aero`. Downstream consumers: `alpha_trim_deg` → stage-5 `compose_requirement` (freestream AoA); `x_np_m` → stage-6 evaluator moment reference; VLM CL → evaluator plausibility band.

## 4. Meshing — two distinct meshers, do not conflate

**(A) Astra deterministic farfield mesher (stage 4, `generate_mesh`), no LLM:**
- `domain_packs/bwb/gmsh_farfield.py` — Gmsh farfield CLI (**FoamAgent conda env** — gmsh lives there). Box 8·MAC up/lateral, 20·MAC downstream; physical groups `inlet/outlet/farfield/aircraft/internal`; Delaunay `Algorithm3D=1` (NOT HXT — silent 0-tet failures) + Netgen optimize. Writes Gmsh **v2.2** `.msh` (gmshToFoam compatibility) + meshinfo JSON. `LEVELS` = coarse/medium/fine sizing.
- `src/services/bwb_mesh.py` — service + validation. `generate_mesh(stl_path, mac_m, span_m, outdir, level, max_cells, timeout_s) → dict`; raises `MeshRejected`. Scratch OpenFOAM case → `gmshToFoam` → set `aircraft` patch to `wall` via `foamDictionary` → `checkMesh` gates → PyVista renders. `FOAM_BASHRC` env, `_MIN_CELLS=20000`, `EXPECTED_PATCHES`.

**(B) Inherited graph mesh path (runs inside stage-5 simulation):**
- `src/nodes/meshing_node.py` — dispatches on `state["mesh_type"]`: `custom_mesh` → `copy_custom_mesh`, `gmsh_mesh` → `handle_gmsh_mesh`, else standard.
- `src/services/mesh.py` — `copy_custom_mesh` (copy `.msh`, minimal controlDict, `gmshToFoam`, LLM wall-patch classification → `foamDictionary`); `handle_gmsh_mesh` (LLM-generated Gmsh python with correction loops, `max_loop=3`).
- `mesh_type` is set in `src/nodes/planner_node.py` via `llm_requires_custom_mesh` (`src/router_func.py`: 1=custom, 2=gmsh, 0=standard); routed by `route_after_planner`.
- GraphState: reads `mesh_type`, `custom_mesh_path`, `case_dir`; writes `mesh_info`, `mesh_commands`, `custom_mesh_used`.
- Caveats: requirement text must say "a custom mesh file is provided—use it as-is" (saying "Gmsh mesh" selects route B-gmsh wrongly). Aircraft patch must be `wall` for wall functions/yPlus. Preserve: Delaunay+Netgen, deterministic conversion, wall-patch classification.

## 5. Simulation bridge + graph orchestration (stage 5, `run_simulation`; adding/modifying a graph stage)

- `src/services/simulate.py` — the bridge. `compose_requirement(enriched_requirement, cruise_speed_ms, alpha_deg, iterations)` (appends custom-mesh paragraph + trim AoA + bounded numerics), `run_simulation(requirement_text, msh_path, workdir, timeout_s)` (subprocess to `src/main.py`); `SimulationFailed`.
- `src/main.py` — graph assembly. `create_foam_agent_graph()` (nodes + edges), `initialize_state(...)` (constructs the full GraphState dict — new fields MUST be initialized here or KeyError), `main(...)`.
- `src/utils.py` — `GraphState` TypedDict (~L736), `LLMService` (`.invoke(user_prompt, system_prompt, pydantic_obj=...)`), `run_command`, `check_foam_errors`, FAISS helpers.
- `src/router_func.py` — all conditional edges (see task 8 for the loop routers).
- `src/nodes/*` — thin wrappers, one `<name>_node(state)` each, returning partial GraphState updates. Keep business logic out.
- `src/services/__init__.py` — lazy `global_llm_service` singleton (services import it; nodes use `state["llm_service"]`).

**Recipe — new graph stage, in order:** (1) service in `src/services/<stage>.py`; (2) thin `src/nodes/<stage>_node.py`; (3) new keys in `GraphState` (`src/utils.py`) AND `initialize_state` (`src/main.py`); (4) wire in `create_foam_agent_graph()` — import, `add_node`, edges / router branch in `src/router_func.py`; (5) if externally orchestrated, expose as MCP tool.

## 6. CFD case planning & input writing (inherited graph)

- `src/services/plan.py` — `generate_simulation_plan(...)`; `parse_requirement_to_case_info` (LLM → `CaseSummaryModel`), `retrieve_references` (FAISS + solver rerank), `decompose_to_subtasks` (LLM → `OpenFOAMPlanModel`).
- `src/nodes/planner_node.py` — calls the plan service, creates/cleans `case_dir`, `setup_logging`, caches `mesh_type`/`requires_hpc`/`requires_visualization` (so routers run deterministically off cache).
- `src/services/input_writer.py` — `initial_write` (subtasks sorted system→constant→0; `sequential_dependency` vs `parallel_no_context`), `build_allrun` (FAISS `openfoam_command_help` → Allrun; premeshed no-remesh constraints), `rewrite_files` (error-fix, plan-restricted, `BANNED_MESH_COMMAND_RE` hard guard), `read_mesh_boundary_summary`.
- `src/nodes/input_writer_node.py` — dispatches on `state["input_writer_mode"]`: initial vs rewrite.
- GraphState: planner writes `case_*`, references, `subtasks`, `mesh_type`; input_writer writes `dir_structure`, `commands`, `foamfiles` (clears `error_logs` on rewrite).

## 7. Solver execution (local + HPC)

- `src/services/run_local.py` — `run_allrun_and_collect_errors(case_dir, timeout, max_retries)`; stages to a temp dir if the path contains whitespace; validation tiers: exit code → `check_foam_errors` → polyMesh present → timestep > 0 → endTime reached.
- `src/nodes/local_runner_node.py` — thin wrapper; writes `error_logs`.
- `src/services/run_hpc.py` — `extract_cluster_info_from_requirement` (LLM), `create_slurm_script*` (LLM), `submit_slurm_job`, `check_job_status`, `wait_for_job`.
- `src/nodes/hpc_runner_node.py` — retry loop up to `config.max_loop`; writes `error_logs`, `job_id`, `cluster_info`, `slurm_script_path`.
- `src/utils.py` — `run_command` (bash with `WM_PROJECT_DIR` bashrc sourcing, `max_time_limit` kill), `check_foam_errors`.
- Routing: `route_after_input_writer` → `hpc_runner`|`local_runner` (cached `requires_hpc`, LLM fallback).

## 8. Review / error-correction loop

- `src/services/review.py` — `review_error_logs(...) → (review_analysis, history)` (stateless LLM diagnosis), `generate_rewrite_plan(...)` (LLM → strict `RewritePlan` JSON).
- `src/nodes/reviewer_node.py` — short-circuits on empty `error_logs`; builds mesh context for locked custom/gmsh meshes; increments `loop_count`; sets `input_writer_mode="rewrite"`.
- Rewrite execution: `src/services/input_writer.py::rewrite_files` on the next loop pass.
- `src/router_func.py` — `route_after_runner` (errors → reviewer; else visualization/END), `route_after_reviewer` (`loop_count >= config.max_loop` → `termination_reason="max_review_loop_reached"`, END).

## 9. Results evaluation, verdict & visualization (stage 6, `evaluate_results`)

- `src/services/evaluate.py` — **out-of-graph** judge. `evaluate_case(case_dir, outdir, required_cl, vlm_cl, extra_slices, ref)` → report with `verdict` ∈ {pass, revise-numerics, revise-design, reject}. `find_force_coeffs`, `parse_residuals`, `parse_yplus`; `_posthoc_function_objects` (injects forceCoeffs/yPlus + `simpleFoam -postProcess` when missing — a MUST-preserve fallback); matplotlib plots + PyVista field renders; writes `evaluation.json`.
- `src/services/visualization.py` — in-graph viz. `generate_deterministic_pyvista_script` (preferred), LLM generate/fix fallback, `run_pyvista_script`.
- `src/nodes/visualization_node.py` — deterministic first, then LLM loop; terminal node.
- Wiring: MCP tool `evaluate_results` builds `ref` from geometry/mission/fast-aero stage JSON. Also called by all three verification scripts.
- Caveat: a clean solver exit is not valid physics — keep convergence/plausibility/mission checks.

## 10. MCP tools

- `src/mcp/fastmcp_server.py` — the single FastMCP server (port 7860 http, or stdio); ALL tools live here. Inherited: `plan`, `input_writer`, `run`, `review`, `apply_fixes`, `visualization`. Astra: `plan_design`, `build_geometry`, `run_fast_aero`, `generate_mesh`, `run_simulation`, `evaluate_results`, `open_cockpit`.
- `src/mcp/cli.py` — `foamagent-mcp` console entry (stdio; deliberately does not add `src/` to path). `src/mcp/start_mcp.py` — env-var entry (`MCP_TRANSPORT/HOST/PORT`), used by Docker.
- Adding a tool: `@mcp.tool` async fn + pydantic Request/Response models; business logic stays in `src/services/`. Astra stage pattern: `_require_stage(sid, stage)` → `ds.new_version_dir` → service → `ds.record_version`.
- Caveat: `RunSimulationRequest` is defined twice (local `run` tool and Astra `run_simulation`); annotations bind at decoration time so both work, but don't rely on the module-level name.

## 11. Cockpit UI (requirements-first explorer)

- `src/cockpit/server.py` — FastAPI, port 8765, shares `design_session` as source of truth, **no LLM**. Endpoints: `/api/health`, `/api/bootstrap` (now includes `requirements`, `requirements_status`, `requirements_schema`), `/api/preview`, `/api/requirements` (inverse solve, persist optional, never builds OpenVSP), `/api/explain` (stateless derivation graph + sensitivities), `/api/mission` (uses `invalidate_from="fast_aero"` — keeps built geometry), `/api/revision` (rebuilds geometry; the only Apply), `/api/stl/...`, `/api/fast_aero`, `/api/locks`, `/api/explanations/...`, `/api/debug/snapshot`. API routes are registered before the static mount so they win.
- Frontend: three-column explorer. `static/req_panel.js` — requirement controls, chips, binding banner; POSTs `/api/requirements` (debounced 120 ms, `persist:false` while dragging). `static/why_panel.js` — clickable metric rows → `/api/explain` derivation trees + sensitivity strips. `static/cockpit.js` — state, three.js viewport, Apply, candidates carousel ("Suggest designs" button), versions. `index.html` (three.js 0.160.0 via unpkg CDN — needs network), `cockpit.css`.
- Launch: `open_cockpit` MCP tool (`fastmcp_server.py`) — Popen `-m src.cockpit.server`, polls `/api/health` up to 120 s (first start loads FAISS); log at `<parent-of-sessions_root>/cockpit.log`.
- Caveat: `aero_mirror.js` mirrors the FORWARD estimate only (frozen scope, instant drag feedback). The inverse solver, derivation graph, and sensitivities are server-side only (`domain_packs/bwb/inverse.py`, `derivation.py`, `sensitivity.py`) — do NOT port them to JS; changing forward BWB math still means changing both `domain_packs/bwb/aero_estimates.py` and `aero_mirror.js`.

## 12. Design sessions / revisions / rollback / requirements

- `src/services/design_session.py` — the versioned store. `sessions_root()` (env `ASTRA_SESSIONS_DIR`, default `runs/design_sessions`), `create_session`, `get_state`, `latest_dir(sid, stage)` (latest **valid** version or None), `new_version_dir`, `record_version(sid, stage, summary, invalidate_from=None)` (marks valid + invalidates downstream from `invalidate_from`, default = the next stage — the cockpit mission route passes `"fast_aero"` so a mission change no longer orphans built geometry). Layout: `<sid>/<stage>/v<N>/` + `session.json`.
- Requirements store (NOT a stage; never touches stage validity): `save_requirements` / `latest_requirements` → `<sid>/requirements/v<N>/requirements.json` + `index.json`. Staleness of built geometry vs requirements is surfaced by the cockpit, not enforced. Models: `domain_packs/bwb/requirements.py`; solver: `domain_packs/bwb/inverse.py` (`solve_requirements`, `check_requirements`, `solve_planform` — also the engine behind `variants._solve_planform`).
- Rollback model: versions are never deleted; a new stage-k version invalidates stages > k, forcing re-run downstream.
- Cockpit extras under `<sid>/cockpit/`: `explanations.jsonl`, `locks.json`.
- Readers/writers: `fastmcp_server.py` (all Astra tools incl. `set_requirements`), `cockpit/server.py`, `verify_revisions.py`.

## 13. Verification & gates

- `verification/_common.py` — `REPO_ROOT`, `RUNS_DIR` resolution (env `ASTRA_RUNS_DIR`; falls back to `~/astra_runs` for space-containing repo paths), `Table` (PASS/FAIL/SKIP + exit code).
- `verification/verify_golden.py` — V1 golden path: intent → derive → geometry → fast aero → mesh (`--no-llm` stops here, no credentials needed); full run continues sim → evaluate. Output: `RUNS_DIR/verify_golden/`.
- `verification/verify_revisions.py` — V2 revisions/rollback, no LLM; sim-restart and extra-slice rows **require a completed golden case**.
- `verification/verify_gates.py` — V3 adversarial gates: bad inputs must reject at the right gate with the right reason; numerics gates require golden case + OpenFOAM.
- Evidence: `reports/verification-2026-07-10/`.

## 14. RAG / FAISS database

- `database/faiss/<model>/` — four prebuilt indices: `openfoam_tutorials_structure`, `openfoam_tutorials_details`, `openfoam_allrun_scripts`, `openfoam_command_help`. Do not regenerate without reason (`init_database.py --openfoam_path --force` rebuilds).
- `src/utils.py` — query side: `get_embedding_model`, `load_faiss_dbs`, module-level `FAISS_DB_CACHE` (built at import with default `Config()` — custom embedding config requires re-calling `load_faiss_dbs`), `retrieve_faiss(database_name, query, topk)`.
- Call sites: `plan.py::retrieve_references` (structure + allrun indices), `input_writer.py::build_allrun` (command help).
- Config coupling: `embedding_provider`/`embedding_model` select the index dir; `searchdocs` is the recall floor.

## 15. Config & environment knobs

- `src/config.py` — `Config` dataclass. Loop/RAG: `max_loop=10`, `searchdocs=10`, `max_time_limit=3600`, `recursion_limit=100`. Paths: `database_path`, `run_directory`, `case_dir`. Writer: `input_writer_generation_mode`, `reuse_generated_dir`. LLM: `model_provider`/`model_version`/`temperature=0`. Embedding: `embedding_provider`/`embedding_model`. Env overrides: `FOAMAGENT_MODEL_PROVIDER`, `FOAMAGENT_MODEL_VERSION`, `FOAMAGENT_EMBEDDING_PROVIDER`, `FOAMAGENT_EMBEDDING_MODEL`.
- Env vars outside Config: `ASTRA_RUNS_DIR` (OpenFOAM-touched artifacts; space-free path), `ASTRA_SESSIONS_DIR` (design-session store — distinct from runs dir), `VSP_PYTHON`, `FOAM_BASHRC`, `WM_PROJECT_DIR`, `MCP_TRANSPORT/HOST/PORT`.
- Entry points: `foambench_main.py` (inherited CLI → `src/main.py`), `app.py` (health stub, NOT the cockpit), `init_database.py`.

## 16. Adding a domain pack

- No registry/auto-discovery — packs are imported by explicit path. The BWB template: `schema.py` (pydantic-only, importable from both Python envs) + `defaults.py` (deterministic sizing) + subprocess CLI scripts (`vsp_builder.py`, `vsp_aero.py`, `gmsh_farfield.py`).
- A new pack needs: (a) schema/defaults, (b) subprocess CLI builders, (c) service wrappers in `src/services/` (mirror `geometry.py`/`vspaero.py`/`bwb_mesh.py` with their `_BUILDER`/`_RUNNER`/`_MESHER` path constants), (d) MCP tools in `fastmcp_server.py`.
- Importers of `domain_packs.bwb.*` today: intent, geometry, vspaero, bwb_mesh, evaluate services; cockpit server; MCP server; all three verification scripts.
