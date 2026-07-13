# BWB design loop

Read this when changing design-stage orchestration, BWB geometry, fast aero, simulation handoff, evaluation, rollback, or verification.

## Interaction model

Claude Code is the v1 interface. The pipeline is exposed as MCP tools and the chat is the approval gate. Step-by-step mode lets the user review each stage and revise it in plain language; autonomous mode runs the full chain and reports the evaluator verdict. Stage outputs are versioned. Revising an upstream stage creates a new version and invalidates downstream results while retaining old versions for comparison.

The heavy CFD stage still runs the inherited LangGraph pipeline internally. Stage-level orchestration belongs in the MCP layer so a later UI can reuse the same tools.

## Pipeline and ownership

```text
prompt -> intent -> geometry -> fast aero -> mesh -> simulation -> evaluate -> verdict
```

- `src/services/intent.py` and `domain_packs/bwb/{schema,defaults}.py`: convert a casual mission into explicit physics, constraints, and logged assumptions.
- `src/services/geometry.py` and `domain_packs/bwb/vsp_builder.py`: OpenVSP geometry, artifacts, renders, and gates.
- `src/services/vspaero.py` and `domain_packs/bwb/vsp_aero.py`: VSPAERO sweep, trim, stability, and L/D estimates.
- `src/services/bwb_mesh.py` and `domain_packs/bwb/gmsh_farfield.py`: deterministic farfield mesh generation.
- `src/services/simulate.py`: hands the approved mesh and expert requirement to the inherited graph.
- `src/services/evaluate.py`: extracts results, renders reports, applies physics-quality gates, and returns `pass`, `revise-numerics`, `revise-design`, or `reject`.
- `src/services/design_session.py`: per-stage versions and rollback.
- `src/cockpit/`: Aero Design Cockpit — FastAPI server + three.js browser UI over the same session store. Live parametric preview (equation mirror of the schema), fixed rebalancing rules for derived controls (S scales chords at fixed span; AR adjusts span+chords at fixed S; locked params make rules refuse, never substitute), deterministic Apply → OpenVSP rebuild + gates, VSPAERO trigger, version history, and a chat-narration queue in `<session>/cockpit/explanations.jsonl`. Spec: `docs/superpowers/specs/2026-07-11-aero-design-cockpit.md`.
- `src/mcp/fastmcp_server.py`: seven Astra tools—`plan_design`, `build_geometry`, `run_fast_aero`, `generate_mesh`, `run_simulation`, `evaluate_results`, and `open_cockpit`—in addition to six inherited tools.

Domain knowledge belongs in `domain_packs/<domain>/`. A new engineering domain should add a pack, not another orchestration agent. Parametric families with tagged parameters are intentional; do not expand the project into general text-to-CAD.

## Correctness model

No probabilistic step may be load-bearing for correctness. Back LLM routing and generated configuration with deterministic validation, fallbacks, and gates. The evaluator must check physical quality even when the solver exits successfully. Every physics or numerics decision must be cited to official documentation or explicitly labelled engineering judgment.

## Verification

- `python verification/verify_golden.py --no-llm`: deterministic golden path without API calls.
- `python verification/verify_golden.py`: full configured-LLM golden path.
- `python verification/verify_revisions.py`: meaningful revision and rollback at each stage; requires a golden case.
- `python verification/verify_gates.py`: adversarial good/bad gate checks; requires a golden case.

The 2026-07-10 campaign recorded golden 29/30, revisions 13/14, and gates 13/13. The golden non-pass was a correctly detected design outcome (`meets_cl_target`), not a pipeline defect. Reports and sources are under `reports/verification-2026-07-10/`.
