"""File and spreadsheet safety helpers."""
from __future__ import annotations
import hashlib, re
from pathlib import Path

ALLOWED_EXTENSIONS={".pdf",".xml",".xhtml",".html",".htm"}
ALLOWED_MIME_TYPES={"application/pdf","application/xml","text/xml","application/xhtml+xml","text/html"}
MAX_INPUT_BYTES=250*1024*1024
MAX_FILES_PER_JOB=10
MIN_FILES_PER_JOB=1
FORMULA_PREFIXES=("=","+","-","@")

def safe_filename(value:str,max_length:int=180)->str:
    name=Path(value).name
    name=re.sub(r"[^A-Za-z0-9._ -]+","_",name).strip(" .") or "document"
    stem=Path(name).stem[:max_length-12]; suffix=Path(name).suffix.lower()[:10]
    return f"{stem}{suffix}"

def validate_input_file(path:Path,claimed_mime:str|None=None)->list[str]:
    issues=[]
    if not path.is_file(): return ["FILE_NOT_FOUND"]
    size=path.stat().st_size
    if size<=0: issues.append("EMPTY_FILE")
    if size>MAX_INPUT_BYTES: issues.append("FILE_TOO_LARGE")
    if path.suffix.lower() not in ALLOWED_EXTENSIONS: issues.append("UNSUPPORTED_EXTENSION")
    if claimed_mime and claimed_mime not in ALLOWED_MIME_TYPES: issues.append("UNSUPPORTED_MIME_TYPE")
    return issues

def sha256_file(path:Path,chunk_size:int=1024*1024)->str:
    h=hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda:fh.read(chunk_size),b""): h.update(chunk)
    return h.hexdigest()

def escape_excel_text(value:str)->str:
    if value.startswith(FORMULA_PREFIXES): return "'"+value
    return value

def ensure_within(root:Path,candidate:Path)->Path:
    resolved=candidate.resolve(); base=root.resolve()
    if base not in resolved.parents and resolved!=base: raise ValueError("path escapes configured storage root")
    return resolved
