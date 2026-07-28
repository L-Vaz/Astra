# 0002 — Close the Stage 1 requirement set

**Date:** 2026-07-27 · **Status:** accepted
**Commits:** uncommitted working tree at time of writing · **Decided by:** Lindon Vaz (scope call)

## Problem

Stage 1 let the user invent requirements. Any unrecognised edit id became a "custom"
vehicle row, offered in the UI as:

```html
<button id="btn-add" class="ghost-add">+ Add constraint — max chord, materials, anything</button>
```

Nothing recognised those ids. The row's own info text admitted it:

```
Custom requirement — recorded on this list only. It is not evaluated at this tier and
does not constrain the solver.
```

`intake.py` already carried the note that *"The old text promised this row was 'passed
to Stage 2 as a constraint when recognised'. No custom row id is recognised anywhere, so
the promise was empty (S3-E3)."*

**Evidence it is real** — measured before changing anything. Adding a custom row to a
default intake and re-deriving:

```
custom row added: True
row count 18 -> 19
derived values identical: True
differences: NONE
```

A button labelled "Add constraint … anything" that constrains nothing.

## Severity and blast radius

No physical number was wrong — this is a scope and honesty defect, not a physics one.
Surface: the `IntakeRow.custom` flag, `MAX_ROWS`, the unknown-id branch of `apply_edits`,
the `_restore_default` drop path, the `addRow()` widget and its button, `.ghost-add` CSS,
and a "Custom requirement" fallback string in `stage1.js`.

Secondary consequence: unbounded row growth. Because every unknown id minted a row,
`MAX_ROWS = 64` existed purely to stop a client writing a 519-row intake (S1-A3).

## Physics

**None.** No physical quantity was added, changed, or removed. Recorded because it
changes what Stage 1 accepts from the user, which is an architectural choice.

## Options considered

1. **Build a real constraint vocabulary** — recognise `max_chord`, `min_tip_chord`,
   `max_area`, etc. and apply them as solver bounds. This was the original S8-D plan.
   Rejected by the maintainer: *"lets make it such that we cant add custom rows … for now
   we control what you can add and change in the conceptual design space."*
2. **Keep custom rows, improve the disclaimer.** Rejected — the disclaimer was already
   accurate and complete. The row was the problem, not the wording.
3. **Silently ignore unknown ids.** Rejected — a silently dropped edit is the exact
   failure mode this pack has spent several stages removing.
4. **Refuse unknown ids by name.** Chosen.

## Chosen

`apply_edits` refuses an unknown id with a `ValueError` naming the id and the closed set;
the cockpit turns that into HTTP 422 **before any write**, so a refused edit leaves no
version behind. `MAX_ROWS`, `IntakeRow.custom`, `addRow()`, the button, `.ghost-add` and
the fallback string are all removed.

`MAX_ROWS` was deleted rather than kept as belt-and-braces: with unknown ids refused, the
row list cannot grow at all, so the cap is unreachable. The attack is closed at its
source rather than bounded downstream.

**One defect introduced and caught in the process, worth recording.** `known_row_ids()`
was first built from `default_intake()`, which returns only the 11 input rows —
`derive()` appends 7 more (`wing_loading`, `s_ref`, `cruise_speed`, `cruise_cl`, `clmax`,
`static_margin`, `ld_est`). That made `apply_edits` refuse a legitimate edit to
`cruise_speed`. Then a second, subtler version keyed the refusal on `current.rows`
rather than the declared set, which refused a declared row that `derive()` had not yet
appended. `test_a_used_value_differing_from_the_typed_one_always_implies_a_warning`
caught both. The final logic has three branches: present → edit; declared but absent →
no-op; otherwise → refuse.

## Evidence it worked

Defined before implementing: (a) a custom row moves no derived value — measured first,
to establish removal is safe; (b) both suites green; (c) the refusal is observable in the
running app, not just in unit tests.

| Check | Before | After | Command |
|---|---|---|---|
| Custom row moves a derived value | added a row, **0** values changed | row cannot be created | `apply_edits` + `derive`, diffed all derived numbers |
| Windows suite | 478 passed | **478 passed, 0 failed** | `python -m pytest tests/ -q -p no:randomly` |
| WSL FoamAgent suite | 683 passed | **683 passed, 4 skipped, 13 xfailed, 0 failed**, 699 collected | `/opt/conda/envs/FoamAgent/bin/python -m pytest tests/ -q -p no:randomly` |
| Collection floor | 663 | 663, passing with 36 slack | `minimum_collected_tests` in `pyproject.toml` |
| Unknown id via live API | created a row, 200 | **422**, body names `custom_max_chord` **and** "closed set" | `fetch('/api/stage1/intake', …)` in the running cockpit |
| Valid edit via live API | 200 | **200**, `payload.number == 1.75` | same session, immediately after the refusal |
| Stage 1 screen renders | — | `btn-add` absent, **0** `.ghost-add`, page fully initialised, no console errors | DOM query at `http://localhost:8765/stage1` |
| JS parses | — | OK | `node --check src/cockpit/static/stage1.js` |

The live-API check matters more than the unit tests here: it proves the refusal reaches
the user as a 422 with a useful message rather than a 500, and that removing
`$('btn-add').addEventListener(...)` did not break page initialisation — a null-reference
there would have killed the whole screen silently.

## Still unproven

- **No screenshot.** The Browser pane does not composite frames in this environment, so
  `computer{screenshot}` is unavailable and all UI evidence above is DOM-level. A layout
  regression from the removed button (e.g. spacing under `#tier-vehicle`) would not have
  been caught.
- **A stale browser tab now gets a 422 where it used to get a silent no-op.** If a client
  is holding a row id that was removed — `endurance`, after decision 0001 — its edit is
  refused by name rather than ignored. That is intended, but it is a behaviour change for
  any open session and has not been exercised against a real stale client.
- **Legacy intakes carrying a custom row were not tested against real data**, because
  there is none: a search of `~/astra_runs` found **0** stored intake files and 0 rows
  flagged `custom`. The `_restore_default` drop path is covered by a synthetic test only.
- The five inert rows (`mission_type`, `regulatory`, `structure`, `airfoil`, `wind`) are
  still present and still move no number. `airfoil` is scheduled to become real in S1-C.
  The other four are an open question — see below.
