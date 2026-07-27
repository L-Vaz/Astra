# NNNN — <short title>

**Date:** YYYY-MM-DD · **Status:** accepted | superseded by NNNN | reverted
**Commits:** `<sha>` … · **Decided by:** <name> (physics calls are the maintainer's)

## Problem

What is broken. Written **before** implementing anything.

**Evidence it is real** — one of: a failing test, a measured wrong output with the
correct value alongside, or a specific reproduced behaviour. Not "looks fragile", not
"could break", not "best practice says".

```
<paste the failing output, the wrong number, or the reproduction>
```

## Severity and blast radius

What it costs if left alone. How much depends on it — one function, or every drag
number this project reports.

## Physics

The first-principles reasoning: what is physically happening, why this quantity exists,
where the relationship comes from. Then every number, graded:

| Quantity | Value | Grade | Source or basis |
|---|---|---|---|
| | | `[A]`/`[B]`/`[C]`/`[D]` | equation/page/command — or, for `[D]`, the sensitivity and what would falsify it |

If nothing here is `[A]` or `[B]`, say so plainly. An entry built entirely on judgment
is allowed; one that hides it is not.

## Options considered

What else was on the table, and why each was rejected. If nothing else was considered,
write that — it is a fact about the decision's quality.

## Chosen

What was done, and why this over the alternatives.

## Evidence it worked

**Defined before implementing.** The specific measurement that would confirm it.

| Check | Before | After | Command |
|---|---|---|---|

If there is no measurement, the status is **Unproven** and this section says so. Never
substitute a claim of quality for a measurement.

## Still unproven

What remains untested, unsourced, or assumed. Anything still `[D]` and unreviewed.
Any decision taken here that should have been the maintainer's.
