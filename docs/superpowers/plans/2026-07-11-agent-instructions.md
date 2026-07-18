# Agent Instructions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the oversized `CLAUDE.md` with a concise project entry point, preserve specialized details in conditional guidance documents, and mirror the approved result to `AGENTS.md` only after user review.

**Architecture:** `CLAUDE.md` is the always-loaded index containing only project identity, high-priority invariants, daily commands, architecture pointers, and critical caveats. Four focused documents under `docs/agent-guidance/` provide progressive disclosure by task type. `AGENTS.md` remains unchanged during the first review checkpoint.

**Tech Stack:** Markdown, PowerShell verification, Git byte comparison.

## Global Constraints

- `CLAUDE.md` must contain at most 150 lines.
- The final `CLAUDE.md` and `AGENTS.md` must be byte-for-byte identical.
- Do not change `AGENTS.md` until the user approves the revised `CLAUDE.md`.
- Preserve specialized project knowledge in linked documents rather than deleting it.
- Do not add manual code-style instructions.

---

### Task 1: Create progressive-disclosure guidance

**Files:**
- Create: `docs/agent-guidance/bwb-design-loop.md`
- Create: `docs/agent-guidance/cfd-core.md`
- Create: `docs/agent-guidance/environment-and-numerics.md`
- Create: `docs/agent-guidance/roadmap.md`

**Interfaces:**
- Consumes: detailed sections in the current `AGENTS.md`/`CLAUDE.md`
- Produces: stable Markdown links used by the concise `CLAUDE.md`

- [ ] **Step 1:** Move the interaction model, BWB loop, evaluator, and verification status into `bwb-design-loop.md`.
- [ ] **Step 2:** Move inherited graph, agent, RAG, layout, and corrected-fact details into `cfd-core.md`.
- [ ] **Step 3:** Move environment setup and hard-won meshing/numerics lessons into `environment-and-numerics.md`.
- [ ] **Step 4:** Move phase status and future scope into `roadmap.md`.
- [ ] **Step 5:** Verify all documents exist with `Get-Item docs/agent-guidance/*.md`; expect four files.

### Task 2: Replace `CLAUDE.md` with the concise entry point

**Files:**
- Modify: `CLAUDE.md`
- Do not modify: `AGENTS.md`

**Interfaces:**
- Consumes: the four documents from Task 1
- Produces: an always-loaded instruction file no longer than 150 lines

- [ ] **Step 1:** Put the one-line project description and trust order first.
- [ ] **Step 2:** Add only project-wide invariants, confirmed daily commands, a compact path map, and critical caveats.
- [ ] **Step 3:** Add conditional links stating when each detailed document should be read.
- [ ] **Step 4:** Add the maintenance rule that the final two instruction files must remain byte-for-byte identical.
- [ ] **Step 5:** Run `(Get-Content CLAUDE.md).Count`; expect a value no greater than 150.
- [ ] **Step 6:** Run `git diff -- AGENTS.md`; expect no output.
- [ ] **Step 7:** Present `CLAUDE.md` for user review without copying it to `AGENTS.md`.

### Task 3: Mirror after approval

**Files:**
- Modify: `AGENTS.md`

**Interfaces:**
- Consumes: user-approved `CLAUDE.md`
- Produces: byte-for-byte identical agent instruction files

- [ ] **Step 1:** Copy the approved bytes from `CLAUDE.md` to `AGENTS.md`.
- [ ] **Step 2:** Run `git diff --no-index -- CLAUDE.md AGENTS.md`; expect no output and exit code 0.
- [ ] **Step 3:** Compare `Get-FileHash CLAUDE.md,AGENTS.md`; expect identical SHA-256 hashes.
- [ ] **Step 4:** Recount both files; expect each to contain at most 150 lines.
