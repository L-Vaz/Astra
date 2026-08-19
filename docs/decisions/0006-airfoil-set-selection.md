# 0006 — Add a deterministic Airfoil Sets approval gate

- **Status:** Accepted; implementation in progress
- **Date:** 2026-08-19
- **Owners:** Astra maintainers
- **Area:** Stage 1 airfoil selection, evidence, geometry propagation

## Problem

Stage 1 currently stores airfoil names in `BWBParams`, but OpenVSP does not construct those named sections. On the unmodified `192bf4b9a2708e5ecd6864ebfc6a64d1800c576e` baseline, the golden reference names are `e474`, `e472`, and `s5010`, while all three OpenVSP wing curves are generic four-series shapes.

Reproduction from the repository root:

```bash
/opt/vsp-venv/bin/python - <<'PY'
import json
import openvsp as vsp
from domain_packs.bwb.schema import BWBParams
from domain_packs.bwb.vsp_builder import build

raw = json.load(open("tests/data/aero_golden.json"))
p = BWBParams.from_dict(raw["params"])
out = build(p, "/tmp/astra_airfoil_before_33c7")
vsp.ClearVSPModel()
vsp.ReadVSPFile(out["vsp3"])
wing = vsp.FindGeoms()[0]
surf = vsp.GetXSecSurf(wing, 0)
print([p.airfoil_root, p.airfoil_mid, p.airfoil_tip])
print([vsp.GetXSecShape(vsp.GetXSec(surf, i)) for i in range(3)])
print(vsp.XS_FOUR_SERIES, vsp.XS_FILE_AIRFOIL)
PY
```

Observed `[C]`:

```text
['e474', 'e472', 's5010']
[7, 7, 7]
7 12
```

The builder also writes `ThickChord` values, which rescales the generic sections but cannot turn them into the named coordinate shapes. The defect is therefore demonstrated in the produced geometry, not inferred from code appearance.

## Severity and blast radius

Severity is **high for design traceability** `[D]`: airfoil-dependent polar estimates and the geometry evaluated downstream describe different section shapes. The blast radius covers Stage 1 concept geometry, VSPAERO runs, promotion manifests, and Stage 2 handoff/report evidence. It does not alter the inherited CFD LangGraph orchestration.

From first principles, a section name is meaningful only if both sides of the calculation use the same boundary shape. Pressure gradients, separation, lift, moment, and profile drag arise from the surface coordinates. Matching thickness alone does not make two cambers or leading-edge shapes aerodynamically identical.

## Evidence ledger

| Quantity or claim | Grade | Source / method | Decision use |
| --- | --- | --- | --- |
| Current curve types are `[7, 7, 7]` while `XS_FILE_AIRFOIL == 12` | `[C]` | Reproduction above with OpenVSP bindings and golden fixture | Proves the mismatch before its fix |
| Polar table contains 2,171 named sections and declares AeroSandbox 4.2.8 / NeuralFoil 0.3.3 | `[C]` | Committed `domain_packs/bwb/data/airfoils_neuralfoil.json`, verified by tests | Defines the parity target |
| `s5010` is a flying-wing section with measured low-speed polars and near-zero quarter-chord moment | `[A]` | Selig et al., *Summary of Low-Speed Airfoil Data, Vol. 2*, §4.3/Table 4.5 and Fig. 5.90 onward, UIUC: https://m-selig.ae.illinois.edu/pubs/Low-Speed-Airfoil-Data-V2.pdf | Exact fixture reference and evidence strength |
| `e472` has measured low-speed data; its listed geometry is 12.13% thick and symmetric | `[A]` | Selig et al., *Summary of Low-Speed Airfoil Data, Vol. 3*, Table 4.1 and Fig. 5.45 onward, UIUC: https://m-selig.ae.illinois.edu/pubs/Low-Speed-Airfoil-Data-V3.pdf | Exact fixture reference and evidence strength |
| UIUC lists coordinates for `e472`, `e474`, and `s5010`, but no exact experimental polar reference was located for `e474` | `[A]` for the database listing; evidence gap for a polar | UIUC Airfoil Coordinates Database: https://m-selig.ae.illinois.edu/ads/coord_database.html | Missing `e474` experiment weakens evidence only |
| Six retained candidates per station | `[D]` | Interactive exhaustive-search budget: at most 216 triples/pass | Must agree with a 10-candidate sensitivity fixture or warn |
| Five coupled resize/trim passes | `[D]` | Bounded-latency convergence judgement | Must agree with an 8-pass fixture or report non-convergence |
| Ten candidates and eight passes for sensitivity | `[D]` | Wider/higher comparison selected to challenge both production bounds | A changed result without a surfaced warning falsifies the production bounds |

## Decision drivers

- One airfoil schedule must be reviewable and approved before Concepts.
- Correctness cannot depend on an LLM choosing or interpreting a section.
- The section used in polar evidence must be the section built in OpenVSP.
- Whole-wing consequences matter more than independent station scores.
- Missing experimental evidence must be visible without excluding otherwise valid library data.
- History, retries, and staleness must remain deterministic and auditable.

## Considered options

### 1. Keep lens-specific hard-coded airfoils and record them after generation

Rejected. It preserves the demonstrated geometry/polar mismatch and never creates a user approval boundary.

### 2. Let users replace individual stations from a searchable library

Rejected. Independent station edits can defeat thickness progression, trim, stability, and stall behavior. It also expands the approved scope beyond a coordinated schedule gate.

### 3. Rank coordinated schedules, approve once, and propagate exact coordinates

Chosen. Exhaustive station combinations are evaluated with existing whole-wing models, Pareto-filtered, ranked under three deterministic strategies, and closed with sensitivity. The approval fixes exact root/mid/tip identities for every concept.

## Decision

Implement the contract in `docs/superpowers/plans/2026-08-19-airfoil-selection.md`.

The selector is deterministic domain code. It derives local operating conditions, gates unavailable or out-of-envelope data, evaluates coordinated triples with existing drag/stall/trim/stability/geometry logic, Pareto-filters them, and produces three distinct strategies. Balanced minimizes worst normalized regret; Efficiency is drag-first; Low-speed/control is stall/control-first.

Commit an exact coordinate archive generated from the same AeroSandbox library/version as the polar table. OpenVSP must use `XS_FILE_AIRFOIL` and `ReadFileAirfoil` for root/mid/tip. Recommendation and approval artifacts are immutable and versioned. Candidate generation explicitly requires a current approval, applies its schedule after every lens override, and propagates the selection version/hash into promotion and Stage 2.

## Predeclared proof

The implementation is proven only if all of the following are observed:

1. Coordinate and polar name sets and AeroSandbox versions are identical; every coordinate hash verifies.
2. A real OpenVSP integration test reads the built `.vsp3` and reports curve types `[12, 12, 12]` for the selected root/mid/tip, with the selected names and hashes in the geometry report.
3. Domain fixtures prove deterministic output, three distinct schedules, correct Pareto/ranking/badge behavior, leave-one-metric-out closure, and explicit 10-candidate/8-pass sensitivity.
4. Store/API tests prove immutable paths, idempotency, locking, reload, staleness, and `404`/`409`/`422` semantics.
5. Propagation tests prove exact schedule equality across baseline and every lens and carry selection identity through promotion and handoff.
6. UI tests prove modal/graphs/switching/reload/keyboard/stale/loading/error/responsive behavior and the documented navigation event without Findings DOM access.
7. The full WSL suite, environment doctor, VSPAERO smoke, and no-LLM golden are run and their exact results recorded.

Until those checks pass, the verdict remains **unproven**; the reproduction proves the defect, not the fix.

## Consequences

Positive consequences are a visible decision gate, one coordinate/polar identity through the design loop, deterministic retries, and an evidence weakness that can be reviewed independently from feasibility.

Costs are a larger committed data artifact, additional Stage 1 state/versioning, an extra approval interaction, and bounded but nontrivial whole-wing schedule evaluation. The production search/pass limits remain explicit `[D]` judgements and can be revised only with recorded sensitivity evidence.

## Isolation contract

The CFD LangGraph is unchanged. Findings DOM IDs, modules, routes, and transitions are unchanged. Shared shell edits are limited to router registration in `src/cockpit/server.py` and Airfoil screen/navigation registration in `stage1.html`/`stage1.js`, each in the separately required commit. Exact commit SHAs and the final shared interface inventory will be appended after verification.

