# Astra MDO Parametric Optimizer — design spec

Status: proposed (2026-07-21, reconciled to final decisions). Adds a fast,
low-fidelity **optimize-and-screen tier** between OpenVSP geometry and CFD,
modeled on the Neuronautics "organic flying wing" workflow (parametric wing →
`score = aero_efficiency − Σ wᵢ·penaltyᵢ` → evolutionary search → confirm with the
existing solvers). Pulls roadmap phases 4–5 forward in the shape of a single
multidisciplinary objective.

## Product goal

Let a user optimize a parametric BWB **around an already-built base wing** and get
back ranked candidates found by fast evolutionary search, with the objective
auto-proposed from the mission and customizable in plain language. Each candidate
must evaluate in ~1s so a search of thousands of designs is feasible on a modest
machine, and every score must be a deterministic, inspectable breakdown (no LLM
step load-bearing for correctness).

## Where it sits — the loop is unchanged, one optional step is inserted

The current Astra loop stays exactly as is. AeroSandbox is an **optional step after
the base wing is built**, optimizing in parameter space around it:

```
requirement → intent/physics → OpenVSP base wing (BWBParams, gated)
   → AeroSandbox fast optimize (NEW, optional)
   → VSPAERO confirm (winners only) → OpenFOAM CFD
```

Roles after the change:
- **AeroSandbox** — fast inner-loop search: thousands of evals, analytic geometry
  derived from `BWBParams`, in-process (py3.12), no OpenVSP subprocess in the loop.
- **OpenVSP** — builds the base wing once (also the one-time validation anchor) and
  the winners' real geometry. Unchanged.
- **VSPAERO** — demoted from per-candidate screening to a **winners-only confirmation
  gate** on real geometry before CFD. Still fully kept.
- **OpenFOAM CFD** — final high-fidelity check. Unchanged.
- **`BWBParams`** (`schema.py`, dual-env pydantic) — single source of truth that both
  OpenVSP and AeroSandbox derive from.

Nothing in the inherited LangGraph CFD spine changes; this attaches before it, in the
pre-CFD design-exploration stage, exactly like the candidate funnel already does.

## Decisions (2026-07-21, final)

1. **Reuse the existing 12-variable `BWBParams`** as the design vector. No new
   station-based schema in v1 — the two-trapezoid-panel model already satisfies the
   geometry gates, VSPAERO, and OpenFOAM. Interfaces are written so the extended
   16-parameter station schema (future phase) drops in behind them without rework.
2. **Backend engine first.** Ship and validate the AeroSandbox evaluator + objective
   before any UI. Validate against VSPAERO on the reference wing (the video's
   VLM-vs-higher-fidelity handshake) so the fast score is trustworthy.
3. **Structural penalties are present but dormant.** The objective keeps the full
   video-style term structure — `|M_root|`, deflection, `σ_peak`, `σ_CV`, 3D-shaping —
   but at **weight 0**, so they are wired-but-off. The beam surrogate that would feed
   them (block B) is **not built in v1**; it and the non-zero weights are a future
   phase. This keeps the objective's shape faithful to the image without modeling
   structures yet, and enables them later with no refactor.
4. **AeroSandbox is the fast aero+stability engine.** Pure numpy, installs in the
   py3.12 FoamAgent env, no OpenVSP subprocess. Viscous section drag via **NeuralFoil**
   (bundled with AeroSandbox) — no XFLR5/XFOIL dependency. `scipy` (already pinned,
   1.15.2) supplies `differential_evolution`.
5. **The optimizer's winners are ordinary Astra candidates.** They appear in the
   existing candidate navigator (same slot as "Suggest designs") and promote through
   the existing "Verify with solver" path to VSPAERO then OpenFOAM.
6. **UI is deferred.** v1 introduces the optimizer backend-first via an MCP/chat
   "optimize around this wing" tool. A dedicated slider/objective panel is a later
   optional enhancement (Slice 3).

## Architecture

Domain-pack side (shared, pydantic/numpy only — no OpenVSP imports):
- `domain_packs/bwb/asb_aero.py` — **block A**: adapter `BWBParams → AeroSandbox
  Airplane`, built analytically and mirroring `vsp_builder.py` **station-for-station**
  (XSec_0 root / XSec_1 blend / XSec_2 tip: chords `c_root/c_mid/c_tip`, per-station
  twist with the same blend interpolation, dihedral, NACA-4 thickness `root/mid/tip`).
  Returns `CL, CD, L/D, Cm_alpha` (→ static margin), `Cn_beta`, `Cl_beta`, spanwise
  lift. NeuralFoil for viscous drag.
- `domain_packs/bwb/objective.py` — **block C**: deterministic `score(params, weights,
  config) → (score, breakdown)` with the full penalty set below; pure, unit-testable.
- `domain_packs/bwb/design_space.py` — `DesignSpace` model: per-field `(lo, nominal,
  hi, frozen)` bounds centered on the base wing, validated against `BWBParams` ranges.
- `domain_packs/bwb/beam_structure.py` — **block B, FUTURE**: equivalent-beam surrogate
  feeding the dormant structural penalties. Not built in v1.

Service side:
- `src/services/asb_aero.py` — session-aware wrapper (artifacts, provenance, caching)
  around the block-A evaluator, matching the shape of `vspaero.py`.
- `src/services/optimize.py` — **block D**: `scipy.optimize.differential_evolution`
  over a `DesignSpace`, objective = block C, each eval = block A. Streams best-so-far;
  winners written as candidates via `services/candidates.py`.

MCP + cockpit:
- New MCP tools in `src/mcp/fastmcp_server.py`: `optimize_around_wing` (run the search),
  `score_current_geometry` (single-point score + breakdown), and later
  `propose_objective`. Winners land in the candidate navigator; promotion reuses the
  existing verify path.
- Cockpit panel (Slice 3, optional): range sliders + auto-proposed objective + live
  score. Reuses the live parametric preview and "Why — first principles" panel.

## The objective function

`score = aero_efficiency − PENALTIES`, all terms returned in an inspectable breakdown.

Aero efficiency: `L/D` at the mission `required_cl` and Reynolds (from `MissionDerived`),
viscous drag via NeuralFoil.

Penalties (each `wᵢ · termᵢ`; **structural terms w1–w5 default to 0 in v1**):
- `w1·|M_root|` — root bending moment (beam surrogate, dormant)
- `w2·U_structure` — tip deflection / compliance (dormant)
- `w3·σ_peak` — peak stress, failure proxy (dormant)
- `w4·σ_CV` — stress non-uniformity, coefficient of variation (dormant)
- `w5·shape_penalty_3d` — 3D-shaping / dihedral curvature material penalty (dormant)
- `w6·latdir_stability` — lateral-directional: reward restoring yaw moment; must be at
  least as stable as the reference wing (hard floor on `Cn_beta`)
- `w7·pitch_stability` — require positive static margin toward `static_margin_target`
- `w8·trim` — control deflection to trim level flight (adds drag)
- `w9·geometry_constraints` — reference area held at target; gate-proximity terms

Hard constraints (design rejected, not just penalized): existing `BWBParams`
validators + geometry gates + positive static margin + the lateral-directional
"no worse than reference" floor. Reuses `schema.py` cross-checks, so the optimizer can
never promote an ungateable design.

Implementation notes (v1):
- `score(params, aero, weights, config)` takes the already-computed `AsbAeroResult`
  (dependency injection), keeping `objective.py` free of the aero stack for testing.
- The lateral-directional floor is **opt-in** (`enforce_latdir_floor`, default off) and
  reference-relative: a wing-only BWB reads slightly negative `Cn_beta` in this
  low-fidelity buildup, so the floor is set from the reference wing's measured
  `Cn_beta` at search setup. The w6 penalty always rewards directional stability.
- CG is fixed at `cg_frac_mac` behind the MAC LE (default quarter-MAC); static margin
  emerges from the swept planform. AeroSandbox supplies `x_np` directly.
- Scoring is a read-only screen (not a `design_session` stage) — it never invalidates
  mesh/CFD.

## Validation

- **ASB-vs-VSPAERO cross-check** on the base/reference wing via
  `verification/verify_asb_aero.py --session <SID>`: compares L/D at trim, trim alpha,
  lift-curve slope, and neutral-point location against that session's VSPAERO
  `fast_aero.json` within documented trend tolerances (L/D ±25%, α_trim ±2°, CL_alpha
  ±20%, x_np ±10% MAC). Runs in the FoamAgent env; needs a session with built geometry
  and a VSPAERO run.
- **`objective.py` unit tests**: penalty monotonicity, breakdown sums to score, frozen
  params never move, gated designs never scored feasible.
- **Optimizer determinism**: fixed seed → reproducible winner that passes all geometry
  gates by construction.

## Implementation plan (summary; full plan in the plans/ doc)

- **Slice 1** — block A (AeroSandbox evaluator) + block C (objective, structural
  weights 0), MCP `score_current_geometry`, `aerosandbox` added to env; validated vs
  VSPAERO on the reference wing. No UI.
- **Slice 2** — block D (optimizer) + `design_space.py` + MCP `optimize_around_wing`;
  winners into the candidate funnel, promotable via the existing verify path.
  *Implemented 2026-07-21; verified with 20 tests. `optimize()` sets the
  reference-relative Cn_beta floor from the base wing; `candidates.add_mdo_candidates`
  writes winners (source="mdo") shape-identical to lens candidates.*
- **Slice 3 (optional)** — cockpit design-space + objective panel, live score,
  best-so-far streaming, chat Q&A that fills the config.
  *Implemented 2026-07-21: `objective_presets` collapses 9 weights to 3 plain-language
  priorities; `/api/mdo/{propose,score,optimize}` (SSE) back a compact panel + live
  differential-evolution gallery (`mdo.js`/`mdo.css`). Winners flow to the candidate
  navigator. Verified via a browser harness; the full cockpit runs in the WSL env.*

## Future phases (planned, not v1)

- **Structures ON** — build block B (beam surrogate), enable structural penalty
  weights `w1–w5`. Labeled low-fidelity engineering-judgment; distinct from the
  reserved CalculiX/FEA structures work (roadmap phase 6).
- **16-parameter extended geometry** — richer span-station parametrization matching
  the video (1 LE sweep + 3 LE notches + 3 chord-distribution + 4 twist stations + 5
  dihedral stations). Requires a new station schema, a matching OpenVSP builder and
  ASB adapter, and extended geometry gates. Introduced behind the same `DesignSpace` /
  objective interfaces so the optimizer and MDO need no rework.
- **Surrogate models / cross-fidelity trust policy** (roadmap phase 5) — DE calls ASB
  directly in v1; surrogates come later.
