# MDO Parametric Optimizer Implementation Plan

Adds AeroSandbox as a fast optimize-and-screen tier between the OpenVSP base wing
and CFD, driven by a video-style MDO objective. Design spec:
[`specs/2026-07-21-mdo-parametric-optimizer-design.md`](../specs/2026-07-21-mdo-parametric-optimizer-design.md).

Final loop (unchanged except the one NEW optional step):

```
requirement → intent/physics → OpenVSP base wing (BWBParams, gated)
   → AeroSandbox fast optimize (NEW, optional)
   → VSPAERO confirm (winners only) → OpenFOAM CFD
```

## Global constraints

- **Do not touch the inherited LangGraph CFD spine.** Attach before it, like the
  candidate funnel (`src/services/candidates.py`) does.
- **No LLM step load-bearing for correctness.** Score, penalties, and search are
  deterministic; the LLM only proposes/explains config.
- **Domain knowledge stays in `domain_packs/bwb/`**; pydantic/numpy only there (no
  OpenVSP imports — dual-env safe).
- **`BWBParams` is the single source of truth.** Both OpenVSP and AeroSandbox derive
  geometry from it; reuse `schema.py` validators as the feasibility gate.
- **New dependency `aerosandbox` on the py3.12 side only** (`environment.yml`,
  `pyproject.toml`). NeuralFoil ships with it — no XFLR5/XFOIL. `scipy` already pinned.
- Every score is a full inspectable breakdown dict; frozen params never move; gated
  designs are never scored feasible.

---

## Slice 1 — AeroSandbox evaluator + MDO objective (backend, no UI)

Goal: score any `BWBParams` in ~1s with a full penalty breakdown, cross-checked
against VSPAERO. Structural penalties present but at weight 0.

**Status: implemented 2026-07-21.** Files: `domain_packs/bwb/asb_aero.py` (block A),
`domain_packs/bwb/objective.py` (block C), `src/services/asb_aero.py` (service),
`score_current_geometry` MCP tool, `tests/test_bwb_objective.py` (12 tests, no ASB
needed), `tests/test_bwb_asb_aero.py` (4 tests), `verification/verify_asb_aero.py`.
Deps added to `environment.yml`/`pyproject.toml` (aerosandbox 4.2.8, neuralfoil 0.3.3,
casadi 3.7.2). Reference wing (AR 7, cruise CL≈0.33) evaluates to L/D≈20 at α_trim≈3.4°
with a positive static margin; all 16 new tests pass.

Refinements adopted during implementation (spec updated to match):
- `score(params, aero, weights, config)` takes the **already-computed** aero result
  (dependency injection) rather than computing it internally — keeps `objective.py`
  import- and test-clean without the heavy aero stack (verified: importing
  `objective`/`asb_aero` does not load aerosandbox).
- The lateral-directional **hard floor is opt-in** (`enforce_latdir_floor`, default
  off). A wing-only BWB reads slightly negative `Cn_beta` in this low-fidelity buildup
  (directional stability comes from effects the component model under-captures), so the
  floor is reference-relative and only enabled once the reference wing's `Cn_beta` is
  measured during search setup (Slice 2). The w6 penalty rewards directional stability
  regardless.
- CG is fixed at `cg_frac_mac` behind the MAC leading edge (default quarter-MAC); the
  static margin emerges from the swept planform, making pitch stability a real
  discriminator. AeroSandbox supplies the neutral point `x_np` directly.
- NACA section thickness is rounded to the nearest integer percent — a documented
  low-fidelity simplification for this fast tier.
- Scoring is deliberately NOT a pipeline stage (not in `design_session.STAGES`): it is
  a read-only screen that never invalidates mesh/CFD. The artifact is written to
  `<session>/asb_aero/asb_aero.json` without stage bookkeeping.

### Task 1.1 — Dependency + environment
- Add `aerosandbox` to `environment.yml` and `pyproject.toml` (py3.12 FoamAgent env).
- Confirm `python verification/doctor.py` still passes; confirm `import aerosandbox`
  and `import neuralfoil` in the py3.12 env.

### Task 1.2 — Block A: `domain_packs/bwb/asb_aero.py`
- `bwbparams_to_airplane(p: BWBParams) -> asb.Airplane`: build a 3-section wing
  mirroring `vsp_builder.py` station-for-station —
  - XSec_0 root: `c_root`, twist `root_twist_deg`, NACA-4 `root_thickness_pct`.
  - XSec_1 blend: `c_mid`, twist `root + (tip−root)·centre_body_fraction` (same
    interpolation as `vsp_builder.py:52`), NACA-4 mean thickness.
  - XSec_2 tip: `c_tip`, twist `tip_twist_deg`, NACA-4 `tip_thickness_pct`.
  - Panel spans `b1, b2` from `half_widths()`; inner/outer LE sweep; `dihedral_deg`.
  - Reuse `chords()`, `half_widths()` so ASB and the analytic planform never diverge.
- `evaluate(p, derived: MissionDerived) -> AsbAeroResult`: run VLM at the mission
  `required_cl`/Reynolds with a NeuralFoil viscous build-up; return
  `CL, CD, L_over_D, Cm_alpha, static_margin, Cn_beta, Cl_beta, spanwise_lift[]`.
- Pure, in-process, deterministic. Pydantic `AsbAeroResult` model.

### Task 1.3 — Block C: `domain_packs/bwb/objective.py`
- `ObjectiveWeights` pydantic model: `w1..w9`, structural `w1..w5` **default 0.0**.
- `ObjectiveConfig`: mission targets (`required_cl`, `static_margin_target`, reference
  `Cn_beta` floor, `s_ref` target) pulled from `MissionDerived`.
- `score(params, weights, config) -> ScoreBreakdown` implementing the spec's terms:
  aero `L/D` minus weighted penalties `w1·|M_root| + w2·U_structure + w3·σ_peak +
  w4·σ_CV + w5·shape_penalty_3d + w6·latdir + w7·pitch + w8·trim + w9·geometry`.
  Structural terms compute a placeholder of 0 (block B absent) but keep the slots.
- `ScoreBreakdown`: `score`, per-term contributions, `feasible: bool`, reject reason.
- Feasibility = `BWBParams` validators pass + geometry gates + `Cn_beta ≥` reference
  floor; infeasible → `feasible=False`, excluded from any ranking.

### Task 1.4 — Service wrapper: `src/services/asb_aero.py`
- Session-aware wrapper (artifacts, provenance, caching) around block A, shaped like
  `src/services/vspaero.py`. Writes an `asb_aero/` artifact record per evaluation.

### Task 1.5 — MCP tool `score_current_geometry`
- In `src/mcp/fastmcp_server.py`: score the session's current `BWBParams`, return the
  breakdown. Read-only, no LLM in the correctness path.

### Task 1.6 — Validation (gate for trusting the search)
- ASB-vs-VSPAERO cross-check on the reference wing: `L/D`, `static_margin`, `Cn_beta`
  agree within a documented tolerance. Record in `reports/`.
- Unit tests: penalty monotonicity per term, breakdown sums to `score`, infeasible
  designs flagged, deterministic output for fixed input.

**Slice 1 exit:** `score_current_geometry` returns a trustworthy breakdown validated
against VSPAERO; structural slots present and zero-weighted.

---

## Slice 2 — Optimizer + candidate integration

Goal: "optimize around this wing" returns ranked candidates in the existing navigator,
promotable through VSPAERO → CFD.

**Status: implemented 2026-07-21.** Files: `domain_packs/bwb/design_space.py`,
`src/services/optimize.py` (block D), `add_mdo_candidates()` in
`src/services/candidates.py`, `optimize_around_wing` MCP tool. Tests:
`tests/test_bwb_design_space.py` (11, pure), `tests/test_bwb_optimize.py` (6, ASB-gated),
`tests/test_bwb_mdo_candidates.py` (3, funnel write). All pass (20 new; 55 total in the
BWB suite). A small deterministic search improves the reference wing (score 17.62 → 18.11,
L/D 19.74 → 20.0, static margin toward target) at ~1s per candidate.

Verified end to end here (no PC/WSL needed — pure AeroSandbox + scipy): the winner passes
the geometry gate by construction, frozen params stay fixed, top-N are distinct and
feasible, the same seed reproduces the winner, and winners land in the candidate navigator
(`source="mdo"`) coexisting with the equation lenses.

Refinements adopted (spec updated):
- **Reference-relative stability floor wired here**: `optimize()` evaluates the base wing
  first and sets `config.cn_beta_floor = base.Cn_beta` with `enforce_latdir_floor=True`, so
  no winner is less directionally stable than the reference (the Slice-1 opt-in floor is
  activated at search setup, exactly as designed).
- `optimize(derived, space=None, weights, config, cg_frac_mac, spread, seed, maxiter,
  popsize, tol, top_n, polish, reference_latdir_floor, on_generation)` — takes `derived`
  (not `sid`), so it is session-store-agnostic and unit-testable. `polish` is exposed
  (L-BFGS-B finish) and turned off for fast test runs.
- **Candidate write** = `candidates.add_mdo_candidates(sid, winners)`: attaches the analytic
  `estimate()` panel so MDO records are shape-identical to lens candidates (build / screen /
  promote unchanged); idempotent on re-run (replaces `mdo_*`, preserves lenses).
- The MCP tool seeds the lens batch (`generate()`, idempotent) before writing MDO winners so
  the navigator always shows both. Note: `generate()` short-circuits once any batch index
  exists — MDO and lenses share one per-plan index, so generate lenses first (the natural
  "Suggest designs → optimize" order) or pass `regenerate=True`.
- Infeasible / out-of-gate vectors cost a large constant so DE steers away; only feasible
  designs enter the winner pool, deduped by rounded parameter vector.

### Task 2.1 — `domain_packs/bwb/design_space.py`
- `DesignSpace`: per-`BWBParams`-field `(lo, nominal, hi, frozen)`, `nominal` = base
  wing, `lo/hi` validated inside `BWBParams` field ranges; frozen fields fixed at
  nominal and excluded from the search vector.
- `from_base(params, spread) -> DesignSpace` default: bounds centered on the base wing.

### Task 2.2 — Block D: `src/services/optimize.py`
- `optimize(sid, space, weights, config, seed) -> OptimizeResult` using
  `scipy.optimize.differential_evolution` over the unfrozen dimensions.
- Objective = block C; each eval = block A; infeasible designs penalized to the floor
  so DE steers away. Fixed `seed` → reproducible winner.
- Stream best-so-far (callback) for later UI; collect top-N distinct feasible designs.

### Task 2.3 — Candidate funnel integration
- Write the top-N as candidates via `src/services/candidates.py` (same records as
  `generate_candidates`), tagged `source="mdo"`, so they appear in the candidate
  navigator and promote via the existing "Verify with solver" (VSPAERO) → CFD path.
- VSPAERO now runs on **winners only**, not per candidate.

### Task 2.4 — MCP tool `optimize_around_wing`
- Params: optional per-field overrides, weights, freezes, seed, budget. Returns ranked
  breakdowns + candidate keys. Deterministic.

### Task 2.5 — Tests
- Determinism (fixed seed → same winner), winner passes all geometry gates, frozen
  params unchanged, top-N are distinct and feasible, VSPAERO confirmation path intact.

**Slice 2 exit:** a chat/MCP call optimizes around the base wing and drops ranked,
gate-passing candidates into the navigator for solver confirmation.

---

## Slice 3 — Cockpit UI (optional enhancement)

Goal: make the search visible and tunable without chat.

**Status: implemented 2026-07-21.** Backend: `domain_packs/bwb/objective_presets.py`
(3 priorities -> 9 weights, the "fewer options" layer), and cockpit endpoints in
`src/cockpit/server.py` — `POST /api/mdo/propose` (auto-fill), `/api/mdo/score` (live
score), `/api/mdo/optimize` (SSE stream of the search). `optimize()`'s per-generation
callback now carries the top-N gallery tiles. Frontend: `src/cockpit/static/mdo.js`
(+ `mdo.css`, wired into `index.html` with a "✦ Optimize (MDO)" button) — a compact
panel (quick-pick presets · efficiency/stability/buildable sliders · explore amount ·
Advanced freeze/seed/budget · live score) and a full-viewport differential-evolution
gallery that renders each candidate wing from its parameters (same canvas renderer as
the mockup, no OpenVSP) with its score, winner highlighted, streaming per generation.
Winners land in the existing candidate navigator via `add_mdo_candidates`.

Tests: `tests/test_bwb_objective_presets.py` (7). `mdo.js` node-syntax-checked; the
per-generation tile stream verified in Python. 79 tests pass across the suite.

Verified in a browser harness (mock endpoints + the real 12-candidate data, served
statically) driven through the Browser pane: panel opens, presets load, the live score
computes (balanced -> 19.66), "Search designs" streams generations, the gallery renders
all 9 candidate wings (canvas pixels confirmed painted), the winner is highlighted
(#f5d21e), status reaches "converged", and the footer shows "5 winners added to
candidates · score 18.7 → 25.1". No console errors. (The full cockpit itself runs in the
WSL FoamAgent env — `astra-cockpit` in `.claude/launch.json` — which also needs OpenVSP;
the harness verifies the new UI in isolation.)

Refinements adopted:
- **Draw synchronously, not via `requestAnimationFrame`** — rAF is throttled in hidden
  tabs, which left the tile canvases blank; a direct draw (clientWidth forces layout) is
  robust regardless of tab visibility.
- **Options collapse in the panel, not the engine**: three priorities map to the nine
  weights in `objective_presets`; structural weights stay hidden (dormant); the full
  12-slider / raw-weight control is intentionally not exposed (Advanced holds only
  freeze + budget). The engine still supports everything; the UI shows only what changes
  the answer.
- Search runs off the **current** wing (`_current_params`), so "optimize this wing"
  means the geometry on screen, edits included.

### Task 3.1 — Design-space panel
- Each `BWBParams` field → range control (lo · nominal · hi + freeze). Nominal drives
  the existing live 3D preview. Mirrors the video's slider stack.

### Task 3.2 — Objective panel + live score
- Auto-proposed weights as plain-language rows (draggable). Live `SCORE = …` readout
  with the penalty breakdown, mirroring the video's Step 2 screen.
- MCP `propose_objective` fills defaults from the mission; chat Q&A (what matters most,
  hard limits, which params to freeze) sets config deterministically.

### Task 3.3 — Run + stream
- "Search designs" calls `optimize_around_wing`, streams best-so-far into the HUD,
  winners into the candidate navigator.

### Task 3.4 — Differential-evolution search gallery (the video's "Step 3" screen)
- While the search runs, show a **grid of candidate wings** — each rendered live from
  its `BWBParams` via the existing in-browser parametric preview (NO OpenVSP/STL per
  tile, so it stays fast) — with its **MDO score** beneath it and the **current best
  highlighted** (the video's yellow-bar marker). The grid refreshes each generation.
- Streaming contract: `optimize()` emits, per generation, the top-N distinct feasible
  candidates as `(params, score)` pairs (it already tracks these in `seen` + the
  `on_generation` callback — extend the callback payload from `(gen, best, n_evals)` to
  also carry the top-N tiles). The cockpit server relays them to the panel (SSE/WS or
  poll, matching the existing cockpit transport).
- Population is ~popsize·dims (~144); render only the top-N tiles (default 9–12), not
  the whole population. Clicking a tile promotes that design into the candidate
  navigator; the winner tile is the same design `add_mdo_candidates` writes as `mdo_1`.

**Slice 3 exit:** the full set-up-and-search loop is usable from the cockpit, including
the live DE gallery that shows candidate wings and scores while the winner is selected.

---

## Future phases (planned, not in this plan's slices)

- **Structures ON** — build `domain_packs/bwb/beam_structure.py` (equivalent single
  beam fed by ASB spanwise lift → `M_root`, deflection, `σ_peak`, `σ_CV`), then raise
  weights `w1–w5` off 0. No objective refactor (slots already present). Labeled
  low-fidelity engineering-judgment; separate from reserved CalculiX/FEA (roadmap §6).
- **16-parameter extended geometry** — richer span-station parametrization matching the
  video: 1 LE sweep + 3 LE notches + 3 chord-distribution + 4 twist stations + 5
  dihedral stations. Adds a new station schema, a matching OpenVSP builder and ASB
  adapter, and extended geometry gates — introduced behind the existing `DesignSpace`
  and `objective` interfaces so the optimizer and MDO need no rework.
- **Surrogates / cross-fidelity trust policy** (roadmap phase 5) — replace direct ASB
  calls in DE with surrogate-guided search.
