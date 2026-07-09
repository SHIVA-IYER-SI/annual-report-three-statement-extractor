# Wake the Numbers — Annual Report Three-Statement Extractor

An open-source web tool built from Block 20A. It accepts **1–10 annual-report PDF or XBRL/iXBRL files**, automatically detects the companies, groups matching reports, and generates a **separate normalized historical three-statement Excel workbook for each company**.

Example: upload five TCS reports and five Tata Capital reports in one batch. The app creates one TCS workbook and one Tata Capital workbook instead of mixing both companies.

## Main features

- 1–10 files per run, up to 250 MB each.
- One company or several companies in the same upload.
- Automatic company-name detection and grouping.
- Low-confidence detections are isolated and marked for review rather than silently combined.
- Separate Excel preview and download card for every detected company.
- **Download all workbooks** as one ZIP.
- Contact / suggestion button: `shivaiyer79@gmail.com`.
- **Download your own** button linking back to this GitHub repository.
- Simple local instructions in [`RUN_LOCALLY.md`](RUN_LOCALLY.md).
- Temporary file processing only; no permanent database or cloud-file storage is required.

## Retained workbook contract

Each generated workbook preserves the locked Block 20A format:

1. `INCOME STATEMENT`
2. `BALANCE SHEET`
3. `CASH FLOW STATEMENT`

It retains historical actual columns only, dynamic disclosed rows, blank-versus-zero distinction, INR-crore normalization, source comments, existing hierarchy/style rules, freeze panes at `C3`, gridlines off, 437 canonical statement rows and 58 packaged validation checks.

The untouched source Block 20A package remains under `reference_original/` for audit/reference.

## How the hosted version works

1. **GitHub Pages (`docs/`)** hosts the always-open quirky landing page.
2. The **Wake the Numbers** button pings the Render backend until Python wakes.
3. The Render backend accepts the files, detects companies, generates workbook(s), provides side previews and deletes temporary files after the session.

## Deploy the Python backend on Render

1. Push this repository to GitHub.
2. In Render, create a Blueprint from the repository. `render.yaml` contains the deployment configuration.
3. Copy the Render URL, for example:

```text
https://annual-report-three-statement-extractor.onrender.com
```

4. Put that URL inside `docs/config.js`.
5. In GitHub repository settings, enable Pages from branch `main` and folder `/docs`.

Share the GitHub Pages URL. Visitors press **Wake the Numbers**, wait for the free backend to wake, and then use the upload interface.

## Run locally

See the beginner-friendly guide: [`RUN_LOCALLY.md`](RUN_LOCALLY.md).

Quick command:

```bash
python -m venv .venv
```

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000`.

Run tests:

```bash
pytest -q
```

## Privacy

Uploaded reports and generated workbooks use temporary server storage. They are deleted automatically after `JOB_TTL_MINUTES`—60 minutes by default—or immediately through **Delete uploaded files and outputs now**. They are never committed to GitHub.

## Extraction boundary

This is a deterministic baseline, not a guarantee that every annual-report layout will extract perfectly. Native-text PDFs and standard XBRL work best. Image-only/scanned PDFs require OCR, which is deliberately not bundled. Uncertain company names and line mappings are marked for review rather than silently forced.

## API

- `GET /api/health`
- `GET /api/blueprint`
- `POST /api/jobs/init`
- `POST /api/jobs/{job_id}/files`
- `POST /api/jobs/{job_id}/start`
- `GET /api/jobs/{job_id}`
- `GET /api/jobs/{job_id}/preview/{artifact_id}`
- `GET /api/jobs/{job_id}/download/workbook/{artifact_id}`
- `GET /api/jobs/{job_id}/download/all`
- `DELETE /api/jobs/{job_id}`

Interactive API documentation is available at `/api/docs`.
