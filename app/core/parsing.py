"""Deterministic parsing for annual-report labels, years, values, units and signs.

This keeps the Block 20A value semantics while broadening common annual-report
header recognition (for example, ``31 March 2026`` and bare year columns).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Iterable

DASHES = {"-", "–", "—", "−", "nil", "n.a.", "na"}
BLANKS = {"", "none", "null"}
UNIT_FACTORS_TO_CRORE = {
    "rupee": Decimal("0.0000001"),
    "rupees": Decimal("0.0000001"),
    "inr": Decimal("0.0000001"),
    "thousand": Decimal("0.0001"),
    "thousands": Decimal("0.0001"),
    "lakh": Decimal("0.01"),
    "lakhs": Decimal("0.01"),
    "million": Decimal("0.1"),
    "millions": Decimal("0.1"),
    "crore": Decimal("1"),
    "crores": Decimal("1"),
}


@dataclass(frozen=True)
class ParsedNumber:
    value: Decimal | None
    status: str
    raw: str
    explicit_zero: bool = False
    ambiguous_dash: bool = False


def normalize_label(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).casefold()
    text = re.sub(r"[†‡*#]+", " ", text)
    text = re.sub(r"\b(note|notes)\s*\d+[a-z]?(?:\.\d+)?\b", " ", text)
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9%]+", " ", text)
    return " ".join(text.split())


def parse_number(
    value: object,
    dash_means_zero: bool | None = None,
    expense_as_positive: bool = False,
) -> ParsedNumber:
    if value is None:
        return ParsedNumber(None, "MISSING_NOT_DISCLOSED", "")
    original = str(value).strip()
    lowered = original.casefold()
    if lowered in BLANKS:
        return ParsedNumber(None, "MISSING_NOT_DISCLOSED", original)
    if lowered in DASHES:
        if dash_means_zero is True or lowered == "nil":
            return ParsedNumber(Decimal(0), "DISCLOSED_ZERO", original, True)
        return ParsedNumber(None, "AMBIGUOUS_REVIEW_REQUIRED", original, False, True)

    raw = original
    negative = False
    if raw.startswith("(") and raw.endswith(")"):
        negative = True
        raw = raw[1:-1]
    raw = (
        raw.replace("₹", "")
        .replace("Rs.", "")
        .replace("Rs", "")
        .replace("INR", "")
        .replace(",", "")
        .replace(" ", "")
        .replace("−", "-")
        .replace("–", "-")
    )
    if raw.endswith("-"):
        negative = True
        raw = raw[:-1]
    # Strip common footnote markers after a valid numeric token.
    match = re.fullmatch(r"([+-]?\d+(?:\.\d+)?)(?:[a-zA-Z*†‡]+)?", raw)
    if match:
        raw = match.group(1)
    try:
        number = Decimal(raw)
    except InvalidOperation:
        return ParsedNumber(None, "AMBIGUOUS_REVIEW_REQUIRED", original)
    if negative:
        number = -abs(number)
    if expense_as_positive:
        number = abs(number)
    return ParsedNumber(
        number,
        "DISCLOSED_ZERO" if number == 0 else "REPORTED",
        original,
        number == 0,
    )


def detect_unit(text: str) -> str | None:
    normalized = normalize_label(text)
    patterns = [
        (r"\b(?:in|amounts? in|figures? in)\s+(?:rs\s*)?crores?\b", "crore"),
        (r"\b(?:in|amounts? in|figures? in)\s+(?:rs\s*)?millions?\b", "million"),
        (r"\b(?:in|amounts? in|figures? in)\s+(?:rs\s*)?lakhs?\b", "lakh"),
        (r"\b(?:in|amounts? in|figures? in)\s+(?:rs\s*)?thousands?\b", "thousand"),
        (r"\b(?:in|amounts? in|figures? in)\s+(?:rupees?|inr)\b", "rupee"),
        (r"\bcrores?\b", "crore"),
        (r"\bmillions?\b", "million"),
        (r"\blakhs?\b", "lakh"),
        (r"\bthousands?\b", "thousand"),
    ]
    for pattern, unit in patterns:
        if re.search(pattern, normalized):
            return unit
    return None


def factor_to_crore(unit: str | None) -> Decimal | None:
    return UNIT_FACTORS_TO_CRORE.get(normalize_label(unit or ""))


def normalize_to_crore(value: Decimal | None, unit: str | None) -> Decimal | None:
    if value is None:
        return None
    factor = factor_to_crore(unit)
    return value * factor if factor is not None else None


def extract_years_from_text(text: str) -> list[int]:
    """Return plausible financial years in left-to-right order."""
    if not text:
        return []
    years: list[int] = []
    # FY25 / FY2025
    for match in re.finditer(r"\bfy\s*['’\-]?\s*(\d{2,4})\b", text, re.I):
        year = int(match.group(1))
        year = 2000 + year if year < 100 else year
        if 1900 <= year <= 2100 and year not in years:
            years.append(year)
    # 2024-25 / 2024/2025 / dates containing a four-digit year / bare year headers.
    for match in re.finditer(r"(?<!\d)(19\d{2}|20\d{2}|2100)(?!\d)", text):
        year = int(match.group(1))
        if year not in years:
            years.append(year)
    return years


def parse_financial_year(text: str) -> int | None:
    years = extract_years_from_text(text)
    return years[-1] if years else None


def extract_year_headers(cells: Iterable[object]) -> list[int]:
    years: list[int] = []
    for cell in cells:
        for year in extract_years_from_text(str(cell or "")):
            if year not in years:
                years.append(year)
    return years


def parse_period_end(text: str) -> date | None:
    year = parse_financial_year(text)
    if not year:
        return None
    if re.search(r"(?:31\s+march|march\s+31)", text, re.I):
        return date(year, 3, 31)
    if re.search(r"(?:31\s+december|december\s+31)", text, re.I):
        return date(year, 12, 31)
    return date(year, 3, 31)


def detect_currency(text: str) -> str | None:
    if re.search(r"₹|\bINR\b|Indian rupees?", text, re.I):
        return "INR"
    if re.search(r"\bUSD\b|US\$", text, re.I):
        return "USD"
    if re.search(r"\bEUR\b|€", text, re.I):
        return "EUR"
    if re.search(r"\bGBP\b|£", text, re.I):
        return "GBP"
    return None
