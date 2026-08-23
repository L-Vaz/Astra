# Astra Mechanical Engineering Portfolio — design specification

Status: approved in chat on 2026-08-23.

## Goal and audience

Create an application-ready mechanical design / CAE portfolio case study that
shows Astra as a credible engineering project rather than a software showcase.
The first-pass reader should understand the project, the author's role, and the
engineering workflow quickly; a technical interviewer should find enough
method, evidence, limitations, and traceability to ask substantive questions.

The portfolio will describe Astra accurately as an extension of the open-source
Foam-Agent CFD foundation. It will distinguish inherited infrastructure from
the BWB workflow, cockpit, geometry, aero, meshing, and verification work added
for Astra.

## Deliverables

- A screen-first, 16:9 landscape PDF suitable for attaching to applications.
- An editable PowerPoint source using the same page dimensions and visual
  system. The PDF is the application artifact; the PowerPoint is retained for
  tailoring future versions.
- A local asset folder containing only portfolio-ready crops, screenshots, and
  diagrams used by the source document.
- A short portfolio README that records the source of each visual and the
  command or repository location that can reproduce it.

The deliverables will live under `portfolio/`. Personal identity fields will be
shown as `YOUR NAME`, `EMAIL`, `LINKEDIN`, and `PROJECT LINK` until CV/contact
details are supplied; these labels are intentional and must be easy to replace.

## Narrative and page architecture

The document will contain ten landscape pages:

1. **Cover — Astra.** Cinematic BWB hero, project title, one-sentence thesis,
   discipline line, author identity, and role.
2. **Engineering challenge and ownership.** Mission/design problem, project
   bounds, and a two-column attribution block separating inherited Foam-Agent
   capabilities from Astra contributions.
3. **Reviewable design workflow.** One visual chain from mission brief through
   requirements, parametric geometry, fast aero, mesh, OpenFOAM CFD, and the
   physical-quality verdict; each stage names its artifact or gate.
4. **Requirement-driven concept design.** Representative requirement rows,
   derivation logic, constraints, trade-offs, and one documented engineering
   decision. Values are included only when their evidence status is available.
5. **Parametric geometry and cockpit.** A large real UI screenshot, annotated
   planform/geometry controls, candidate lifecycle, and the distinction between
   instant preview and solver-backed outputs.
6. **Mesh generation.** Surface mesh hero, farfield slice details, boundary
   classification, and the checks used before solver handoff.
7. **CFD analysis.** Solver-purpose summary, pressure and velocity visuals, and
   short physical interpretation tied to the displayed case rather than generic
   CFD claims.
8. **Verification and result quality.** Residual and force-history plots,
   convergence/quality reasoning, plausibility checks, and an explicit statement
   of what the available evidence does and does not prove.
9. **Engineering outcomes.** Skills demonstrated, key design decisions,
   iteration lessons, current project maturity, and the next experiment that
   would reduce the largest uncertainty.
10. **Evidence and project notes.** Toolchain, repository/project link,
    selected sources, visual provenance, attribution, and a concise limitations
    block.

Each page has one message. Cover and section-opening regions favour imagery;
technical pages balance annotated visuals with concise explanatory text. Body
copy uses short paragraphs and captions rather than dense report prose.

## Visual system

The approved direction is **technical cinematic** with balanced information
density:

- dark navy backgrounds, cyan analysis accents, amber evidence/attention
  accents, and off-white text;
- large geometry, mesh, UI, or CFD imagery as the primary visual anchor;
- compact uppercase section labels, strong page titles, restrained body copy,
  thin technical rules, evidence chips, and consistent figure captions;
- diagrams and charts rebuilt or restyled as clean vector shapes where doing so
  does not alter the underlying data;
- no decorative stock imagery, fake telemetry, invented interface states, or
  unsupported performance callouts.

The cover remains minimal. Interior pages add technical density through
annotations, method blocks, result captions, evidence labels, and limitation
notes rather than smaller text or crowded tables.

## Evidence and claim policy

Version one uses existing repository evidence; it does not require a new
end-to-end CFD run. Candidate sources include the cockpit implementation,
`reports/verification-2026-07-10/`, current decision records, and existing
verification artifacts. A local cockpit launch may be used solely to capture
the current UI against an existing stored design session; it must not be
presented as a new simulation run.

Every technical statement must be traceable to live code, a decision record, a
report, or a reproducible project artifact. Physical quantities retain Astra's
evidence grades. Measured values name the case and source artifact; assumptions
and unresolved questions remain visibly labelled. Existing plots may be
cropped, recoloured, or laid out for legibility, but their data, axes, and
meaning must not be altered.

No result from the inherited Foam-Agent paper is presented as the author's
result. No BWB visual is paired with an unrelated solver result without a clear
caption identifying the source case. A later controlled hero run may replace
selected visuals, but is outside version one.

## Production and acceptance

Production will inventory and map each visual to a page before layout. Missing
UI views will be captured from existing state; missing explanatory diagrams
will be built from presentation-native vector shapes. The source presentation
will then be exported to PDF and both formats rendered for visual review.

The portfolio is accepted when:

- all ten pages render without clipping, overlap, illegible captions, or broken
  images;
- every technical image and claim has recorded provenance;
- the authorship split between Foam-Agent and Astra is unambiguous;
- the cover is visually bold while interior pages remain readable at normal
  laptop zoom;
- geometry, mesh, CFD fields, residuals, force histories, UI, verification, and
  limitations are all represented;
- engineering claims survive a cross-check against the repository and the
  grounding protocol;
- placeholder identity fields are the only intentionally incomplete content;
- the PDF opens independently and the editable source can reproduce it.

## Explicit exclusions

- No claim of structural, aeroelastic, propulsion, battery, manufacturing, or
  flight-test validation.
- No new engineering capability or Astra workflow change.
- No fabricated result, invented coefficient, or unlabelled illustrative data.
- No full pipeline or CFD rerun for version one.
- No website in this first release; the PDF can later seed a web case study.
