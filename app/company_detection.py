"""Best-effort company detection and safe grouping for mixed annual-report uploads.

The extractor never combines documents merely because detection failed. Confident
company names are normalized and grouped; uncertain files receive their own group
so two unrelated companies cannot be silently merged into one workbook.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable

from .core.parsing import extract_years_from_text

LEGAL_SUFFIX_RE = re.compile(
    r"\b(?:limited|ltd\.?|private\s+limited|pvt\.?\s+ltd\.?|corporation|corp\.?|incorporated|inc\.?|plc|llp|company|co\.?)\b",
    re.I,
)
NOISE_RE = re.compile(
    r"\b(?:annual\s+report|integrated\s+report|financial\s+statements?|registered\s+office|corporate\s+office|"
    r"company\s+secretary|statutory\s+auditors?|independent\s+auditors?|board\s+of\s+directors|contents?|website|www\.|http|"
    r"consolidated|standalone|for\s+the\s+year|year\s+ended|cin\s*:|email\s*:|telephone|phone|fax)\b",
    re.I,
)
FILE_NOISE_RE = re.compile(
    r"\b(?:annual|report|integrated|financial|statements?|consolidated|standalone|results?|fy|year|final|signed|"
    r"english|full|copy|version|ar)\b",
    re.I,
)


@dataclass(frozen=True)
class DetectedDocument:
    path: Path
    company_name: str
    company_key: str
    confidence: float
    method: str
    review_required: bool
    reason: str | None = None


@dataclass
class CompanyGroup:
    artifact_id: str
    company_name: str
    company_key: str
    documents: list[DetectedDocument]
    review_required: bool


def _clean_line(value: str) -> str:
    text = unicodedata.normalize("NFKC", value or "")
    text = re.sub(r"\s+", " ", text).strip(" \t|:;,-–—•")
    text = re.sub(r"^(?:the\s+)?(?:name\s+of\s+(?:the\s+)?reporting\s+entity\s*[:\-]\s*)", "", text, flags=re.I)
    text = re.sub(r"\s+(?:annual|integrated)\s+report.*$", "", text, flags=re.I)
    return text[:180]


def normalize_company_key(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode("ascii").casefold()
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"\b(?:annual|integrated)\s+report\b.*$", " ", text)
    text = LEGAL_SUFFIX_RE.sub(" ", text)
    text = re.sub(r"\b(?:india|indian)\b", " ", text)
    text = re.sub(r"\b(?:19|20)\d{2}\b", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _filename_fallback(path: Path) -> str:
    stem = unicodedata.normalize("NFKC", path.stem)
    stem = re.sub(r"(?:19|20)\d{2}(?:\s*[-_/]\s*\d{2,4})?", " ", stem)
    stem = FILE_NOISE_RE.sub(" ", stem)
    stem = re.sub(r"[_\-.]+", " ", stem)
    stem = " ".join(stem.split()).strip()
    return stem.title() if len(stem) >= 3 else "Undetected Company"


def _candidate_score(line: str, page_number: int, line_number: int, occurrences: int) -> float:
    score = 0.0
    if LEGAL_SUFFIX_RE.search(line):
        score += 4.5
    if page_number == 1:
        score += 3.5
    elif page_number <= 3:
        score += 2.0
    elif page_number <= 10:
        score += 0.8
    if line_number <= 15:
        score += 1.4
    if 4 <= len(line) <= 90:
        score += 1.0
    if line.isupper() or line.istitle():
        score += 0.4
    score += min(2.0, max(0, occurrences - 1) * 0.45)
    if NOISE_RE.search(line):
        score -= 3.5
    if re.search(r"\b(?:llp|chartered\s+accountants?|auditor|secretary|director|registrar)\b", line, re.I):
        score -= 2.5
    if re.search(r"[@/]|\b(?:road|street|floor|building|mumbai|delhi|bangalore|bengaluru|kolkata|chennai|pune)\b", line, re.I):
        score -= 1.0
    if sum(ch.isdigit() for ch in line) > 3:
        score -= 2.0
    return score


def _pdf_candidates(path: Path, max_pages: int = 20) -> list[tuple[str, float, str]]:
    try:
        import pdfplumber
    except ImportError:
        return []
    raw: list[tuple[str, int, int]] = []
    try:
        with pdfplumber.open(path) as pdf:
            page_count = len(pdf.pages)
            indices = set(range(min(max_pages, page_count)))
            indices.update(range(max(0, page_count - 10), page_count))
            indices.update(range(0, page_count, 25))
            for page_index in sorted(indices):
                page_number = page_index + 1
                page = pdf.pages[page_index]
                text = page.extract_text(x_tolerance=2, y_tolerance=3) or ""
                for line_number, original in enumerate(text.splitlines()[:120], start=1):
                    line = _clean_line(original)
                    if not 3 < len(line) < 180:
                        continue
                    if LEGAL_SUFFIX_RE.search(line):
                        raw.append((line, page_number, line_number))
    except Exception:
        return []
    counts: dict[str, int] = {}
    for line, _, _ in raw:
        counts[normalize_company_key(line)] = counts.get(normalize_company_key(line), 0) + 1
    candidates = []
    for line, page_number, line_number in raw:
        key = normalize_company_key(line)
        if len(key) < 3:
            continue
        candidates.append((line, _candidate_score(line, page_number, line_number, counts.get(key, 1)), f"PDF page {page_number}"))
    candidates.sort(key=lambda item: (-item[1], len(item[0]), item[0].casefold()))
    return candidates


def _xbrl_candidates(path: Path) -> list[tuple[str, float, str]]:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")[:5_000_000]
    except Exception:
        return []
    candidates: list[tuple[str, float, str]] = []
    patterns = [
        r"<(?:[^>]*:)?(?:NameOfReportingEntityOrOtherMeansOfIdentification|EntityLegalName|NameOfCompany)[^>]*>(.*?)</",
        r"(?:Name of reporting entity|Entity legal name|Name of company)\s*[:\-]\s*([^<\n]{3,180})",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.I | re.S):
            value = re.sub(r"<[^>]+>", " ", match.group(1))
            value = _clean_line(value)
            if len(normalize_company_key(value)) >= 3:
                candidates.append((value, 10.0, "XBRL entity fact"))
    return candidates


def detect_document_company(path: Path) -> DetectedDocument:
    suffix = path.suffix.casefold()
    candidates = _xbrl_candidates(path) if suffix in {".xml", ".xhtml", ".html", ".htm"} else _pdf_candidates(path)
    if candidates:
        display, score, method = candidates[0]
        key = normalize_company_key(display)
        confidence = max(0.0, min(0.99, score / 10.0))
        if score >= 5.5 and key:
            return DetectedDocument(path, display, key, confidence, method, score < 7.0, None if score >= 7.0 else "LOW_COMPANY_DETECTION_CONFIDENCE")
    fallback = _filename_fallback(path)
    key = normalize_company_key(fallback)
    # A fallback is deliberately unique later, so unrelated unknown files are
    # never merged merely because both were called "annual_report.pdf".
    return DetectedDocument(
        path=path,
        company_name=fallback,
        company_key=key or "undetected",
        confidence=0.20,
        method="filename fallback",
        review_required=True,
        reason="COMPANY_NAME_NOT_CONFIDENTLY_DETECTED",
    )


def _keys_match(left: str, right: str) -> bool:
    if left == right:
        return True
    if min(len(left), len(right)) >= 8 and (left in right or right in left):
        return True
    return SequenceMatcher(None, left, right).ratio() >= 0.90


def _artifact_id(index: int, key: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", key.casefold()).strip("-")[:48] or "company"
    return f"company-{index:02d}-{slug}"


def group_documents(paths: Iterable[Path]) -> list[CompanyGroup]:
    detected = [detect_document_company(Path(path)) for path in paths]
    groups: list[CompanyGroup] = []
    for document in detected:
        target: CompanyGroup | None = None
        # Uncertain files are isolated unless the detected key is meaningful and
        # another confident group already matches it.
        if document.confidence >= 0.55:
            target = next((group for group in groups if _keys_match(document.company_key, group.company_key)), None)
        if target is None:
            groups.append(
                CompanyGroup(
                    artifact_id="",
                    company_name=document.company_name,
                    company_key=document.company_key,
                    documents=[document],
                    review_required=document.review_required,
                )
            )
        else:
            target.documents.append(document)
            target.review_required = target.review_required or document.review_required
            # Prefer the longest legal display name among matching files.
            if len(document.company_name) > len(target.company_name):
                target.company_name = document.company_name
    groups.sort(key=lambda group: (group.company_name.casefold(), group.documents[0].path.name.casefold()))
    for index, group in enumerate(groups, start=1):
        group.artifact_id = _artifact_id(index, group.company_key)
    return groups
