"""Low-memory company detection and statement-page inspection.

Each PDF is text-scanned once with pypdf. The resulting inspection is reused by
financial-table extraction, avoiding a second full-document scan with
pdfplumber. Uncertain company detections remain isolated so unrelated reports
are never silently combined.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable

from .core.parsing import detect_currency, extract_years_from_text

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

STATEMENT_HEADING_PATTERNS = {
    "INCOME_STATEMENT": (
        r"statement\s+of\s+profit\s+and\s+loss",
        r"profit\s+and\s+loss\s+account",
        r"income\s+statement",
        r"statement\s+of\s+income",
    ),
    "BALANCE_SHEET": (
        r"balance\s+sheet",
        r"statement\s+of\s+financial\s+position",
    ),
    "CASH_FLOW_STATEMENT": (
        r"cash\s+flow\s+statement",
        r"statement\s+of\s+cash\s+flows",
    ),
}


@dataclass(frozen=True)
class DetectedDocument:
    path: Path
    company_name: str
    company_key: str
    confidence: float
    method: str
    review_required: bool
    reason: str | None = None
    page_count: int = 0
    candidate_pages: tuple[int, ...] = field(default_factory=tuple)
    document_year: int | None = None
    currency_detected: str | None = None
    scanned_pages: int = 0

    def extraction_hint(self) -> dict[str, object]:
        return {
            "page_count": self.page_count,
            "candidate_pages": list(self.candidate_pages),
            "document_year": self.document_year,
            "currency_detected": self.currency_detected,
            "company_detected": self.company_name,
            "inspection_method": self.method,
            "scanned_pages": self.scanned_pages,
        }


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


def _statement_types(text: str) -> set[str]:
    lowered = text.casefold()
    return {
        statement
        for statement, patterns in STATEMENT_HEADING_PATTERNS.items()
        if any(re.search(pattern, lowered, re.I) for pattern in patterns)
    }


def _inspect_pdf(path: Path) -> tuple[list[tuple[str, float, str]], dict[str, object]]:
    try:
        from pypdf import PdfReader
    except ImportError:
        return [], {"page_count": 0, "candidate_pages": [], "scanned_pages": 0}

    raw_candidates: list[tuple[str, int, int]] = []
    candidate_pages: set[int] = set()
    all_years: set[int] = set()
    currency: str | None = None
    page_count = 0
    scanned_pages = 0

    try:
        reader = PdfReader(str(path), strict=False)
        page_count = len(reader.pages)
        for page_index, page in enumerate(reader.pages):
            page_number = page_index + 1
            try:
                text = page.extract_text() or ""
            except Exception:
                text = ""
            scanned_pages += 1

            if page_number <= 30:
                all_years.update(extract_years_from_text(text))
                currency = currency or detect_currency(text)
            if page_number <= 20 or page_number > max(20, page_count - 5):
                for line_number, original in enumerate(text.splitlines()[:120], start=1):
                    line = _clean_line(original)
                    if 3 < len(line) < 180 and LEGAL_SUFFIX_RE.search(line):
                        raw_candidates.append((line, page_number, line_number))

            statement_types = _statement_types(text)
            if statement_types:
                # Financial statements normally continue for a few pages after
                # the heading. Reusing this list avoids full-document table scans.
                for offset in range(0, 7):
                    if page_number + offset <= page_count:
                        candidate_pages.add(page_number + offset)

            years_on_page = extract_years_from_text(text)
            looks_tabular = bool(
                years_on_page
                and re.search(r"\bparticulars?\b", text, re.I)
                and re.search(r"\b(?:assets?|liabilit|revenue|income|expenses?|cash\s+flow|profit|loss)\b", text, re.I)
            )
            if looks_tabular:
                candidate_pages.add(page_number)

            # Release page content before the next iteration.
            del text
    except Exception:
        return [], {"page_count": page_count, "candidate_pages": [], "scanned_pages": scanned_pages}

    counts: dict[str, int] = {}
    for line, _, _ in raw_candidates:
        key = normalize_company_key(line)
        counts[key] = counts.get(key, 0) + 1
    candidates: list[tuple[str, float, str]] = []
    for line, page_number, line_number in raw_candidates:
        key = normalize_company_key(line)
        if len(key) < 3:
            continue
        candidates.append((line, _candidate_score(line, page_number, line_number, counts.get(key, 1)), f"PDF page {page_number}"))
    candidates.sort(key=lambda item: (-item[1], len(item[0]), item[0].casefold()))

    selected_pages = sorted(candidate_pages)
    if len(selected_pages) > 60:
        selected_pages = selected_pages[:60]
    return candidates, {
        "page_count": page_count,
        "candidate_pages": selected_pages,
        "document_year": max(all_years) if all_years else None,
        "currency_detected": currency,
        "scanned_pages": scanned_pages,
    }


def _inspect_xbrl(path: Path) -> tuple[list[tuple[str, float, str]], dict[str, object]]:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")[:5_000_000]
    except Exception:
        return [], {"page_count": 0, "candidate_pages": [], "scanned_pages": 1}
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
    years = extract_years_from_text(text)
    return candidates, {
        "page_count": 0,
        "candidate_pages": [],
        "document_year": max(years) if years else None,
        "currency_detected": detect_currency(text),
        "scanned_pages": 1,
    }


def detect_document_company(path: Path) -> DetectedDocument:
    suffix = path.suffix.casefold()
    if suffix in {".xml", ".xhtml", ".html", ".htm"}:
        candidates, inspection = _inspect_xbrl(path)
    else:
        candidates, inspection = _inspect_pdf(path)

    common = {
        "page_count": int(inspection.get("page_count") or 0),
        "candidate_pages": tuple(int(item) for item in inspection.get("candidate_pages") or []),
        "document_year": inspection.get("document_year"),
        "currency_detected": inspection.get("currency_detected"),
        "scanned_pages": int(inspection.get("scanned_pages") or 0),
    }
    if candidates:
        display, score, method = candidates[0]
        key = normalize_company_key(display)
        confidence = max(0.0, min(0.99, score / 10.0))
        if score >= 5.5 and key:
            return DetectedDocument(
                path=path,
                company_name=display,
                company_key=key,
                confidence=confidence,
                method=method,
                review_required=score < 7.0,
                reason=None if score >= 7.0 else "LOW_COMPANY_DETECTION_CONFIDENCE",
                **common,
            )

    fallback = _filename_fallback(path)
    key = normalize_company_key(fallback)
    return DetectedDocument(
        path=path,
        company_name=fallback,
        company_key=key or "undetected",
        confidence=0.20,
        method="filename fallback",
        review_required=True,
        reason="COMPANY_NAME_NOT_CONFIDENTLY_DETECTED",
        **common,
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
            if len(document.company_name) > len(target.company_name):
                target.company_name = document.company_name
    groups.sort(key=lambda group: (group.company_name.casefold(), group.documents[0].path.name.casefold()))
    for index, group in enumerate(groups, start=1):
        group.artifact_id = _artifact_id(index, group.company_key)
    return groups
