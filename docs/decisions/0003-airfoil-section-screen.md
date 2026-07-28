# 0003 — A real airfoil section, from a committed NeuralFoil screen

**Date:** 2026-07-27 · **Status:** accepted (data layer only — not yet wired into sizing)
**Commits:** uncommitted working tree at time of writing · **Decided by:** Lindon Vaz (scope), Claude (implementation)

## Problem

The aerofoil was a fiction. Three separate places said so:

- `asb_aero._symmetric_naca` snapped every station to a symmetric NACA 00xx, so no
  aircraft in this project had camber or reflex.
- `aero_estimates.CL_MAX_2D = 1.30` was ONE constant for every geometry, cited to
  "NACA 4-series section CLmax at Re ~ 7e5". Both halves are wrong for this aircraft:
  NACA 4-series are conventionally cambered — what a *tailed* aircraft uses — and
  Astra flies at Re 1.15e5–1.09e6, not 7e5.
- The `airfoil` intake row was inert, and `intake.py` documented it as such (S3-E2).

**Evidence it is real** — measured through NeuralFoil at Re 2e5 before any code changed:

```
naca4412  conventional camber    Cm0 -0.107   CLmax 1.454
eh1590    reflexed, flying wing  Cm0 -0.019   CLmax 1.038
```

29% of maximum lift separates a section a tailless aircraft can trim from one it
cannot. A single constant cannot represent both.

## Severity and blast radius

CLmax sets stall speed, stall speed sets the wing-area floor, wing area sets every
downstream number. Measured on the golden reference: the constant 1.30 lets it pass
the hand-launch gate by 1%, and **every** real tailless section fails it (see 0004
when CLmax is wired in). This is the largest single source of optimism in Stage 1.

## Physics

A cambered aerofoil carries load on its aft portion. That moves the centre of
pressure aft and produces a nose-down pitching moment, Cm0 ~ -0.10, which a
conventional aircraft cancels with a tail. A BWB has no tail, so it must find the
moment from reflex, washout, or sweep — each of which unloads the wing where it was
carrying lift. Cm0 is therefore read here as a TRIM BURDEN.

| Quantity | Value | Grade | Source or basis |
|---|---|---|---|
| Library size | 2174 sections shipped, 2171 screened, 3 rejected | `[C]` | `glob` over `aerosandbox/geometry/airfoil/airfoil_database`, asb 4.2.8 |
| Screening cost | 4.2 ms/section (vectorised 25-alpha sweep) | `[C]` | 61-section sample |
| Reynolds envelope | 1.15e5 – 1.09e6 | `[C]` | 35 generated candidates, payload 0.1–4 kg, 8–60 m/s, 0–4000 m |
| Quadratic drag polar error | RMS median 20.7–26.0%, worst 271% | `[C]` | 22 sections × 3 Re, attached branch only |
| CL-ladder error (what shipped) | median 5.04%, p90 16.52%, worst 45.82% | `[C]` | 87 sections, written into the table's own provenance |
| Cm0 separation, reflexed vs conventional | 4–7× | `[C]` | mh60 −0.019, eh1590 −0.015, s5010 −0.013 vs clarky −0.091, naca4412 −0.102 at Re 2e5 |
| `TRIM_BURDEN_BAND = 0.03` | — | `[D]` | ~25th percentile of the library. Falsified if a section inside the band still cannot be trimmed — only the S1-D trim solve can answer |
| `TRIM_RANK_RESOLUTION = 0.005` | — | `[D]` | Spread among four sections designed for the same job (mh60 −0.0035, eh1590 +0.0019, s5010 −0.0010, mh45 −0.0013). Falsified if NeuralFoil's CM error is much smaller |

Nothing here is `[A]` or `[B]`. NeuralFoil is a surrogate for XFoil, itself a panel
method — this is a computational screen, not measured data, and no wind-tunnel source
was consulted.

## Options considered

1. **Live NeuralFoil call per evaluation.** Rejected — `domain_packs` must import in
   the OpenVSP py3.10 venv where aerosandbox is absent (CLAUDE.md), and `estimate()`
   runs in fuzz loops.
2. **Fitted quadratic polar** `CD = a + b·CL + c·CL²`, the conceptual-design standard
   form. Rejected on measurement: 20–26% median error (table above). Cause is
   physical — a laminar drag bucket at Re 1e5–1e6 kinks the polar where transition
   jumps.
3. **Curated shortlist of ~20 hand-picked sections.** Rejected by the maintainer, who
   asked for "every airfoil that I can have and that suits the Reynolds number".
4. **Full-library screen committed as a table.** Chosen. 2.33 MB, ~230 s to generate.

## Chosen

`verification/generate_airfoil_table.py` screens all 2174 sections across five
Reynolds numbers into `domain_packs/bwb/data/airfoils_neuralfoil.json`.
`domain_packs/bwb/airfoils.py` reads it with stdlib only, interpolating in log(Re).

Suitability is **computed, not curated**: a `.dat` file is a name line and
coordinates, with no application metadata to curate from. Cm0 separates the families
cleanly, so the screen derives suitability and returns *every* section with reasons
attached — including unsuitable ones, so the trade stays visible.

## Four bugs found while building this, three of one root cause

All three shared a shape: **a statistic computed over the whole alpha sweep instead of
the attached branch it describes.** Recorded because the pattern will recur.

1. **`argmax(CL)` selected a post-stall point.** For a thin section at these Reynolds
   numbers CL collapses at stall then climbs again as the separated wake reorganises —
   naca64a010 at Re 4e5 peaks 0.823 at 8.25°, stalls to 0.689, recovers to 0.831 by
   19.5°. The "attached branch" therefore swallowed the whole stall, CD spanned 34×,
   and CL became multi-valued. Stored CLmax was up to **11.6% too high** (s1010 0.9217
   → 0.8151) and ladder error reached **862%**.
   *Diagnosis note:* the first hypothesis was a narrow drag bucket, tested by refining
   the ladder — 762% at 7 points, 770% at 37. That **negative result** is what proved
   the data was multi-valued rather than under-sampled.
2. **CLmax pinned at the sweep boundary** for 98 of 2171 sections, i.e. still climbing
   when data ran out. Fixed by extending the sweep to 30° *and* recording a `stalled`
   flag, so a truncation is declared rather than passed off as a stall peak. 63 of
   10855 entries (0.58%) still never stall and say so.
3. **`nanmin(confidence)` over the full sweep returned 0.000 for all 2171 sections**,
   because NeuralFoil's confidence collapses post-stall (naca64a010: 0.97 attached,
   0.13 separated). Any confidence filter rejected the entire library.
4. **Ranking on raw |Cm0| put symmetric sections above reflexed ones.** A symmetric
   section has Cm0 exactly 0.0000, so it won every tie — but it makes no lift at zero
   incidence. Measured at Re 4e5: mh60 cl0 +0.095, s5010 +0.120, naca0012 +0.000. A
   reflexed section carries the same trim burden AND keeps its camber, which is the
   entire point of reflex.

**And one default that was actively backwards.** `min_confidence=0.5` removed mh60
(0.36), eh1590 (0.31), s5010 (0.27), mh45 (0.35) and mh64 (0.29) while keeping
naca0012 (0.91) — it selected against precisely the sections a tailless BWB wants,
because confidence tracks how common a shape is in NeuralFoil's training set, not how
good it is. Now defaults to 0.0 and is surfaced as a note.

## Evidence it worked

| Check | Before | After | Command |
|---|---|---|---|
| Windows suite | 478 passed | **495 passed, 0 failed** | `python -m pytest tests/ -q -p no:randomly` |
| WSL FoamAgent suite | 683 passed | **700 passed, 4 skipped, 13 xfailed, 0 failed**, 716 collected | `/opt/conda/envs/FoamAgent/bin/python -m pytest tests/ -q -p no:randomly` |
| Collection floor | 663 | raised to **685** (690 collected on Windows) | `pyproject.toml` |
| Ladder interpolation error | worst 750% | **worst 45.82%**, median 5.04% | table provenance |
| Confidence usable | 0.000 for every section | p5 0.127, median 0.402, p95 0.965 | screen across 2171 sections |
| Top of the screen | all symmetric | `sb97_fw`, `s5010`, `s5020`, `e636`, `e328` — flying-wing series | `AF.screen(4e5, tc_range=(0.08,0.14))` |
| Cross-check vs pre-table measurement | — | naca0012 CLmax **1.126** (table) vs **1.119** (direct NeuralFoil) at Re 2e5 | independent paths agree to 0.6% |

## Still unproven

- **Nothing is wired into sizing.** `CL_MAX_2D = 1.30` is still what `cl_max_3d` uses;
  no candidate, stall speed or wing area has changed. Every number above is about the
  data layer only.
- **No experimental validation.** NeuralFoil is a surrogate for XFoil, a panel method.
  No wind-tunnel data was consulted for any section, at any Reynolds number. The
  reflexed sections most relevant here also carry the LOWEST model confidence
  (0.27–0.36), so they are the least trustworthy entries in the table.
- **`TRIM_BURDEN_BAND` and `TRIM_RANK_RESOLUTION` are both `[D]`** and unreviewed.
- **Two decisions are outstanding and block the wiring**: whether a chosen section is
  scaled to `root_thickness_pct`/`tip_thickness_pct` or overrides them, and whether the
  suitability cut stands.
- The table is 2.33 MB of generated JSON committed to git. No checksum is enforced on
  load (unlike the atmosphere table, which verifies a SHA-256) — `table_sha256()`
  exists but nothing calls it.
