# Astra agent guidance

Astra extends Foam-Agent into a conversational, reviewable blended-wing-body drone design loop: mission intent → parametric geometry → fast aero → mesh → OpenFOAM CFD → physical-quality verdict.

Live code is authoritative. When sources disagree, trust live code, then this file, then linked guidance, then the inherited Foam-Agent paper. Verify configuration claims in `src/config.py` and tool claims in `src/mcp/fastmcp_server.py`.

## MUST preserve

- The inherited Foam-Agent LangGraph graph is the CFD spine. Astra attaches before and around it; do not redesign graph orchestration for Astra stages.
- No LLM step may be load-bearing for correctness. Back probabilistic routing and generated configuration with deterministic validation, fallbacks, and gates.
- Domain knowledge belongs in `domain_packs/<domain>/`. Add a domain pack rather than a domain-specific agent.
- Keep the scope aerodynamics-first and parametric. Structures are reserved; general text-to-CAD is out of scope.
- Cite physics and numerical choices to official sources or label them explicitly as engineering judgment.

## Daily commands

Run from the repository root in the configured WSL environment:

```bash
python verification/doctor.py
python foambench_main.py --output ./output --prompt_path ./user_requirement.txt
python verification/verify_golden.py --no-llm
python verification/verify_golden.py
python verification/verify_revisions.py
python verification/verify_gates.py
```

`doctor` is a seconds-fast environment preflight (paths, both Python envs, OpenFOAM, FAISS, credentials) — run it first on a fresh or changed environment. The last two verification scripts require a completed golden case. Full CFD verification can use API credentials and take substantial time; use the deterministic `--no-llm` golden run for a quick pipeline check.

## Code map

- `src/main.py`: inherited LangGraph workflow.
- `src/router_func.py`: LLM-driven routing.
- `src/utils.py`: mutable, by-reference `GraphState`.
- `src/nodes/`: thin graph wrappers; keep business logic out.
- `src/services/`: inherited CFD services and Astra stage logic.
- `src/mcp/fastmcp_server.py`: inherited and Astra MCP tools.
- `domain_packs/bwb/`: BWB schema, assumptions, OpenVSP/VSPAERO, and Gmsh knowledge.
- `verification/`: golden-path, revision, and adversarial-gate checks.
- `reports/`: verification results and source accounting.
- `database/faiss/`: prebuilt RAG indices; do not regenerate without a specific reason.

For a new graph stage: service → thin node → required `GraphState` fields → wiring in `src/main.py`. For external stage-level orchestration, expose the service through the MCP server.

## Task → files (start here; skip repo-wide search)

Detailed maps with roles, wiring, and caveats per task: [`docs/agent-guidance/task-maps.md`](docs/agent-guidance/task-maps.md). For planning, read that file's relevant section first and open only the listed files; fan out into other guidance docs only if the plan requires it.

| Task | Go straight to |
| --- | --- |
| Mission intent / requirement parsing | `src/services/intent.py`, `domain_packs/bwb/defaults.py`, `domain_packs/bwb/schema.py` |
| Drone geometry / parametric changes | `domain_packs/bwb/schema.py`, `domain_packs/bwb/vsp_builder.py`, `src/services/geometry.py` |
| Fast aero (VSPAERO) | `domain_packs/bwb/vsp_aero.py`, `src/services/vspaero.py` |
| Farfield meshing (Astra, deterministic) | `domain_packs/bwb/gmsh_farfield.py`, `src/services/bwb_mesh.py` |
| Graph mesh conversion (in-sim) | `src/nodes/meshing_node.py`, `src/services/mesh.py` |
| Simulation bridge / add a graph stage | `src/services/simulate.py`, `src/main.py`, `src/utils.py`, `src/router_func.py` |
| CFD case planning / input writing | `src/services/plan.py`, `src/services/input_writer.py`, their nodes |
| Solver runs (local/HPC) | `src/services/run_local.py`, `src/services/run_hpc.py` |
| Review / error-correction loop | `src/services/review.py`, `src/nodes/reviewer_node.py`, `src/router_func.py` |
| Evaluation / verdict / visualization | `src/services/evaluate.py`, `src/services/visualization.py` |
| MCP tools | `src/mcp/fastmcp_server.py` |
| Cockpit UI | `src/cockpit/server.py`, `src/cockpit/static/cockpit.js` |
| Sessions / revisions / rollback | `src/services/design_session.py` |
| Verification & gates | `verification/`, `reports/` |
| RAG / FAISS | `src/utils.py`, `database/`, `init_database.py` |
| Config / env knobs | `src/config.py`, task-maps §15 |

## Critical caveats

- Put all OpenFOAM-touched runtime artifacts under `~/astra_runs`, never the space-containing repository path. Override with `ASTRA_RUNS_DIR`.
- In requirements say “a custom mesh file is provided—use it as-is.” Calling it a “Gmsh mesh” can select the wrong LLM route.
- OpenVSP bindings require Python 3.10. Invoke `/opt/vsp-venv/bin/python` as a subprocess; do not import them into the Python 3.12 FoamAgent environment.
- Preserve deterministic custom-mesh conversion, wall-patch classification, Netgen optimization, bounded schemes, and post-hoc evaluator fallbacks.
- `GraphState` is mutable and passed by reference; check for unintended side effects.
- A clean solver exit is not proof of valid physics. Keep result-level convergence, plausibility, mission, and visualization checks.

## Read only when relevant

- BWB stages, MCP orchestration, evaluation, rollback, or verification: [`docs/agent-guidance/bwb-design-loop.md`](docs/agent-guidance/bwb-design-loop.md)
- Inherited graph, agents, routing, RAG, or shared state: [`docs/agent-guidance/cfd-core.md`](docs/agent-guidance/cfd-core.md)
- WSL/OpenFOAM/OpenVSP setup, meshing, numerics, or thresholds: [`docs/agent-guidance/environment-and-numerics.md`](docs/agent-guidance/environment-and-numerics.md)
- Feature scope and phase sequencing: [`docs/agent-guidance/roadmap.md`](docs/agent-guidance/roadmap.md)
- Verification evidence and citations: `reports/verification-2026-07-10/`

## Maintain this file

Keep this file at or below 150 lines. Include only guidance useful to most tasks; move specialized detail into a descriptive linked document. Do not add manual formatting rules—use project formatters and linters. After user approval, keep `CLAUDE.md` and `AGENTS.md` byte-for-byte identical.
