# Run This Tool on Your Own Computer

You do not need Render to use the extractor privately. The uploaded annual reports stay on your computer while the app is running locally.

## Windows — simple steps

1. Install **Python 3.11 or newer**.
2. Download this GitHub repository as a ZIP and extract it.
3. Open the extracted folder in **VS Code**.
4. In VS Code, open **Terminal → New Terminal**.
5. Run these commands one by one:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload
```

6. Open this address in your browser:

```text
http://127.0.0.1:8000
```

7. Upload 1–10 annual reports and download the generated Excel workbook(s).

## Stop the app

Return to the VS Code terminal and press:

```text
Ctrl + C
```

## Start it again later

Open the same folder in VS Code and run:

```powershell
.\.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload
```

## Important

- Native-text PDFs and standard XBRL/iXBRL work best.
- Scanned image-only PDFs need OCR, which is not included in this lightweight version.
- Always review the company grouping and validation warnings before using the numbers.
