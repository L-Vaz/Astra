# Decision records

One entry per substantive decision: anything touching a physical number, an algorithm's
correctness, or an architectural choice. Refactors, tests, formatting, UI wiring and
tooling do not get an entry.

**Why this exists.** Two failures it is built to prevent:

1. Numbers that cannot be told apart — solid from guessed, all stated with the same
   confidence.
2. Work that cannot be defended — changes that may have solved nothing, indistinguishable
   after the fact from real progress.

**An honest entry is also the interview answer** to "walk me through a decision you made
and why." There is no separate portfolio document, and there should not be one.

## Naming

`NNNN-short-slug.md`, zero-padded, sequential. `0001-launch-stall-caps.md`.

Never renumber. A superseded decision is not deleted — it gets `Status: superseded by
NNNN`, and the new entry says what changed and why. The trail of a reversed decision is
worth more than a clean-looking log.

## Evidence grades

Every engineering number in an entry carries one:

| Grade | Meaning |
|---|---|
| `[A]` | Primary standard / official doc / peer-reviewed report, cited to a specific equation, table or section |
| `[B]` | Established textbook method, cited to edition + page |
| `[C]` | Measured on this machine, with the reproducing command recorded |
| `[D]` | Judgment — **not a fact**. Requires a sensitivity and what would falsify it |

`"Raymer says"` with no page is `[D]`. `"Standard practice"` is `[D]`.

## The template

Copy `TEMPLATE.md`. Every heading is required. **"None" and "I don't know" are valid
answers and must be written rather than omitted** — a missing section reads as a clean
one, which is the exact failure this log exists to stop.

## The two rules that make it worth keeping

**Write the Problem and Evidence sections before implementing, not after.** Deciding
what would prove the fix worked *after* seeing the result is how a bar gets moved
without anyone noticing.

**No entry may claim success without a checkable measurement.** "This is more robust" is
not evidence. "`test_bwb_inverse.py::test_stall_cap` failed before at 10.52 m/s against
a 10.5 cap and passes now" is. If there is no measurement, the honest status is
`Unproven`, and that is an acceptable thing for an entry to say.
