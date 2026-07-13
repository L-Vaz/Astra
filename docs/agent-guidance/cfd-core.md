# Inherited Foam-Agent CFD core

Read this when changing the LangGraph workflow, inherited agents, routing, RAG, or shared graph state.

## Graph

`src/main.py` defines the inherited workflow:

```text
PLANNER -> mesh routing -> MESHING -> INPUT_WRITER -> HPC/local routing
        -> RUNNER -> error? -> REVIEWER -> INPUT_WRITER (retry loop)
        -> VISUALIZATION if requested -> END
```

The graph is Astra's CFD spine and should remain unchanged for orchestration. Astra attaches before and around it. Keep fork hardening minimal for upstream mergeability. Routing in `src/router_func.py` is LLM-driven, so consequential routes require deterministic safeguards.

## Responsibilities

- Planner: natural-language requirement to structured plan using classification and RAG.
- Meshing: standard OpenFOAM mesh, supplied custom `.msh`, or LLM-authored Gmsh script.
- Input writer: ordered generation of `system/`, `constant/`, `0/`, then `Allrun`.
- Runner: local or HPC execution and error collection.
- Reviewer: minimal corrections and retry until success or the configured limit.
- Visualization: PyVista/ParaView rendering with correction support.

Astra uses the supplied custom-mesh path. The graph converts and locks the approved mesh; the reviewer must not remesh it.

## Code map

- `src/config.py`: configuration and `FOAMAGENT_*` environment overrides.
- `src/router_func.py`: LLM routing.
- `src/utils.py`: mutable, by-reference `GraphState` and LLM service types.
- `src/nodes/`: thin graph wrappers.
- `src/services/`: real logic; place new behavior here.
- `src/mcp/fastmcp_server.py`: stage-level tools.
- `database/faiss/`: four prebuilt hierarchical RAG indices; do not regenerate without a specific reason.

For a new graph stage, implement a service, add a thin node wrapper, add required `GraphState` fields, and wire it in `src/main.py`. For stage-level external orchestration, expose a service through the MCP server instead.

## Facts to verify in live code

Live code outranks prose. At the 2026-07-10 audit the MCP server had 12 tools (six inherited and six Astra), `max_loop` was 10, embeddings defaulted to `Qwen/Qwen3-Embedding-0.6B` on CPU, and the default LLM setting was `openai-codex`/`gpt-5.3-codex`. Recheck `src/config.py` and `src/mcp/fastmcp_server.py` before relying on these values.
