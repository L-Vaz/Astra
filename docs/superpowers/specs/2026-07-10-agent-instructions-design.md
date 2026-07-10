# Agent Instructions Redesign

## Goal

Make `CLAUDE.md` and `AGENTS.md` byte-for-byte identical, keep each at or below 150 lines, and apply the transcript's guidance by retaining only broadly useful project context while loading specialized instructions on demand.

## Root instruction file

The shared content will use neutral wording so it is valid under either filename. It will put the highest-priority information first and contain:

- a one-line project description and trust order;
- project-wide invariants that must not be violated;
- daily build, test, and verification commands confirmed from the repository;
- a compact architecture and path map;
- high-impact, non-obvious caveats;
- conditional links to detailed guidance documents;
- a maintenance rule requiring both files to remain byte-for-byte identical and under 150 lines.

It will not contain formatting preferences, long history, full roadmaps, detailed component explanations, or infrequently needed setup notes.

## Progressive disclosure

Existing detail will be preserved under `docs/agent-guidance/`, divided by when an agent needs it:

- `bwb-design-loop.md`: interaction model, BWB pipeline, intent and evaluator behavior, and verification status;
- `cfd-core.md`: inherited Foam-Agent graph, agents, RAG, layout, and corrected facts;
- `environment-and-numerics.md`: WSL/OpenFOAM/OpenVSP setup and hard-won meshing, solver, and evaluation lessons;
- `roadmap.md`: completed, planned, and reserved phases.

The root file will say exactly when to read each document. Detailed guidance remains available without occupying every task's initial context.

## Verification

After editing:

1. Compare cryptographic hashes and a byte-level comparison of `CLAUDE.md` and `AGENTS.md`.
2. Count lines and fail if the shared content exceeds 150 lines.
3. Check that every linked guidance file exists.
4. Review the final diff for accidental loss of project-specific facts.

No application code or runtime behavior is changed.
