# Start Here

This folder is already structured as the root of a GitHub repository.

## What to upload to GitHub

Upload **all files and folders inside this directory**, including:

- `app/`
- `.github/`
- `Dockerfile`
- `render.yaml`
- `requirements.txt`
- `README.md`
- the remaining root files

Do not upload only the outer ZIP and expect it to run.

## What the website does

1. Accepts 1–10 PDF, XML, XHTML or HTML annual-report files for the same company, up to 250 MB per file.
2. Processes them in temporary server storage.
3. Extracts and maps the three financial statements.
4. Generates the exact three-sheet historical Excel format.
5. Shows the validation checklist and unmapped labels.
6. Provides **Open / Preview** in a right-side drawer.
7. Provides **Download Excel**.
8. Deletes the source files and output automatically after the temporary session window.

## Deployment route

Use the included `render.yaml` and `Dockerfile` to deploy the GitHub repository as a Render web service. The repository needs no database and no API key.

After the repository is uploaded, follow `README.md` under **GitHub + Render deployment**.
