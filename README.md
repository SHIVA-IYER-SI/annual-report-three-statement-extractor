# Annual Report Three-Statement Extractor

A standalone website built from Block 20A. A user uploads between 1 and 10 annual-report PDF or XBRL/iXBRL files for the same company, the app extracts and normalizes historical financial statements, generates the locked Excel workbook, offers a **Download Excel** action, and opens a structured workbook preview in a right-side drawer.

## What is retained

- Exact workbook sheet names and order:
  1. `INCOME STATEMENT`
  2. `BALANCE SHEET`
  3. `CASH FLOW STATEMENT`
- Historical actual columns only; no forecast columns.
- Dynamic rows: a line appears only when disclosed or supported by a disclosed formula.
- Blank-versus-zero distinction.
- INR-crore normalization.
- Original Block 20A styling: Arial, actual header fill, row hierarchy, subtotal/major-total borders, gridlines off, freeze panes at `C3`, source comments, and the existing number formats.
- 437 canonical statement rows from the packaged blueprint resources: 159 Income Statement, 193 Balance Sheet and 85 Cash Flow rows.
- 58 packaged validation checks: 16 Income Statement, 27 Balance Sheet and 15 Cash Flow checks.
- Multi-file upload for 1–10 annual reports/XBRL files, each up to 250 MB. Files are uploaded sequentially to avoid one oversized combined request.
- Progress state, validation checklist, artifact card, Download Excel action, side preview drawer, unmapped-line review and temporary-file deletion.

The untouched source Block 20A package is retained under `reference_original/` for audit/reference. The deployed application uses the standalone files under `app/`.

## Privacy and file storage

Uploaded annual reports and generated files are stored only in temporary server storage. A job accepts 1–10 files for one company and combines up to the latest 10 detected historical years. They are deleted automatically after `JOB_TTL_MINUTES` (60 minutes by default), or immediately when the user clicks **Delete uploaded files and outputs now**. They are not committed to GitHub.

Render's filesystem is ephemeral, which is appropriate for this temporary workflow. This app does not require a database or permanent cloud storage.

## Local run

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

## GitHub + Render deployment

GitHub stores the repository; Render runs the Python backend. GitHub Pages cannot run this app because it only serves static files.

1. Create a new empty GitHub repository.
2. Upload the contents of this folder to the repository root. Do not upload the outer ZIP as a single file.
3. Commit and push.
4. In Render, choose **New → Blueprint** and connect the GitHub repository.
5. Render detects `render.yaml`, builds the Docker image, and creates the web service.
6. When deployment finishes, open the Render URL and share it.

No environment secrets are required. Optional settings:

- `JOB_TTL_MINUTES=60`
- `JOB_WORKERS=2`
- `APP_TEMP_ROOT=/tmp/annual_report_three_statement_extractor`

## Important extraction boundary

This is a functional deterministic baseline, not a guarantee that every annual-report layout will extract perfectly. Native text PDFs and standard XBRL work best. Image-only/scanned PDFs require OCR, which is deliberately not bundled into this lightweight deployment. When a label cannot be mapped confidently, it is excluded rather than forced into the workbook and is displayed under **Unmapped source rows**. A workbook may therefore be marked **Review required** while still remaining downloadable and previewable.

## API

- `GET /api/health`
- `GET /api/blueprint`
- `POST /api/jobs/init`
- `POST /api/jobs/{job_id}/files` (one file per request)
- `POST /api/jobs/{job_id}/start`
- `POST /api/jobs` (backward-compatible all-files endpoint)
- `GET /api/jobs/{job_id}`
- `GET /api/jobs/{job_id}/preview`
- `GET /api/jobs/{job_id}/download/workbook`
- `GET /api/jobs/{job_id}/download/extracted`
- `GET /api/jobs/{job_id}/download/validation`
- `GET /api/jobs/{job_id}/download/audit`
- `DELETE /api/jobs/{job_id}`

Interactive API documentation is at `/api/docs`.
