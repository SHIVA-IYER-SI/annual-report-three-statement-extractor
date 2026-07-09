"""FastAPI application for the standalone annual-report extractor."""
from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .core.resources import line_item_universes, validation_universes, verify_runtime_resources
from .core.security import MAX_FILES_PER_JOB, MAX_INPUT_BYTES, MIN_FILES_PER_JOB, safe_filename, validate_input_file
from .job_manager import JobManager

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
manager = JobManager()

app = FastAPI(
    title="Annual Report Three-Statement Extractor",
    version="2.0.0",
    docs_url="/api/docs",
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict:
    errors = verify_runtime_resources()
    return {"status": "ok" if not errors else "error", "resource_errors": errors}


@app.get("/api/blueprint")
def blueprint() -> dict:
    universes = line_item_universes()
    checks = validation_universes()
    name_map = {
        "INCOME_STATEMENT": "INCOME STATEMENT",
        "BALANCE_SHEET": "BALANCE SHEET",
        "CASH_FLOW_STATEMENT": "CASH FLOW STATEMENT",
    }
    return {
        "line_item_counts": {name_map[key]: len(value) for key, value in universes.items()},
        "validation_checks": {
            name_map[key]: [
                {
                    "id": item.get("check_id"),
                    "name": item.get("check_name"),
                    "severity": item.get("severity"),
                    "test": item.get("formula_or_test"),
                }
                for item in value
            ]
            for key, value in checks.items()
        },
    }


async def _store_upload(job_id: str, upload: UploadFile) -> None:
    filename = safe_filename(upload.filename or "annual_report")
    suffix = Path(filename).suffix.lower()
    if suffix not in {".pdf", ".xml", ".xhtml", ".html", ".htm"}:
        raise HTTPException(422, f"Unsupported file: {filename}")
    temp = Path(tempfile.mkstemp(prefix="annual_report_upload_", suffix=suffix)[1])
    total = 0
    try:
        with temp.open("wb") as handle:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_INPUT_BYTES:
                    raise HTTPException(413, f"{filename} exceeds 250 MB")
                handle.write(chunk)
        issues = validate_input_file(temp, upload.content_type)
        issues = [issue for issue in issues if not (issue == "UNSUPPORTED_MIME_TYPE" and suffix != ".pdf")]
        if issues:
            raise HTTPException(422, f"{filename}: {', '.join(issues)}")
        manager.add_file(job_id, filename, temp, total, upload.content_type)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    finally:
        temp.unlink(missing_ok=True)


def _validated_job_metadata(scope: str, file_count: int) -> str:
    clean_scope = scope.upper()
    if clean_scope not in {"CONSOLIDATED", "STANDALONE"}:
        raise HTTPException(422, "Invalid accounting scope")
    if not MIN_FILES_PER_JOB <= file_count <= MAX_FILES_PER_JOB:
        raise HTTPException(422, f"Upload between {MIN_FILES_PER_JOB} and {MAX_FILES_PER_JOB} files")
    return clean_scope


@app.post("/api/jobs/init")
def initialize_job(
    scope: str = Form("CONSOLIDATED"),
    fallback_unit: str = Form("crore"),
    file_count: int = Form(...),
    batch_label: str = Form(""),
    # Backward-compatible field: older frontends may still send company_name.
    company_name: str = Form(""),
) -> dict:
    clean_scope = _validated_job_metadata(scope, file_count)
    try:
        state = manager.create_job(
            scope=clean_scope,
            fallback_unit=fallback_unit,
            expected_file_count=file_count,
            batch_label=batch_label or company_name,
        )
        return manager.public_state(state["id"])
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/jobs/{job_id}/files")
async def upload_job_file(job_id: str, file: UploadFile = File(...)) -> dict:
    try:
        manager.require(job_id)
    except KeyError as exc:
        raise HTTPException(404, "Job not found or already deleted") from exc
    await _store_upload(job_id, file)
    return manager.public_state(job_id)


@app.post("/api/jobs/{job_id}/start")
def start_job(job_id: str) -> dict:
    try:
        manager.start(job_id)
        return manager.public_state(job_id)
    except KeyError as exc:
        raise HTTPException(404, "Job not found or already deleted") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/jobs")
async def create_job(
    scope: str = Form("CONSOLIDATED"),
    fallback_unit: str = Form("crore"),
    batch_label: str = Form(""),
    company_name: str = Form(""),
    files: list[UploadFile] = File(...),
) -> dict:
    file_count = len(files)
    clean_scope = _validated_job_metadata(scope, file_count)
    state = manager.create_job(
        scope=clean_scope,
        fallback_unit=fallback_unit,
        expected_file_count=file_count,
        batch_label=batch_label or company_name,
    )
    job_id = state["id"]
    try:
        for upload in files:
            await _store_upload(job_id, upload)
        manager.start(job_id)
        return manager.public_state(job_id)
    except HTTPException:
        manager.delete(job_id)
        raise
    except Exception as exc:
        manager.delete(job_id)
        raise HTTPException(500, str(exc)) from exc


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> dict:
    try:
        return manager.public_state(job_id)
    except KeyError as exc:
        raise HTTPException(404, "Job not found or already deleted") from exc


@app.get("/api/jobs/{job_id}/preview/{artifact_id}")
def preview(job_id: str, artifact_id: str) -> dict:
    try:
        return manager.preview(job_id, artifact_id)
    except KeyError as exc:
        raise HTTPException(404, "Artifact not found") from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/jobs/{job_id}/download/all")
def download_all(job_id: str) -> FileResponse:
    try:
        path, filename, mime = manager.output_path(job_id, "all")
    except KeyError as exc:
        raise HTTPException(404, "ZIP output not found") from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not path.exists():
        raise HTTPException(404, "Output file is missing")
    return FileResponse(path, media_type=mime, filename=filename)


@app.get("/api/jobs/{job_id}/download/{kind}/{artifact_id}")
def download(job_id: str, kind: str, artifact_id: str) -> FileResponse:
    try:
        path, filename, mime = manager.output_path(job_id, kind, artifact_id)
    except KeyError as exc:
        raise HTTPException(404, "Output not found") from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not path.exists():
        raise HTTPException(404, "Output file is missing")
    return FileResponse(path, media_type=mime, filename=filename)


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str) -> dict:
    manager.delete(job_id)
    return {"deleted": True}


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
