# Astra mechanical engineering portfolio

This folder contains a concise, application-ready case study of Astra for mechanical design, CAE and simulation roles.

## Deliverables

- `astra-mechanical-engineering-portfolio.pdf` - the primary file to attach to applications.
- `astra-mechanical-engineering-portfolio.pptx` - the editable source deck.
- `source-manifest.json` - provenance for every image used in the portfolio.
- `claim-ledger.json` - evidence grade and source for each substantive engineering claim.
- `content.json` - the page-level narrative and content model.

## Attribution and limits

Astra transparently extends the open-source Foam-Agent CFD foundation. The portfolio separates the inherited CFD spine from the Astra-authored BWB requirement, geometry, candidate, cockpit, meshing and quality-gate work.

The displayed engineering quantities and simulation images are evidence grade `[C]`: measured in the repository's recorded verification case and traceable through the source manifest. The deck does not claim structural, propulsion, manufacturing or flight-test validation. The mesh-refinement proposal is labelled `[D]` because it is a next experiment, not a completed result.

No new CFD run was performed to create the portfolio. Existing recorded artifacts and a read-only cockpit session were used.

## Re-export the PDF

From PowerShell in this directory:

```powershell
./export_pdf.ps1
```

The export requires Microsoft PowerPoint. To render the resulting PDF for visual QA:

```powershell
python ./render_pdf.py ./astra-mechanical-engineering-portfolio.pdf ../tmp/portfolio-pdf-render
```

The public links shown are:

- LinkedIn: <https://www.linkedin.com/in/lindon-vaz->
- GitHub: <https://github.com/L-Vaz/Astra>

Email is intentionally omitted until an exact public address is supplied.
