"""Temporary job storage, mixed-company grouping and background execution."""
from __future__ import annotations

import gc
import json
import os
import shutil
import threading
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .company_detection import CompanyGroup, group_documents
from .core.security import MAX_FILES_PER_JOB, MIN_FILES_PER_JOB, safe_filename
from .standalone_engine import ExtractionResult, extract_to_workbook, failure_payload


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JobManager:
    def __init__(self) -> None:
        self.root = Path(os.getenv("APP_TEMP_ROOT", "/tmp/annual_report_three_statement_extractor")).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.ttl_minutes = max(10, int(os.getenv("JOB_TTL_MINUTES", "60")))
        # One extraction at a time is intentionally safer on free 512 MB hosts.
        self.max_workers = max(1, int(os.getenv("JOB_WORKERS", "1")))
        self.executor = ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="extractor")
        self.lock = threading.RLock()
        self.jobs: dict[str, dict[str, Any]] = {}
        self._load_existing()

    def _load_existing(self) -> None:
        for directory in self.root.iterdir():
            if not directory.is_dir():
                continue
            state_path = directory / "state.json"
            if not state_path.exists():
                continue
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                if datetime.fromisoformat(state["expires_at"]) <= utcnow():
                    shutil.rmtree(directory, ignore_errors=True)
                    continue
                if state.get("status") in {"PROCESSING", "QUEUED"}:
                    state["status"] = "FAILED"
                    state["error"] = {"message": "The server restarted while this job was processing. Please upload the files again."}
                    self._write_state(state)
                self.jobs[state["id"]] = state
            except Exception:
                shutil.rmtree(directory, ignore_errors=True)

    def _job_dir(self, job_id: str) -> Path:
        return self.root / job_id

    def _write_state(self, state: dict[str, Any]) -> None:
        path = self._job_dir(state["id"]) / "state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    def create_job(self, *, scope: str, fallback_unit: str, expected_file_count: int = 1, batch_label: str = "") -> dict[str, Any]:
        if not MIN_FILES_PER_JOB <= expected_file_count <= MAX_FILES_PER_JOB:
            raise ValueError(f"File count must be between {MIN_FILES_PER_JOB} and {MAX_FILES_PER_JOB}")
        job_id = uuid.uuid4().hex
        created = utcnow()
        state = {
            "id": job_id,
            "batch_label": batch_label.strip(),
            "scope": scope.upper(),
            "fallback_unit": fallback_unit,
            "status": "UPLOADING",
            "progress": 5,
            "message": "Receiving annual-report files",
            "created_at": created.isoformat(),
            "updated_at": created.isoformat(),
            "expires_at": (created + timedelta(minutes=self.ttl_minutes)).isoformat(),
            "files": [],
            "expected_file_count": expected_file_count,
            "max_files": MAX_FILES_PER_JOB,
            "result": None,
            "error": None,
        }
        with self.lock:
            self.jobs[job_id] = state
            self._job_dir(job_id).mkdir(parents=True, exist_ok=True)
            (self._job_dir(job_id) / "inputs").mkdir(exist_ok=True)
            (self._job_dir(job_id) / "outputs").mkdir(exist_ok=True)
            self._write_state(state)
        return state.copy()

    def add_file(self, job_id: str, original_name: str, data_path: Path, size: int, content_type: str | None) -> Path:
        state = self.require(job_id)
        if state["status"] != "UPLOADING":
            raise ValueError("Files can only be added while the job is uploading")
        if len(state["files"]) >= MAX_FILES_PER_JOB:
            raise ValueError(f"A job can contain at most {MAX_FILES_PER_JOB} files")
        safe = safe_filename(original_name)
        destination = self._job_dir(job_id) / "inputs" / safe
        counter = 2
        while destination.exists():
            destination = self._job_dir(job_id) / "inputs" / f"{Path(safe).stem}_{counter}{Path(safe).suffix}"
            counter += 1
        shutil.move(str(data_path), destination)
        with self.lock:
            state["files"].append({"name": destination.name, "bytes": size, "content_type": content_type})
            uploaded = len(state["files"])
            expected = max(uploaded, int(state.get("expected_file_count") or uploaded))
            state["progress"] = min(40, 5 + int(35 * uploaded / expected))
            state["message"] = f"Uploaded {uploaded} of {expected} source file(s)"
            state["updated_at"] = utcnow().isoformat()
            self._write_state(state)
        return destination

    def start(self, job_id: str) -> None:
        state = self.require(job_id)
        file_count = len(state["files"])
        expected = int(state.get("expected_file_count") or file_count)
        if not MIN_FILES_PER_JOB <= file_count <= MAX_FILES_PER_JOB:
            raise ValueError(f"File count must be between {MIN_FILES_PER_JOB} and {MAX_FILES_PER_JOB}")
        if file_count != expected:
            raise ValueError(f"Expected {expected} file(s), but only {file_count} were uploaded")
        with self.lock:
            state["status"] = "QUEUED"
            state["progress"] = 45
            state["message"] = "Queued for company detection and extraction"
            state["updated_at"] = utcnow().isoformat()
            self._write_state(state)
        self.executor.submit(self._run, job_id)

    def _run(self, job_id: str) -> None:
        state = self.require(job_id)
        try:
            with self.lock:
                state["status"] = "PROCESSING"
                state["progress"] = 52
                state["message"] = "Detecting companies and grouping source files"
                state["updated_at"] = utcnow().isoformat()
                self._write_state(state)

            input_paths = [self._job_dir(job_id) / "inputs" / item["name"] for item in state["files"]]
            groups = group_documents(input_paths)
            with self.lock:
                state["progress"] = 55
                state["message"] = f"Detected {len(groups)} company group(s); processing reports one at a time"
                state["updated_at"] = utcnow().isoformat()
                self._write_state(state)
            artifacts: list[dict[str, Any]] = []
            successful_paths: list[tuple[Path, str]] = []
            total_groups = max(1, len(groups))

            for index, group in enumerate(groups, start=1):
                with self.lock:
                    state["progress"] = 52 + int(43 * (index - 1) / total_groups)
                    state["message"] = f"Extracting {group.company_name} ({index} of {total_groups})"
                    state["updated_at"] = utcnow().isoformat()
                    self._write_state(state)
                group_dir = self._job_dir(job_id) / "outputs" / group.artifact_id
                group_dir.mkdir(parents=True, exist_ok=True)
                try:
                    group_file_count = max(1, len(group.documents))

                    def progress_callback(stage: str, file_index: int, file_total: int, detail: str) -> None:
                        stage_weights = {
                            "EXTRACTING": 0.05,
                            "PAGE": 0.30,
                            "MAPPING": 0.72,
                            "ASSEMBLING": 0.84,
                            "VALIDATING": 0.91,
                            "BUILDING_WORKBOOK": 0.96,
                        }
                        group_base = 55 + 40 * (index - 1) / total_groups
                        group_span = 40 / total_groups
                        file_fraction = max(0.0, min(1.0, (file_index - 1) / max(1, file_total)))
                        stage_fraction = stage_weights.get(stage, 0.50)
                        if stage in {"EXTRACTING", "PAGE", "MAPPING"}:
                            fraction = file_fraction + stage_fraction / max(1, file_total)
                        else:
                            fraction = stage_fraction
                        progress = min(94, int(group_base + group_span * fraction))
                        labels = {
                            "EXTRACTING": "Opening",
                            "PAGE": "Reading statement pages",
                            "MAPPING": "Normalizing",
                            "ASSEMBLING": "Combining historical years",
                            "VALIDATING": "Running validation checks",
                            "BUILDING_WORKBOOK": "Building Excel workbook",
                        }
                        message = f"{labels.get(stage, 'Processing')} — {detail}"
                        with self.lock:
                            state["progress"] = max(int(state.get("progress") or 0), progress)
                            state["message"] = message
                            state["updated_at"] = utcnow().isoformat()
                            self._write_state(state)

                    inspection_hints = {str(document.path): document.extraction_hint() for document in group.documents}
                    result = extract_to_workbook(
                        input_paths=[document.path for document in group.documents],
                        output_dir=group_dir,
                        company_name=group.company_name,
                        scope=state["scope"],
                        fallback_unit=state["fallback_unit"],
                        inspection_hints=inspection_hints,
                        progress_callback=progress_callback,
                    )
                    summary = self._result_summary(result, group)
                    artifacts.append(summary)
                    successful_paths.append((result.workbook_path, result.workbook_filename))
                    gc.collect()
                except Exception as exc:
                    artifacts.append(
                        {
                            "artifact_id": group.artifact_id,
                            "company_name": group.company_name,
                            "status": "FAILED",
                            "review_required": True,
                            "source_files": [document.path.name for document in group.documents],
                            "detection": self._detection_summary(group),
                            "error": failure_payload(exc),
                        }
                    )

            zip_filename = None
            if successful_paths:
                zip_filename = "Annual_Report_Extracted_Workbooks.zip"
                zip_path = self._job_dir(job_id) / "outputs" / zip_filename
                with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                    for path, filename in successful_paths:
                        archive.write(path, arcname=filename)

            failed_count = sum(1 for item in artifacts if item["status"] == "FAILED")
            review_count = sum(1 for item in artifacts if item.get("review_required"))
            success_count = len(artifacts) - failed_count
            result_payload = {
                "company_count": len(groups),
                "artifact_count": len(artifacts),
                "success_count": success_count,
                "failed_count": failed_count,
                "review_count": review_count,
                "zip_filename": zip_filename,
                "artifacts": artifacts,
            }
            with self.lock:
                if success_count == 0:
                    state["status"] = "FAILED"
                    state["message"] = "No workbook could be generated"
                    state["error"] = {"message": "All detected company groups failed. Open each result for details."}
                elif failed_count or review_count:
                    state["status"] = "REVIEW_REQUIRED"
                    state["message"] = f"Generated {success_count} workbook(s); review the company grouping and warnings"
                else:
                    state["status"] = "READY"
                    state["message"] = f"Generated {success_count} company workbook(s)"
                state["progress"] = 100
                state["updated_at"] = utcnow().isoformat()
                state["result"] = result_payload
                self._write_state(state)
        except Exception as exc:
            with self.lock:
                state["status"] = "FAILED"
                state["progress"] = 100
                state["message"] = "Extraction failed"
                state["updated_at"] = utcnow().isoformat()
                state["error"] = failure_payload(exc)
                self._write_state(state)

    @staticmethod
    def _detection_summary(group: CompanyGroup) -> list[dict[str, Any]]:
        return [
            {
                "filename": document.path.name,
                "detected_company": document.company_name,
                "confidence": round(document.confidence, 3),
                "method": document.method,
                "review_required": document.review_required,
                "reason": document.reason,
                "page_count": document.page_count,
                "candidate_page_count": len(document.candidate_pages),
                "document_year": document.document_year,
            }
            for document in group.documents
        ]

    def _result_summary(self, result: ExtractionResult, group: CompanyGroup) -> dict[str, Any]:
        return {
            "artifact_id": group.artifact_id,
            "company_name": result.company_name,
            "scope": result.scope,
            "status": "REVIEW_REQUIRED" if (result.review_required or group.review_required) else "READY",
            "years": result.years,
            "review_required": result.review_required or group.review_required,
            "workbook_filename": result.workbook_filename,
            "workbook_bytes": result.workbook_path.stat().st_size,
            "source_files": [document.path.name for document in group.documents],
            "detection": self._detection_summary(group),
            "checklist": result.checklist,
            "statistics": result.statistics,
            "warnings": result.warnings,
            "unmapped_preview": result.unmapped[:100],
            "unmapped_count": len(result.unmapped),
            "restatements": result.restatements[:100],
            "validation_summary": {
                "critical_count": result.validation["critical_count"],
                "error_count": result.validation["error_count"],
                "warning_count": result.validation["warning_count"],
                "passed_count": result.validation["passed_count"],
            },
        }

    def require(self, job_id: str) -> dict[str, Any]:
        self.cleanup_expired()
        with self.lock:
            state = self.jobs.get(job_id)
            if not state:
                raise KeyError(job_id)
            return state

    def public_state(self, job_id: str) -> dict[str, Any]:
        state = self.require(job_id)
        return json.loads(json.dumps(state))

    def _artifact(self, state: dict[str, Any], artifact_id: str) -> dict[str, Any]:
        artifacts = (state.get("result") or {}).get("artifacts") or []
        artifact = next((item for item in artifacts if item.get("artifact_id") == artifact_id), None)
        if not artifact:
            raise KeyError(artifact_id)
        if artifact.get("status") == "FAILED":
            raise RuntimeError("This company group did not produce a workbook")
        return artifact

    def preview(self, job_id: str, artifact_id: str) -> dict[str, Any]:
        state = self.require(job_id)
        if state["status"] not in {"READY", "REVIEW_REQUIRED"}:
            raise RuntimeError("Preview is not ready")
        artifact = self._artifact(state, artifact_id)
        path = self._job_dir(job_id) / "outputs" / artifact_id / "extracted_data.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        preview = data["preview"]
        preview["validation_summary"] = artifact["validation_summary"]
        preview["artifact"] = {
            "artifact_id": artifact_id,
            "filename": artifact["workbook_filename"],
            "review_required": artifact["review_required"],
            "bytes": artifact["workbook_bytes"],
        }
        return preview

    def output_path(self, job_id: str, kind: str, artifact_id: str | None = None) -> tuple[Path, str, str]:
        state = self.require(job_id)
        if state["status"] not in {"READY", "REVIEW_REQUIRED"}:
            raise RuntimeError("Output is not ready")
        outputs = self._job_dir(job_id) / "outputs"
        if kind == "all":
            filename = (state.get("result") or {}).get("zip_filename")
            if not filename:
                raise KeyError(kind)
            return outputs / filename, filename, "application/zip"
        if not artifact_id:
            raise KeyError("artifact_id")
        artifact = self._artifact(state, artifact_id)
        artifact_dir = outputs / artifact_id
        if kind == "workbook":
            filename = artifact["workbook_filename"]
            return artifact_dir / filename, filename, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        mapping = {
            "extracted": ("extracted_data.json", "application/json"),
            "validation": ("validation_report.json", "application/json"),
            "audit": ("audit_summary.json", "application/json"),
        }
        if kind not in mapping:
            raise KeyError(kind)
        filename, mime = mapping[kind]
        return artifact_dir / filename, filename, mime

    def delete(self, job_id: str) -> None:
        with self.lock:
            self.jobs.pop(job_id, None)
        shutil.rmtree(self._job_dir(job_id), ignore_errors=True)

    def cleanup_expired(self) -> None:
        now = utcnow()
        expired: list[str] = []
        with self.lock:
            for job_id, state in list(self.jobs.items()):
                try:
                    if datetime.fromisoformat(state["expires_at"]) <= now:
                        expired.append(job_id)
                except Exception:
                    expired.append(job_id)
            for job_id in expired:
                self.jobs.pop(job_id, None)
        for job_id in expired:
            shutil.rmtree(self._job_dir(job_id), ignore_errors=True)
