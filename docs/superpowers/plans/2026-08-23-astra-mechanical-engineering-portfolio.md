# Astra Mechanical Engineering Portfolio Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a polished ten-page technical-cinematic Astra portfolio as an editable PowerPoint and application-ready PDF for Lindon Vaz.

**Architecture:** A source-backed content and provenance layer feeds a deterministic PptxGenJS presentation builder. Existing Astra geometry, mesh, CFD, verification, and cockpit artifacts are copied or captured into a portable asset bundle; PowerPoint exports the generated deck to PDF; standalone QA scripts validate content, provenance, page count, links, and rendered output.

**Tech Stack:** PptxGenJS and Sharp from the bundled Node.js runtime; Python with Pillow, pypdf, and PyMuPDF from the bundled document runtime; local Astra cockpit; Microsoft PowerPoint PDF export.

**Spec:** `docs/superpowers/specs/2026-08-23-astra-mechanical-engineering-portfolio-design.md`

## File Map

- `portfolio/source-manifest.json` — machine-readable visual provenance and
  permitted presentation edits.
- `portfolio/content.json` — author identity and ordered page copy; no layout.
- `portfolio/claim-ledger.json` — claim-to-source mapping and evidence grade.
- `portfolio/prepare_assets.py` — lossless source-to-portable-asset copier and
  raster validation.
- `portfolio/theme.js` — colour, typography, page chrome, evidence chips, and
  source-footer helpers.
- `portfolio/build_portfolio.js` — page-specific PptxGenJS composition only.
- `portfolio/export_pdf.ps1` — reproducible PowerPoint-to-PDF conversion.
- `portfolio/render_pdf.py` — temporary page renders and QA contact sheet.
- `portfolio/test_portfolio.py` — standalone contract and artifact tests that
  do not change the repository's pytest collection floor.
- `portfolio/README.md` — build, verification, provenance, and refresh guide.
- `portfolio/assets/` — portable images referenced by the deck.
- `portfolio/astra-mechanical-engineering-portfolio.pptx` and `.pdf` — final
  editable and application-ready artifacts.

## Global Constraints

- Produce a 16:9 landscape PowerPoint and PDF with exactly ten pages.
- Use the approved technical-cinematic theme: navy `#101923`, cyan `#54D1E2`, amber `#E6B958`, off-white `#EFF7F8`.
- Author identity is `Lindon Vaz`; public links are `https://www.linkedin.com/in/lindon-vaz-` and `https://github.com/L-Vaz/Astra`.
- Omit email from version one; do not inspect Gmail or infer an address.
- Use existing repository evidence only. Do not run a new CFD case or claim a new end-to-end run.
- Launching the cockpit to capture existing state is allowed; do not call mutating UI/API actions.
- Preserve plotted data, axes, and meaning. Cropping, scaling, and colour-neutral presentation framing are allowed.
- Every technical claim and visual must resolve to a repository source or reproducible capture entry.
- Explicitly separate inherited Foam-Agent capabilities from Astra contributions.
- Do not claim structural, aeroelastic, propulsion, battery, manufacturing, or flight-test validation.
- Generated slides must contain no unfinished identity tokens, invented telemetry, unsupported performance claims, or unrelated solver imagery.

---

### Task 1: Create the portable evidence bundle and provenance contract

**Files:**
- Create: `portfolio/source-manifest.json`
- Create: `portfolio/prepare_assets.py`
- Create: `portfolio/test_portfolio.py`
- Create: `portfolio/assets/geometry/iso.png`
- Create: `portfolio/assets/geometry/top.png`
- Create: `portfolio/assets/mesh/surface.png`
- Create: `portfolio/assets/mesh/slice-y.png`
- Create: `portfolio/assets/mesh/slice-z.png`
- Create: `portfolio/assets/cfd/surface-pressure.png`
- Create: `portfolio/assets/cfd/velocity-slice.png`
- Create: `portfolio/assets/verification/residuals.png`
- Create: `portfolio/assets/verification/force-coefficients.png`

**Interfaces:**
- Consumes: existing files under `runs/design_sessions/44d2e8b0/geometry/v1/renders/` and `reports/verification-2026-07-10/img/`.
- Produces: `source-manifest.json` entries with `id`, `output`, `source`, `kind`, `case_label`, `caption`, `evidence_grade`, and `allowed_edits`; portable PNG assets under `portfolio/assets/`.

- [ ] **Step 1: Write the initial failing manifest test**

Create `portfolio/test_portfolio.py` with a `unittest.TestCase` that loads
`source-manifest.json`, asserts the required nine source IDs exist, verifies
that every `source` path exists, verifies each `output` stays under
`portfolio/assets`, and fails while the manifest is absent.

```python
REQUIRED_SOURCE_IDS = {
    "geometry_iso", "geometry_top", "mesh_surface", "mesh_slice_y",
    "mesh_slice_z", "surface_pressure", "velocity_slice",
    "residual_history", "force_history",
}
```

- [ ] **Step 2: Run the test and confirm the evidence bundle is absent**

Run:

```powershell
& 'C:\Users\asus\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' portfolio/test_portfolio.py
```

Expected: FAIL because `portfolio/source-manifest.json` does not exist.

- [ ] **Step 3: Define the exact manifest entries**

Map the following sources without changing their data:

| ID | Repository source | Portable output | Label |
|---|---|---|---|
| `geometry_iso` | `runs/design_sessions/44d2e8b0/geometry/v1/renders/iso.png` | `portfolio/assets/geometry/iso.png` | existing design session geometry |
| `geometry_top` | `runs/design_sessions/44d2e8b0/geometry/v1/renders/top.png` | `portfolio/assets/geometry/top.png` | existing design session geometry |
| `mesh_surface` | `reports/verification-2026-07-10/img/mesh_aircraft_surface.png` | `portfolio/assets/mesh/surface.png` | verification report case |
| `mesh_slice_y` | `reports/verification-2026-07-10/img/mesh_slice_y.png` | `portfolio/assets/mesh/slice-y.png` | verification report case |
| `mesh_slice_z` | `reports/verification-2026-07-10/img/mesh_slice_z.png` | `portfolio/assets/mesh/slice-z.png` | verification report case |
| `surface_pressure` | `reports/verification-2026-07-10/img/surface_pressure.png` | `portfolio/assets/cfd/surface-pressure.png` | verification report case |
| `velocity_slice` | `reports/verification-2026-07-10/img/U_slice_y.png` | `portfolio/assets/cfd/velocity-slice.png` | verification report case |
| `residual_history` | `reports/verification-2026-07-10/img/residuals.png` | `portfolio/assets/verification/residuals.png` | verification report case |
| `force_history` | `reports/verification-2026-07-10/img/force_coeffs.png` | `portfolio/assets/verification/force-coefficients.png` | verification report case |

Set `evidence_grade` to `C` for these machine-produced artifacts and make the
caption state the exact source case. Set `allowed_edits` to
`["crop", "scale", "presentation_frame"]` for plots and fields.

- [ ] **Step 4: Implement deterministic asset preparation**

`prepare_assets.py` must resolve paths relative to the repository root, create
parent directories, copy each source losslessly with `shutil.copy2`, open the
copy with Pillow to verify it is a readable raster image, and reject path
traversal or a missing source. It must not recolour, redraw, smooth, or relabel
the data.

- [ ] **Step 5: Generate the bundle and rerun the test**

Run:

```powershell
& 'C:\Users\asus\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' portfolio/prepare_assets.py
& 'C:\Users\asus\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' portfolio/test_portfolio.py
```

Expected: the prepare command lists nine copied assets; the test passes.

- [ ] **Step 6: Commit the evidence contract**

```powershell
git add portfolio/source-manifest.json portfolio/prepare_assets.py portfolio/test_portfolio.py portfolio/assets
git commit -m "feat: assemble portfolio evidence bundle"
```

---

### Task 2: Capture the real cockpit against existing design state

**Files:**
- Modify: `portfolio/source-manifest.json`
- Create: `portfolio/assets/ui/cockpit-overview.png`
- Create: `portfolio/assets/ui/cockpit-candidate-analysis.png`

**Interfaces:**
- Consumes: read-only session `runs/design_sessions/44d2e8b0`, local cockpit URL `http://127.0.0.1:8765/?session=44d2e8b0`, and the browser-control skill.
- Produces: two full-window PNG captures and manifest entries `cockpit_overview` and `cockpit_analysis` with evidence grade `C` and capture metadata.

- [ ] **Step 1: Extend the test before capturing**

Add `cockpit_overview` and `cockpit_analysis` to `REQUIRED_SOURCE_IDS`; add a
check that UI entries contain `capture_url`, `session_id`, `viewport`, and
`captured_at` fields.

- [ ] **Step 2: Run the test and verify it fails**

Run the standalone test. Expected: FAIL because the two UI entries and files
do not exist.

- [ ] **Step 3: Start the cockpit without changing session state**

Use the configured WSL FoamAgent environment from the repository root:

```bash
export ASTRA_SESSIONS_DIR=/mnt/e/AI_projects/Astra/runs/design_sessions
python -m src.cockpit.server --host 127.0.0.1 --port 8765
```

Confirm `GET http://127.0.0.1:8765/api/health` succeeds. Do not click Apply,
Generate, Screen, Promote, Optimize, Verify, or any control that issues a POST.

- [ ] **Step 4: Capture the overview**

Open `http://127.0.0.1:8765/?session=44d2e8b0` at a 1600×1000 viewport. Wait for
the 3D view, panels, and version state to finish loading. Capture the complete
viewport as `portfolio/assets/ui/cockpit-overview.png`.

- [ ] **Step 5: Capture the existing candidate-analysis state**

Use only controls that reveal existing client state. Open the candidate browser
and analysis panel without invoking a solver or promotion action. Capture the
complete viewport as `portfolio/assets/ui/cockpit-candidate-analysis.png`. If
the session has no non-mutating candidate-analysis view, capture the
requirements/derivation panel instead and use the manifest caption
`Cockpit engineering explanation view`; do not manufacture an analysis state.

- [ ] **Step 6: Record capture provenance and rerun the test**

Add both manifest entries with session `44d2e8b0`, the full capture URL,
viewport `1600x1000`, timestamp, caption, and `allowed_edits` of
`["crop", "scale", "presentation_frame"]`. Rerun `portfolio/test_portfolio.py`.

Expected: PASS; both screenshots are readable PNGs and resolve through the
manifest.

- [ ] **Step 7: Commit the UI evidence**

```powershell
git add portfolio/source-manifest.json portfolio/assets/ui portfolio/test_portfolio.py
git commit -m "feat: capture Astra cockpit for portfolio"
```

---

### Task 3: Lock the source-backed page copy and claim ledger

**Files:**
- Create: `portfolio/content.json`
- Create: `portfolio/claim-ledger.json`
- Modify: `portfolio/test_portfolio.py`

**Interfaces:**
- Consumes: the approved design spec, `README.md`, `docs/decisions/`, `reports/verification-2026-07-10/report.md`, and the source manifest.
- Produces: exactly ten ordered page records in `content.json`; claim records with `id`, `text`, `source`, `grade`, and `use_on_pages` in `claim-ledger.json`.

- [ ] **Step 1: Write content-contract tests first**

Extend `test_portfolio.py` to assert:

```python
assert len(content["pages"]) == 10
assert [p["number"] for p in content["pages"]] == list(range(1, 11))
assert content["author"]["name"] == "Lindon Vaz"
assert content["author"]["linkedin"] == "https://www.linkedin.com/in/lindon-vaz-"
assert content["author"]["github"] == "https://github.com/L-Vaz/Astra"
```

Also reject the tokens `YOUR NAME`, `EMAIL`, `LINKEDIN`, `PROJECT LINK`, and
`lorem`; require every `claim_id` used on a page to resolve in the
claim ledger; require every `asset_id` to resolve in the source manifest.

- [ ] **Step 2: Run the tests and verify the content layer is absent**

Expected: FAIL because `content.json` and `claim-ledger.json` do not exist.

- [ ] **Step 3: Create the exact ten-page narrative**

Use these page headlines and messages:

| Page | Headline | Core message |
|---|---|---|
| 1 | `ASTRA` | `A reviewable blended-wing-body design workflow — from mission intent to CFD evidence.` |
| 2 | `Turning a mission brief into an engineering problem` | Requirements, assumptions, ownership, and scope are made explicit before geometry is accepted. |
| 3 | `One traceable design loop` | Each stage creates an inspectable artifact and downstream work is invalidated when an upstream design changes. |
| 4 | `Requirements drive the concept` | The design loop separates what the user typed, what the solver used, and which constraint forced a change. |
| 5 | `Parametric geometry, reviewed in context` | The cockpit exposes design variables and trade-offs while server-side validation remains authoritative. |
| 6 | `A deterministic path to a solver-ready mesh` | Geometry, farfield, boundary classification, and mesh evidence are checked before OpenFOAM handoff. |
| 7 | `From geometry to flow-field evidence` | Pressure and velocity fields are interpreted as evidence from the named verification case, not as generic decoration. |
| 8 | `A clean solver exit is not the verdict` | Residuals, force histories, plausibility, mission checks, and known limits determine whether a result is usable. |
| 9 | `What this project demonstrates` | Parametric design, CAE workflow integration, automation, technical communication, and disciplined uncertainty management. |
| 10 | `Evidence, attribution and limits` | Astra extends Foam-Agent; sources, toolchain, current boundaries, and public project links remain visible. |

Page 2 must contain the following authorship split:

- **Inherited Foam-Agent foundation:** LangGraph CFD spine; planning, input
  writing, execution, review/correction, visualization; hierarchical RAG.
- **Astra extension:** BWB schema and intake; inverse sizing and concept
  variants; browser cockpit; versioned design sessions; OpenVSP geometry;
  VSPAERO screening; deterministic Gmsh farfield; verification gates and
  evidence discipline.

- [ ] **Step 4: Create the claim ledger from repository sources**

Every qualitative technical sentence must resolve to one of these live sources:

- project definition/scope: `README.md`;
- workflow and stage behavior: `docs/agent-guidance/bwb-design-loop.md`;
- cockpit and versioning: `docs/agent-guidance/task-maps.md` sections 11–13;
- engineering decisions:
  `docs/decisions/0001-remove-endurance-from-stage-1.md`,
  `docs/decisions/0002-close-the-stage-1-requirement-set.md`,
  `docs/decisions/0003-airfoil-section-screen.md`,
  `docs/decisions/0004-stall-station-trim-and-span-efficiency.md`, and
  `docs/decisions/0005-section-drag-locks-dihedral-and-the-stage-2-handoff.md`;
- CFD/mesh/result evidence: `reports/verification-2026-07-10/report.md` and
  its image directory;
- inherited attribution: `README.md` below the Foam-Agent divider and
  `https://github.com/csml-rpi/Foam-Agent`.

Use grade `C` for current-machine/report artifacts, preserve grades already
recorded in decision files, and use no ungraded physical quantity in page copy.

- [ ] **Step 5: Run the content tests**

Expected: PASS with ten pages, no unfinished tokens, all asset IDs resolved,
and all claims linked to ledger entries.

- [ ] **Step 6: Commit the portfolio narrative**

```powershell
git add portfolio/content.json portfolio/claim-ledger.json portfolio/test_portfolio.py
git commit -m "docs: define Astra portfolio narrative"
```

---

### Task 4: Build the editable technical-cinematic PowerPoint

**Files:**
- Create: `portfolio/theme.js`
- Create: `portfolio/build_portfolio.js`
- Create: `portfolio/astra-mechanical-engineering-portfolio.pptx`
- Modify: `portfolio/test_portfolio.py`

**Interfaces:**
- Consumes: `content.json`, `claim-ledger.json`, `source-manifest.json`, and prepared PNG assets.
- Produces: a deterministic 16:9 ten-slide PowerPoint at `portfolio/astra-mechanical-engineering-portfolio.pptx`.

- [ ] **Step 1: Add the presentation-output test**

Extend the standalone test to open the `.pptx` as a ZIP archive and assert that
`ppt/presentation.xml` exists, exactly ten `ppt/slides/slide*.xml` files exist,
the archive contains image relationships, and the strings `Lindon Vaz`,
`ASTRA`, and `Foam-Agent` occur in slide XML.

- [ ] **Step 2: Run the test and verify the deck is absent**

Expected: FAIL because the PowerPoint has not been generated.

- [ ] **Step 3: Implement the shared theme and layout helpers**

`theme.js` must export:

```javascript
const COLORS = {
  navy: "101923", cyan: "54D1E2", amber: "E6B958",
  white: "EFF7F8", muted: "AEBCC2", panel: "14222E", line: "2D4353"
};
const PAGE = { width: 13.333, height: 7.5 };
function addPageChrome(pptx, slide, number, section) {
  slide.background = { color: COLORS.navy };
  slide.addShape(pptx.ShapeType.line, {
    x: 0.34, y: 0.45, w: 0, h: 6.55,
    line: { color: COLORS.cyan, width: 1.5 }
  });
  slide.addText(section.toUpperCase(), {
    x: 0.55, y: 0.28, w: 5.2, h: 0.22,
    fontFace: "Aptos", fontSize: 7, bold: true,
    color: COLORS.cyan, charSpacing: 1.4, margin: 0
  });
  slide.addText(String(number).padStart(2, "0"), {
    x: 12.35, y: 7.05, w: 0.45, h: 0.18,
    fontFace: "Aptos", fontSize: 7, color: COLORS.muted,
    align: "right", margin: 0
  });
}
function addEvidenceChip(pptx, slide, text, tone, x, y, w) {
  const color = tone === "open" ? COLORS.amber : COLORS.cyan;
  slide.addShape(pptx.ShapeType.roundRect, {
    x, y, w, h: 0.24,
    fill: { color, transparency: 80 }, line: { color, width: 0.7 }
  });
  slide.addText(text.toUpperCase(), {
    x: x + 0.06, y: y + 0.055, w: w - 0.12, h: 0.1,
    fontFace: "Aptos", fontSize: 6.5, bold: true,
    color, margin: 0, align: "center"
  });
}
function addSourceFooter(slide, sourceText) {
  slide.addText(sourceText, {
    x: 0.55, y: 7.03, w: 10.9, h: 0.18,
    fontFace: "Aptos", fontSize: 6.5, color: COLORS.muted,
    margin: 0
  });
}
module.exports = { COLORS, PAGE, addPageChrome, addEvidenceChip, addSourceFooter };
```

Use Aptos for body copy and Aptos Display for titles. Titles must remain at
least 24 pt, body copy at least 11 pt, and captions at least 9 pt. Use native
PowerPoint shapes for workflow arrows, callouts, evidence chips, and the
attribution table.

- [ ] **Step 4: Implement the ten page layouts**

`build_portfolio.js` must read the three JSON inputs, set `LAYOUT_WIDE`, embed
document metadata, and generate these compositions:

1. Full-bleed dark cover with geometry hero, cyan glow frame, thesis, author,
   discipline, LinkedIn, and GitHub.
2. Left: challenge and scope. Right: inherited-versus-Astra contribution
   matrix. Bottom: limitations strip.
3. Six-stage horizontal workflow with artifact/gate captions and a versioning
   side note.
4. Requirement rows plus a derivation chain and one evidence-backed design
   decision card.
5. Large cockpit overview, two annotated callouts, and a narrow strip showing
   the second UI capture.
6. Surface mesh hero with two farfield-slice details and boundary/gate labels.
7. Pressure field and velocity slice side by side with a source-case caption
   and restrained interpretation.
8. Residual and force-history charts with verdict logic, evidence chips, and a
   visible `what this proves / what this does not prove` block.
9. Four capability cards, three engineering lessons, and next-experiment note.
10. Attribution, toolchain, evidence sources, limitations, QR/link area,
    LinkedIn, and GitHub.

- [ ] **Step 5: Add deterministic build diagnostics**

Before writing the deck, verify all referenced assets exist and all page asset
IDs/claim IDs resolve. During layout, track every element rectangle and throw
when an element exceeds the 13.333×7.5 page bounds. Print a page-by-page asset
and claim summary after generation.

- [ ] **Step 6: Generate the PowerPoint**

Run:

```powershell
$env:NODE_PATH='C:\Users\asus\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\node_modules'
& 'C:\Users\asus\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe' portfolio/build_portfolio.js
```

Expected: ten page summaries, no missing assets or out-of-bounds elements, and
the `.pptx` file is written.

- [ ] **Step 7: Run the artifact test and commit**

Expected: PASS, including the ten-slide and embedded-image checks.

```powershell
git add portfolio/theme.js portfolio/build_portfolio.js portfolio/astra-mechanical-engineering-portfolio.pptx portfolio/test_portfolio.py
git commit -m "feat: build Astra engineering portfolio deck"
```

---

### Task 5: Export, render, inspect, and deliver the PDF

**Files:**
- Create: `portfolio/export_pdf.ps1`
- Create: `portfolio/render_pdf.py`
- Create: `portfolio/README.md`
- Create: `portfolio/astra-mechanical-engineering-portfolio.pdf`
- Modify: `portfolio/test_portfolio.py`

**Interfaces:**
- Consumes: the generated PowerPoint and Microsoft PowerPoint at `C:\Program Files\Microsoft Office\root\Office16\POWERPNT.EXE`.
- Produces: application-ready PDF, temporary page renders under the OS temp directory, final build instructions, and a complete QA result.

- [ ] **Step 1: Add PDF acceptance tests**

Extend `test_portfolio.py` to use `pypdf.PdfReader` and assert:

- the PDF exists and has exactly ten pages;
- every page uses the same landscape media box;
- extracted text contains `Lindon Vaz`, `ASTRA`, `Foam-Agent`, the LinkedIn URL,
  and the GitHub URL;
- extracted text contains none of the forbidden unfinished tokens;
- the PDF file is non-empty and independently readable.

- [ ] **Step 2: Run the test and verify the PDF is absent**

Expected: FAIL because the PDF has not been exported.

- [ ] **Step 3: Implement reproducible PDF export**

`export_pdf.ps1` must resolve absolute input/output paths, start PowerPoint via
COM with the window hidden, open the deck read-only, call `SaveAs` with PDF
format constant `32`, close the presentation, quit PowerPoint, release COM objects in a
`finally` block, and fail if the output is missing.

Run:

```powershell
powershell -ExecutionPolicy Bypass -File portfolio/export_pdf.ps1
```

- [ ] **Step 4: Implement page rendering for visual QA**

`render_pdf.py` must use PyMuPDF to render all PDF pages at 1600-pixel width
into a new directory under `$env:TEMP`, then create a labelled contact sheet
with Pillow and print both paths. It must not write generated QA renders into
the repository.

- [ ] **Step 5: Perform visual inspection and iterate**

Render the PDF, inspect the contact sheet and each questionable page at full
resolution, and correct the source deck until all pages satisfy:

- no clipping, overlap, stretched imagery, broken characters, or unreadable
  captions;
- consistent page rails, margins, title hierarchy, source footers, and figure
  framing;
- cover remains visually bold; technical pages remain readable;
- charts retain axes and legends; screenshots remain recognisable as actual UI;
- attribution and limitations are immediately visible where relevant.

Regenerate the PPTX and PDF after every correction; do not patch the PDF.

- [ ] **Step 6: Write the portfolio README**

Document the final deliverables, exact prepare/build/export/test commands,
identity links, evidence policy, source-manifest role, the existing-session UI
capture, the no-new-CFD-run limitation, and the procedure for replacing
selected visuals after a later hero run.

- [ ] **Step 7: Run the complete verification sequence**

```powershell
& 'C:\Users\asus\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' portfolio/prepare_assets.py
$env:NODE_PATH='C:\Users\asus\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\node_modules'
& 'C:\Users\asus\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe' portfolio/build_portfolio.js
powershell -ExecutionPolicy Bypass -File portfolio/export_pdf.ps1
& 'C:\Users\asus\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' portfolio/test_portfolio.py
& 'C:\Users\asus\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' portfolio/render_pdf.py
git diff --check
```

Expected: all commands succeed, standalone tests pass, the contact sheet shows
ten legible pages, and `git diff --check` reports no whitespace errors.

- [ ] **Step 8: Commit the application-ready package**

```powershell
git add portfolio/export_pdf.ps1 portfolio/render_pdf.py portfolio/README.md portfolio/astra-mechanical-engineering-portfolio.pdf portfolio/astra-mechanical-engineering-portfolio.pptx portfolio/build_portfolio.js portfolio/theme.js portfolio/content.json portfolio/claim-ledger.json portfolio/source-manifest.json portfolio/prepare_assets.py portfolio/test_portfolio.py portfolio/assets
git commit -m "feat: deliver Astra mechanical engineering portfolio"
```

- [ ] **Step 9: Final handoff**

Provide clickable links to the PDF, editable PowerPoint, and README. State that
email was intentionally omitted, identify the existing evidence sources used,
and list any remaining claim or visual that could only be improved by a future
controlled hero run.
