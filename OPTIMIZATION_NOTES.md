# Optimization Notes

This revision was prepared after the first free-Render test exceeded memory.

## What changed

- PDFs are inspected once with `pypdf` to detect the company and locate likely financial-statement pages.
- `pdfplumber` table extraction runs only on those likely pages instead of every page.
- Reports are processed sequentially, one file at a time.
- Raw rows are mapped immediately; only compact selected candidates continue to the next file.
- PDF page caches and temporary row objects are released during processing.
- One backend worker remains enforced for small-host safety.
- The upload button and inputs remain locked until the job reaches success or failure.
- Progress messages now show file/page extraction, mapping, validation and workbook generation stages.
- The quirky **Wake the Numbers** screen is now included on the Render app itself as well as GitHub Pages.
- The app heading, source-code link and single contact card were revised.

## Verification performed

- `13` automated tests passed.
- JavaScript syntax validation passed.
- Python compilation checks passed.
- A synthetic five-PDF sequential extraction completed successfully.

The synthetic test does not guarantee that ten unusually large or image-heavy 250 MB PDFs will fit within a free host's limits. Local execution remains the reliable fallback for extreme batches.
