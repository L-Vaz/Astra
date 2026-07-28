# 0001 — Remove endurance from Stage 1

**Date:** 2026-07-27 · **Status:** accepted
**Commits:** uncommitted working tree at time of writing · **Decided by:** Lindon Vaz (scope call)

## Problem

Stage 1 collected two mission requirements it never computed with: `endurance_min` and
`range_km`. Both were labelled honestly — the schema said *"Recorded goal only — not
evaluated (propulsion/energy out of scope)"*, the intake row said *"recorded only — aero
scope"*, and the cockpit field carried a warning note saying the same thing.

The disclosure was accurate. That is the problem: a requirement field that announces it
does nothing is still a field the user fills in, and filling it in reads as a promise
the tool does not keep.

**Evidence it is real** — the pack's own test measured it. `intake.py` carried this
comment, verified on every run by `test_bwb_disclosure.py`:

```
# The six editable rows that measurably move a derived number. The other six
# (mission_type, endurance, regulatory, structure, airfoil, wind) and every custom row
# change nothing at all — verified by perturbing each row and comparing all seven
# derived values (S3-E2/E3).
```

Endurance had also been a live failure source. From `test_stage1_api_robustness.py`:

```
* "loiter for 11 hours" parsed 200, then made EVERY later generate return 500, forever.
  GET still returned 200, so the screen showed a healthy requirements list with a dead
  Generate button and no indication which row was at fault (S2-B1).
```

That was fixed by clamping the value into `MissionSpec`'s `le=600` bound — a clamp, a
pair of range constants, a `clamp_endurance` helper, a `ROW_LIMITS` entry and a
belt-and-braces second clamp in `server.py`, all guarding a number no equation read.

## Severity and blast radius

Low severity, wide surface. No physical number was wrong. But the machinery spanned
8 files: schema field, intake row, brief-parsing pattern, a bare-number fallback, the
entire `time` unit dimension, two clamps, a `ROW_LIMITS` entry, the LLM intent prompt,
and two cockpit fields.

The `time` dimension is the clearest illustration. `endurance` was its only consumer, so
minutes and hours existed as recognised units solely to feed a row nothing read.

## Physics

**None.** This is a scope decision, not a physics change, and it is recorded here because
it changes what Stage 1 promises — not because it moves a number.

One quantity was renamed, and the distinction matters:

| Quantity | Value | Grade | Source or basis |
|---|---|---|---|
| `AeroEstimate.power_fom` (was `endurance_fom`) | 9.736490255121012 at the golden reference | `[C]` | `python verification/verify_aero_goldens.py` — value bit-identical before and after; only the JSON key changed |

`power_fom` is CL^1.5/CD. For steady level flight, power required
`P = D·V = CD·½ρSV³` with `V = √(2W/ρS·CL)`, so `P ∝ CD/CL^1.5`. Maximising CL^1.5/CD
therefore minimises the power needed to hold altitude. That is a genuine aerodynamic
figure of merit for comparing shapes, which is why it stays. It was named
`endurance_fom` because for a battery aircraft endurance = energy/power — but Stage 1
models no battery, so the name promised a time the tool cannot produce.

Nothing in this entry is `[A]` or `[B]`. Nothing needed to be: no sourced physical
quantity was added, changed, or removed.

## Options considered

1. **Keep the fields, improve the disclosure.** Rejected — the disclosure was already
   accurate and complete. There was no better wording available; the field itself was
   the problem.
2. **Keep `endurance_min`, wire it to something.** Rejected — that requires propulsion
   and energy modelling, which CLAUDE.md places outside Stage 1's scope
   ("keep the scope aerodynamics-first").
3. **Delete the fields, keep the `time` unit dimension** as generic infrastructure.
   Rejected — no remaining row is measured in time, so it would be dead code whose only
   effect is to let `"45 min"` tokenise as a quantity no row can claim.
4. **Delete the fields, keep the field name `endurance_fom`.** Rejected — it is the one
   place the word would survive a grep, and it names a shape-comparison number after a
   time.

## Chosen

Option 3's scope plus the rename: delete `endurance_min`, `range_km`, the intake row,
its brief patterns, the bare-time fallback, `ENDURANCE_MIN_MIN`/`ENDURANCE_MIN_MAX`,
`clamp_endurance`, the `ROW_LIMITS` entry, the server-side clamp, the intent-prompt
line, the two cockpit fields, the now-dead `.goalnote` CSS, and the entire `time`
dimension. Rename `endurance_fom` → `power_fom`.

`MissionSpec` deliberately does **not** gain `extra="forbid"`. Pydantic v2's default is
to ignore unknown keys, and that is what lets sessions written before this change still
load.

## Evidence it worked

Defined before implementing: (a) both suites green, (b) the golden diff is a key rename
with a bit-identical value, (c) real stored sessions carrying `endurance_min` still load.

| Check | Before | After | Command |
|---|---|---|---|
| Windows suite (domain_packs core) | 674 collected | 671 collected, **476 passed, 0 failed** | `python -m pytest tests/ -q -p no:randomly` |
| WSL FoamAgent suite (full) | — | 697 collected, **681 passed, 4 skipped, 13 xfailed, 0 failed** | `/opt/conda/envs/FoamAgent/bin/python -m pytest tests/ -q -p no:randomly` |
| Collection floor | 663 | 663, still passing with 8 slack | `minimum_collected_tests` in `pyproject.toml` |
| Golden diff | `"endurance_fom": 9.736490255121012` | `"power_fom": 9.736490255121012` — 1 line, value identical | `git diff tests/data/aero_golden.json` |
| Aero goldens | PASS | PASS, all grids unchanged | `python verification/verify_aero_goldens.py` |
| Legacy session load | 6 stored `mission_derived.json` carry `endurance_min` | **6/6 load, 0 failures**, and `endurance_min` does not survive onto the model | `MissionDerived.model_validate` over `~/astra_runs/**/mission_derived.json` |
| `cockpit.js` syntax | — | OK | `node --check src/cockpit/static/cockpit.js` |

The "Before" collection count of 674 was measured with the source edits already in place
but before the test edits — not on a pristine tree. The floor check is the load-bearing
guard and it passes either way.

Two new tests pin the deletion rather than merely removing coverage:
`test_endurance_words_create_no_row_and_leak_into_no_other` (the words are inert **and**
the orphaned number is not adopted by a neighbouring row) and
`test_endurance_is_not_a_mission_field` (the field is gone **and** a legacy spec carrying
it still validates).

## Still unproven

- ~~The cockpit Mission tab was never rendered in a browser.~~ **CLOSED during S1-B.**
  The Browser pane became available; the live cockpit at `http://localhost:8765/` renders
  the Mission tab with exactly
  `["payload_kg","cruise_speed_ms","cruise_altitude_m","mtow_kg"]`, labels
  `Payload mass / Cruise speed / Cruise altitude / MTOW (optional) / Launch method`,
  **0** `.goalnote` elements, no "Intended range" or "Intended endurance", and no console
  errors. Verified by DOM query, not screenshot — the pane does not composite frames in
  this environment, so `computer{screenshot}` remains unavailable.
- **`.goalnote` was removed from `cockpit.css` on the strength of a grep** over
  `*.{py,js,json,css,html,toml}` showing no remaining user. A reference in a file type
  outside that glob would have been missed.
- Historical plan and spec documents under `docs/superpowers/` still describe the
  endurance row. They are records of past work and were deliberately not rewritten.
