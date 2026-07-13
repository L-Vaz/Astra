# Astra roadmap

Read this when scoping a new feature or deciding whether proposed work belongs in the current project phase.

## Status at 2026-07-11

1. Intent and geometry: built and verified. Casual mission prompts become explicit physics and gated OpenVSP BWB geometry.
2. Aero tiers and evaluation: built and verified. VSPAERO screening, Gmsh farfield meshing, inherited-graph CFD, visual evaluation, and trust verdicts are connected.
3. Aero Design Cockpit: next (pulled ahead of DOE on 2026-07-11). Browser-based interactive 3D workspace over the existing services, with chat as controller, deterministic rebalancing, live parametric preview, and half-model CFD export. Spec: `docs/superpowers/specs/2026-07-11-aero-design-cockpit.md`.
4. DOE and provenance: after the cockpit. Sample `BWBParams`, screen broadly with VSPAERO, select OpenFOAM cases, and record parameters, gates, outputs, and artifacts in a DuckDB/Parquet registry.
5. Surrogates and trust policy: planned. Combine RF/GP uncertainty, novelty, and cross-fidelity disagreement to select surrogate, VSPAERO, OpenFOAM, or rejection; optimize L/D, static margin, and a mass proxy.
6. Structures: reserved. Pressure export and CalculiX/topology work starts only after the aerodynamic loop is proven.

Keep current work aerodynamics-first. Preserve the inherited graph as the CFD spine, make correctness deterministic, and keep domain knowledge in replaceable domain packs.
