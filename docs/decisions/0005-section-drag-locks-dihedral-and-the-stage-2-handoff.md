# 0005 — Two drag models that disagree, locks that finally bite, dihedral from a recovered source, and a handoff that grades itself

**Date:** 2026-07-28 · **Status:** accepted
**Decided by:** Lindon Vaz (the S1-H source was supplied by the maintainer mid-session,
which unblocked an item the agent had deferred)

Covers S1-E, S1-F, S1-H and S1-I — the remainder of the Stage-1 plan.
[0004](0004-stall-station-trim-and-span-efficiency.md) is the prerequisite: `spanwise.py`
and the Schrenk loading underpin both S1-E and the handoff's evidence block.

## Problem

**S1-E — CD0 cannot see the aerofoil.** `parasite_drag_coeff` is flat-plate Cf × Hoerner
form factor × wetted-area fit × a lumped 1.10 interference margin. The only way a
section enters is through its thickness ratio, so two aerofoils of equal t/c and
different drag are the same aircraft to it. Since S1-C the pack has carried measured CD
for 2,171 sections and was not using it.

**S1-F — locks were decoration, and the code said so.** The intake's own field
description recorded it: *"nothing reads it: the solver produces byte-identical derived
values with every row 'fixed' and with every row 'variable'"*. An xfail (S3-E1) held the
proof.

```
free  : (('wing_loading', 7.064140843), ('s_ref', 0.471394905), ...)
fixed : (('wing_loading', 7.064140843), ('s_ref', 0.471394905), ...)   # identical
```

**S1-H — effective dihedral was not computed at all.** A swept wing supplies dihedral
effect whether or not anyone models it, and the amount is not small. The item was
**deferred earlier in this session** rather than guessed: its algebra was attributed to
Kuhlman (RCSD 2000), which could not be obtained, and NACA RM L8A28a — pulled and
OCR-extracted — defines `Cl_β` as the effective-dihedral parameter but carries no sweep
equation.

**S1-I — nothing carried Stage 1's conclusions, or their evidence, forward.** A CLmax
computed from measured section data at the right Reynolds and an Oswald efficiency the
project has explicitly declined to trust are both floats on the same object.

## Severity and blast radius

S1-E reaches every L/D on every card, because `cd0` is half the polar. S1-F is a
correctness-of-promise defect: the UI advertised a constraint it did not apply, which is
worse than not offering it. S1-H is reporting-only but the numbers span **−5.88° to
+49.91°** across the schema, including sign flips. S1-I is the interface to everything
Stage 2 will do.

## Physics

**Profile drag by strip theory.** Each spanwise strip behaves as a 2D aerofoil at its
own local lift coefficient and chord Reynolds number:

```
CD_profile(CL) = (2/S) ∫ cd( cl(y), Re(y) ) c(y) dy ,   cl(y) = CL·cl_add(y) + cl_basic(y)
```

This is **not** CD0. A section's CD rises with its own lift coefficient — thicker
boundary layer, more pressure drag — so the integral at cruise CL already contains the
viscous part of the lift-dependent drag. That is exactly the term the classical polar
`CD0 + CL²/(πeAR)` has no home for, and exactly why an empirical Oswald e lands at
0.75–0.85 while the same wing's planar inviscid span efficiency is 0.98: **Oswald e is
carrying viscous drag under an inviscid name.**

**Effective dihedral from sweep.** Sideslip makes the windward panel present a smaller
effective sweep to the flow, so it sees more velocity normal to its quarter-chord line
and lifts more; the leeward panel lifts less. The difference rolls the aircraft away
from the sideslip — which is what dihedral does. Because it acts on lift the panels are
already making, it scales with CL: strong when slow, weak when fast.

```
Cl_β_sweep = −[(1 + 2λ)/(3(1 + λ))] · CL · tan(Λ_c/2)
```

| Quantity | Value | Grade | Source or basis |
|---|---|---|---|
| Sweep contribution to `Cl_β` | equation above | **`[B]`** | McCormick, *Aerodynamics, Aeronautics and Flight Mechanics*, Wiley 1979, **p. 547**, via Kuhlman, *RC Soaring Digest* Jan–Mar 2000, PDF pp. 15–18, read this session |
| `Cl_β = −0.00021 ≈ 1° of positive dihedral` | −0.00021 /deg | **`[B]`** | Kuhlman, same article, PDF p. 18. The author's own word is "roughly" |
| Reference effective dihedral, cruise → CLmax | 5.08° → 8.95° | **`[C]`** | `dihedral.effective_dihedral(reference, CL)` |
| Envelope span | −5.88° … +49.91° | **`[C]`** | corner scan over the legal schema |
| Reference sweeps LE / c4 / **c2** | 23.27 / 16.67 / **9.58°** | **`[C]`** | `effective_*_sweep_deg(reference)` |
| Strip profile CD, cruise / zero lift | 0.01032 / 0.00990 | **`[C]`** | `section_drag.profile_drag(reference, cl, Re)` |
| Raymer CD0, with / without its 1.10 margin | 0.01360 / 0.01237 | **`[C]`** | `parasite_drag_coeff` |
| Section-swap sensitivity, tip mh60 → e474 | +8.3% | **`[C]`** | same, two geometries |
| CD ladder's own interpolation error | 5.04% median, 16.52% p90 | **`[C]`** | table provenance, 87 probes |
| `LOCK_TOL` 1 mm span / 1 cm² area | — | **`[D]`** | Finer than either can be built to, coarser than the solver converges. Moves only whether a `CapViolation` is raised |
| `LOCKABLE_ROW_IDS` = span/area/MTOW | — | **`[D]`** | Each pins a number the sizing solve would otherwise choose. `cruise_speed` and `ld_est` excluded as outcomes |
| The 1.10 interference margin itself | 1.10 | **`[D]`** | Unallocated. Does not explain the 1.37 gap — inherited, not introduced here |

## Options considered

**S1-E — swap the polar, as the plan intended?** No. Measured, substituting the section
buildup with the planar induced term takes the reference wing's cruise L/D from **15.93
to 20.16, +27%** — a model change wearing the clothes of a better aircraft. Choosing a
reconciling factor to close a gap of 1.37 would be fitting a judgment constant to the
answer. Same call as S1-G in 0004, for the same reason, and the consistency is
deliberate.

**S1-E — could the gap just be the interference margin?** Checked: no. Raymer without
its 1.10 still reads **1.250×** the section sum.

**S1-F — repair locks inside `_feasible_planform`?** Rejected. Those repairs exist to
satisfy schema gates and are free to move area and span for their own reasons. Applying
a lock as a request and then **verifying it against the delivered geometry** is the only
check a future change to the repair machinery cannot bypass.

**S1-F — let the L/D loop run under a lock?** No. It works by trading area against
aspect ratio, which is precisely what a lock removes; running it anyway would break the
lock to chase a target. It is skipped, with a note saying so.

**S1-H — implement from memory?** Refused earlier in the session and routed as a
research task. The maintainer supplied the source, which is the mechanism working.

## Chosen

`section_drag.py` — strip-integrated profile drag, reported beside `cd0` with the ratio
and the off-ladder span fraction as first-class fields; **polar unchanged**.

`inverse.LockedValues` + `intake.to_locked` — three lockable rows, applied then
verified, `CapViolation` on anything unhonourable, L/D loop skipped under a geometry
lock. A locked MTOW additionally stops the assumed value following the payload row,
which is the one thing the intake changes behind the operator's back.

`dihedral.py` + `effective_c2_sweep_deg` — the half-chord sweep line the equation is
written at, which the repo did not have.

`handoff.py` + `GET /api/handoff/{session}` — versioned artifact with an evidence row
per exported quantity, the locks and their violations, unmet requirements, three
`known_disagreements`, and provenance pinning the airfoil table SHA-256.

## Evidence it worked

| Check | Before | After | Command |
|---|---|---|---|
| Drag responds to the aerofoil (tip mh60→e474) | 0 (t/c only) | **+8.3%** | `pytest tests/test_bwb_section_drag.py` |
| Two drag models, ratio | not comparable | **1.375** (1.250 without the 1.10 margin) | same |
| Off-ladder span reported at CL 0 / 1.05 | hidden | **66.6% / 94.4%** | same |
| `profile_drag` runtime | 7.85 ms | **3.74 ms** | hoisted bilinear surface |
| Locking every row changes the outcome | byte-identical | **W/S 7.064 → 3.519**, S 0.4714 → 0.9378 | `pytest tests/test_bwb_locks.py` |
| Span lock 1.2/1.5/2.0 m honoured | n/a | **< 1 mm** | same |
| Span lock 2.5 m | silently ignored | **CapViolation at 2.1795 m**, blocked by the AR gate | same |
| Area lock 0.30 m² vs a 0.4750 m² stall need | n/a | lock held, requirement named unmet | same |
| Unit reconciliation, Kuhlman Graph 3/4 corner | — | **26.4° / 28.8°** under a 30° axis (per-degree gives 1512°) | `pytest tests/test_bwb_dihedral.py` |
| Disputed "3° sweep = 1° dihedral" rule | — | exact at **CL 0.50 / 0.46** | same |
| Reference effective dihedral | not computed | **5.08° cruise → 8.95° CLmax** | same |
| Handoff evidence rows | none | **11 graded**, `cd0`/`oswald_e`/`x_np_m` all `[D]` | `pytest tests/test_bwb_handoff.py` |
| Handoff live over HTTP | 500 → 500 → | **200, 10,591 bytes** | `GET /api/handoff/44d2e8b0` |
| xfail backlog | 11 | **10** (S3-E1 retired) | `pytest -rx` |
| Windows suite | 731 collected | **794 collected, 599 passed, 0 failed** | `pytest tests/ -q -p no:randomly` |
| WSL suite | 757 collected | **820 collected, 806 passed, 0 failed** | same, FoamAgent env |
| JS parity (4 scripts) | — | all pass | `node tests/js/*.mjs` |

**Two defects the live check caught that the green suite did not**, both in the handoff
route: `check_requirements` was called with the panel instead of the geometry
(`AttributeError` at request time), and an unknown session id raised `FileNotFoundError`
→ 500 instead of 404. Both now have tests in
`tests/test_stage1_api_robustness.py`. A route with no test is a route that has never
run.

## Still unproven

- **The 1.37 drag disagreement is unresolved and the polar keeps the weaker model.**
  `cd0` is graded `[D]` in the handoff for exactly this reason. Settling it needs a CFD
  or tunnel point — tier 3 exists for this — not another correlation.
- **The zero-lift drag comparison is 66.6% clamped.** At CL = 0 the washed-out tips
  carry negative local lift and the stored ladder starts at 0.0. The clamp is defensible
  (the floor sits near the bottom of a roughly symmetric low-Re drag bucket) but two
  thirds of that particular number is a boundary value. Reported as a field.
- **`Cl_β = −0.00021` is the author's "roughly".** It sets the accuracy of every
  effective-dihedral number in the pack, and is not independently confirmed against
  McCormick directly — only against a NACA-era statement of −0.0002 quoted in a search
  result, which is corroboration, not verification.
- **Taper ratio for the dihedral equation is the tip/root ratio of a two-panel wing**,
  where McCormick's derivation assumes linear taper. The factor spans only 0.333–0.500
  across all possible taper, which bounds the error, but it is a modelling choice.
- **The handoff has no consumer.** Every field is a guess about what Stage 2 will want.
  `SCHEMA_VERSION` exists so the first real consumer can say what it needed instead.
- **Locks are not yet exposed in the cockpit UI.** The solver honours them and the API
  route reads the persisted flags, but nothing on screen sets them per row.
- **S1-H is reported, not optimised**, by design — `dihedral_deg` comes out exactly as
  the user set it.
