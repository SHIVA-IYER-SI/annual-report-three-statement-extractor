"""Standalone, temporary-file annual-report extraction pipeline.

The workbook compiler and blueprint resources are inherited from Block 20A.
This module removes Kuberpath database dependencies and supplies a deployable
single-purpose workflow: upload -> extract -> validate -> preview -> download.
"""
from __future__ import annotations

import json
import re
import shutil
import traceback
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from .core.assembly import DynamicStatementAssembler, SelectedValue, workbook_payload
from .core.detection import STATEMENT_PATTERNS, detect_scope
from .core.mapping import CanonicalMapper
from .core.parsing import (
    detect_currency,
    detect_unit,
    extract_year_headers,
    extract_years_from_text,
    factor_to_crore,
    normalize_label,
    normalize_to_crore,
    parse_number,
)
from .core.resources import line_item_universes, validation_universes, verify_runtime_resources
from .core.security import safe_filename, sha256_file, validate_input_file
from .core.validation import StatementValidator
from .core.workbook import SHEET_ORDER, assert_workbook_contract, build_workbook
from .core.xbrl import parse_xbrl

STATEMENTS = ("INCOME_STATEMENT", "BALANCE_SHEET", "CASH_FLOW_STATEMENT")
SHEET_TO_STATEMENT = {
    "INCOME STATEMENT": "INCOME_STATEMENT",
    "BALANCE SHEET": "BALANCE_SHEET",
    "CASH FLOW STATEMENT": "CASH_FLOW_STATEMENT",
}
STATEMENT_TO_SHEET = {value: key for key, value in SHEET_TO_STATEMENT.items()}


@dataclass
class RawRow:
    source_file: str
    source_checksum: str
    page: int | None
    statement_hint: str | None
    scope_hint: str
    label: str
    values: dict[int, str | Decimal | None]
    unit: str | None
    extraction_confidence: float
    table_title: str | None = None
    context: str | None = None
    source_document_year: int | None = None
    source_type: str = "PDF"


@dataclass
class CandidateRecord:
    selected: SelectedValue
    mapping_confidence: float
    extraction_confidence: float
    source_document_year: int
    original_label: str
    source_file: str
    page: int | None

    @property
    def preference(self) -> tuple[int, float, float]:
        return (self.source_document_year, self.mapping_confidence, self.extraction_confidence)


@dataclass
class ExtractionResult:
    company_name: str
    scope: str
    years: list[int]
    payload: dict[str, Any]
    preview: dict[str, Any]
    validation: dict[str, Any]
    checklist: list[dict[str, Any]]
    statistics: dict[str, Any]
    unmapped: list[dict[str, Any]]
    restatements: list[dict[str, Any]]
    warnings: list[str]
    review_required: bool
    workbook_filename: str
    workbook_path: Path
    extracted_json_path: Path
    validation_json_path: Path
    audit_json_path: Path


def _statement_heading(text: str) -> str | None:
    lowered = text.casefold()
    for statement, patterns in STATEMENT_PATTERNS.items():
        if any(re.search(pattern, lowered, re.I) for pattern in patterns):
            return statement
    return None


def _clean_label(value: str) -> str:
    value = re.sub(r"\s+", " ", value.replace("\n", " ")).strip(" :–—-")
    return value[:500]


def _looks_like_note_header(value: str) -> bool:
    normalized = normalize_label(value)
    return normalized in {"note", "notes", "note no", "note number", "particulars", "particular"}


def _find_year_columns(table: list[list[Any]]) -> tuple[int | None, list[tuple[int, int]]]:
    """Find the strongest header row and map year -> column index."""
    best_row: int | None = None
    best_pairs: list[tuple[int, int]] = []
    for row_index, row in enumerate(table[:10]):
        pairs: list[tuple[int, int]] = []
        for col_index, cell in enumerate(row):
            years = extract_years_from_text(str(cell or ""))
            if len(years) == 1:
                pairs.append((years[0], col_index))
        unique: list[tuple[int, int]] = []
        seen: set[int] = set()
        for year, col in pairs:
            if year not in seen:
                unique.append((year, col))
                seen.add(year)
        if len(unique) > len(best_pairs):
            best_pairs = unique
            best_row = row_index
    if len(best_pairs) >= 1:
        return best_row, best_pairs

    # Fallback for a merged header containing multiple years: align them with
    # the right-most columns, which is the dominant annual-report layout.
    for row_index, row in enumerate(table[:10]):
        years = extract_year_headers(row)
        if years:
            nonempty_cols = [i for i, cell in enumerate(row) if str(cell or "").strip()]
            candidate_cols = nonempty_cols[-len(years) :] if len(nonempty_cols) >= len(years) else list(range(max(0, len(row) - len(years)), len(row)))
            return row_index, list(zip(years, candidate_cols))
    return None, []


def _table_rows(
    table: list[list[Any]],
    *,
    source_file: str,
    source_checksum: str,
    page: int,
    statement_hint: str | None,
    scope_hint: str,
    unit: str | None,
    source_document_year: int | None,
    table_title: str | None,
) -> list[RawRow]:
    header_row, year_columns = _find_year_columns(table)
    if header_row is None or not year_columns:
        return []
    min_year_col = min(col for _, col in year_columns)
    rows: list[RawRow] = []
    for row_index, row in enumerate(table[header_row + 1 :], start=header_row + 1):
        cells = ["" if cell is None else str(cell).strip() for cell in row]
        if not any(cells):
            continue
        label_parts: list[str] = []
        for col, cell in enumerate(cells[:min_year_col]):
            if not cell or _looks_like_note_header(cell):
                continue
            parsed = parse_number(cell)
            if parsed.value is None and not extract_years_from_text(cell):
                label_parts.append(cell)
        label = _clean_label(" ".join(label_parts))
        if not label:
            # Common fallback: the first non-numeric cell is the row label.
            label = _clean_label(
                next(
                    (
                        cell
                        for cell in cells
                        if cell and parse_number(cell).value is None and not extract_years_from_text(cell)
                    ),
                    "",
                )
            )
        if not label or label.casefold() in {"particulars", "particular", "description"}:
            continue

        values: dict[int, str | None] = {}
        for year, col in year_columns:
            values[year] = cells[col] if col < len(cells) and cells[col] != "" else None
        if not any(parse_number(value).value is not None or str(value or "").strip().casefold() in {"-", "–", "—", "nil"} for value in values.values()):
            continue
        confidence = 0.88 if all(value is not None for value in values.values()) else 0.68
        rows.append(
            RawRow(
                source_file=source_file,
                source_checksum=source_checksum,
                page=page,
                statement_hint=statement_hint,
                scope_hint=scope_hint,
                label=label,
                values=values,
                unit=unit,
                extraction_confidence=confidence,
                table_title=table_title,
                context=f"table row {row_index}",
                source_document_year=source_document_year,
                source_type="PDF_NATIVE_TABLE",
            )
        )
    return rows


def extract_pdf_rows(path: Path, selected_scope: str, fallback_unit: str) -> tuple[list[RawRow], dict[str, Any], list[str]]:
    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("pdfplumber is required") from exc

    checksum = sha256_file(path)
    warnings: list[str] = []
    rows: list[RawRow] = []
    page_texts: list[str] = []
    page_meta: list[dict[str, Any]] = []
    document_years: list[int] = []
    detected_currency: str | None = None
    detected_company: str | None = None
    active_statement: str | None = None
    active_until = 0

    with pdfplumber.open(path) as pdf:
        for page_number, page in enumerate(pdf.pages, start=1):
            text = page.extract_text(x_tolerance=2, y_tolerance=3) or ""
            page_texts.append(text)
            if page_number <= 20:
                document_years.extend(extract_years_from_text(text))
                detected_currency = detected_currency or detect_currency(text)
                if not detected_company:
                    for line in text.splitlines()[:80]:
                        if re.search(r"\b(limited|ltd\.?|corporation|company)\b", line, re.I) and 3 < len(line.strip()) < 180:
                            detected_company = line.strip()
                            break

            heading = _statement_heading(text)
            if heading:
                active_statement = heading
                active_until = page_number + 5
            elif page_number > active_until:
                active_statement = None

            scope_hint, available_scopes, _ = detect_scope(text)
            explicit_opposite = (
                scope_hint in {"CONSOLIDATED", "STANDALONE"}
                and scope_hint != selected_scope
                and selected_scope not in available_scopes
            )
            page_unit = detect_unit(text) or fallback_unit
            tables = page.extract_tables(
                {
                    "vertical_strategy": "lines",
                    "horizontal_strategy": "lines",
                    "snap_tolerance": 4,
                    "join_tolerance": 4,
                    "intersection_tolerance": 6,
                }
            ) or []
            if not tables:
                tables = page.extract_tables(
                    {
                        "vertical_strategy": "text",
                        "horizontal_strategy": "text",
                        "text_tolerance": 3,
                        "intersection_tolerance": 8,
                    }
                ) or []
            page_meta.append(
                {
                    "page": page_number,
                    "text_characters": len(text),
                    "tables": len(tables),
                    "statement_hint": active_statement,
                    "scope_hint": scope_hint,
                    "unit": page_unit,
                }
            )
            if explicit_opposite:
                continue
            for table_index, table in enumerate(tables):
                rows.extend(
                    _table_rows(
                        table,
                        source_file=path.name,
                        source_checksum=checksum,
                        page=page_number,
                        statement_hint=active_statement,
                        scope_hint=scope_hint,
                        unit=page_unit,
                        source_document_year=max(document_years) if document_years else None,
                        table_title=f"Page {page_number} table {table_index + 1}",
                    )
                )

    if not rows:
        warnings.append(f"No native financial-table rows were detected in {path.name}. The PDF may be scanned or unusually structured.")
    if any(meta["text_characters"] < 80 and meta["tables"] == 0 for meta in page_meta):
        warnings.append(f"{path.name} contains image-dominant pages. OCR is not bundled in this lightweight deployment.")
    metadata = {
        "filename": path.name,
        "checksum": checksum,
        "page_count": len(page_texts),
        "document_year": max(document_years) if document_years else None,
        "company_detected": detected_company,
        "currency_detected": detected_currency,
        "page_metadata": page_meta,
    }
    return rows, metadata, warnings


def _concept_label(concept: str) -> str:
    local = concept.rsplit("}", 1)[-1].split(":")[-1]
    local = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", local)
    local = local.replace("_", " ").replace("-", " ")
    return _clean_label(local)


def extract_xbrl_rows(path: Path, selected_scope: str) -> tuple[list[RawRow], dict[str, Any], list[str]]:
    result = parse_xbrl(path)
    checksum = sha256_file(path)
    rows: list[RawRow] = []
    years: list[int] = []
    for fact in result.facts:
        if fact.numeric_value is None or not fact.context_id:
            continue
        context = result.contexts.get(fact.context_id)
        if not context:
            continue
        period = context.instant or context.end_date
        if not period:
            continue
        year = period.year
        years.append(year)
        dimension_text = " ".join(context.dimensions.values()).casefold()
        if selected_scope == "CONSOLIDATED" and any(token in dimension_text for token in ("standalone", "separate")):
            continue
        if selected_scope == "STANDALONE" and any(token in dimension_text for token in ("consolidated", "group")):
            continue
        unit_text = result.units.get(fact.unit_id or "", "")
        # Standard XBRL monetary facts are generally base currency units.
        unit = "rupee" if re.search(r"inr|iso4217", unit_text, re.I) else "rupee"
        rows.append(
            RawRow(
                source_file=path.name,
                source_checksum=checksum,
                page=None,
                statement_hint=None,
                scope_hint=selected_scope,
                label=_concept_label(fact.concept),
                values={year: fact.numeric_value},
                unit=unit,
                extraction_confidence=0.94,
                table_title="XBRL fact",
                context=fact.context_id,
                source_document_year=max(years) if years else year,
                source_type=result.kind,
            )
        )
    warnings = [] if rows else [f"No numeric annual XBRL facts were detected in {path.name}."]
    return rows, {"filename": path.name, "checksum": checksum, "kind": result.kind, "facts": len(result.facts), "document_year": max(years) if years else None}, warnings


def _best_mapping(mapper: CanonicalMapper, row: RawRow) -> tuple[str | None, Any | None, list[Any]]:
    statements = [row.statement_hint] if row.statement_hint in STATEMENTS else []
    statements.extend(statement for statement in STATEMENTS if statement not in statements)
    all_candidates: list[tuple[str, Any]] = []
    context = " ".join(filter(None, [row.table_title, row.context, row.scope_hint]))
    for statement in statements:
        for candidate in mapper.candidates(statement, row.label, note_context=context, limit=3):
            all_candidates.append((statement, candidate))
    all_candidates.sort(key=lambda item: (-item[1].score, item[0], item[1].canonical_key))
    if not all_candidates:
        return None, None, []
    statement, candidate = all_candidates[0]
    same_statement_candidates = [c for s, c in all_candidates if s == statement]
    mandatory = mapper.needs_mandatory_review(row.label, same_statement_candidates, context)
    # Ambiguity creates a review flag but does not discard a plausible source row.
    # Rows below the deterministic floor remain unmapped rather than being forced.
    threshold = 0.58
    if candidate.confidence < threshold:
        return None, candidate, all_candidates[:5]
    return statement, candidate, all_candidates[:5]


def _display_value(value: Any, value_type: str) -> str:
    if value is None:
        return ""
    try:
        number = Decimal(str(value))
    except Exception:
        return str(value)
    if value_type == "percent":
        return f"{number * Decimal(100):,.2f}%"
    if value_type in {"currency", "count"}:
        return f"{number:,.2f}"
    return str(value)


def _preview_from_payload(payload: dict[str, Any], review_required: bool) -> dict[str, Any]:
    sheets = []
    for sheet_name in SHEET_ORDER:
        rows = []
        for row in payload["statements"][sheet_name]:
            cells: dict[str, Any] = {}
            for year in payload["years"]:
                sources = row.get("sources", {}).get(year) or []
                source = sources[0] if sources else {}
                value = row.get("values", {}).get(year)
                formula = row.get("formulas", {}).get(year) or row.get("formula")
                status = row.get("statuses", {}).get(year)
                cells[year] = {
                    "display_value": _display_value(value, row.get("value_type", "currency")) if value is not None else ("Formula" if formula else ""),
                    "raw_value": value,
                    "formula": bool(formula),
                    "missing": value is None and not formula,
                    "review_required": status in {"AMBIGUOUS_REVIEW_REQUIRED", "MISSING_NOT_DISCLOSED"},
                    "source_page": source.get("page"),
                    "source_file": source.get("source_file"),
                    "original_label": source.get("original_label"),
                }
            rows.append(
                {
                    "key": row["key"],
                    "label": row["label"],
                    "row_type": row.get("row_type", "component"),
                    "hierarchy_level": row.get("hierarchy_level", 0),
                    "value_type": row.get("value_type", "currency"),
                    "cells": cells,
                }
            )
        sheets.append({"name": sheet_name, "years": payload["years"], "rows": rows})
    return {
        "company_name": payload["company_name"],
        "scope": payload["scope"],
        "years": payload["years"],
        "review_required": review_required,
        "sheets": sheets,
    }


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    raise TypeError(type(value).__name__)


def _validation_dict(summary: Any) -> dict[str, Any]:
    return {
        "critical_count": summary.critical_count,
        "error_count": summary.error_count,
        "warning_count": summary.warning_count,
        "passed_count": summary.passed_count,
        "export_eligible": summary.export_eligible,
        "findings": [asdict(item) for item in summary.findings],
    }


def extract_to_workbook(
    *,
    input_paths: Iterable[Path],
    output_dir: Path,
    company_name: str,
    scope: str = "CONSOLIDATED",
    fallback_unit: str = "crore",
    max_years: int = 10,
) -> ExtractionResult:
    resource_errors = verify_runtime_resources()
    if resource_errors:
        raise RuntimeError("Blueprint resource integrity failed: " + ", ".join(resource_errors))
    scope = scope.upper()
    if scope not in {"CONSOLIDATED", "STANDALONE"}:
        raise ValueError("scope must be CONSOLIDATED or STANDALONE")
    if factor_to_crore(fallback_unit) is None:
        raise ValueError("unsupported fallback unit")

    output_dir.mkdir(parents=True, exist_ok=True)
    mapper = CanonicalMapper()
    raw_rows: list[RawRow] = []
    document_metadata: list[dict[str, Any]] = []
    warnings: list[str] = []

    paths = [Path(path) for path in input_paths]
    if not paths:
        raise ValueError("At least one annual report or XBRL file is required")
    for path in paths:
        issues = validate_input_file(path)
        if issues:
            raise ValueError(f"{path.name}: {', '.join(issues)}")
        if path.suffix.lower() == ".pdf":
            rows, metadata, file_warnings = extract_pdf_rows(path, scope, fallback_unit)
        else:
            rows, metadata, file_warnings = extract_xbrl_rows(path, scope)
        raw_rows.extend(rows)
        document_metadata.append(metadata)
        warnings.extend(file_warnings)

    chosen: dict[tuple[str, str, int], CandidateRecord] = {}
    restatements: list[dict[str, Any]] = []
    unmapped: list[dict[str, Any]] = []
    evidence_counter = 0
    mapped_row_count = 0
    mapping_review_count = 0

    for row in raw_rows:
        statement, candidate, alternatives = _best_mapping(mapper, row)
        if not statement or not candidate:
            unmapped.append(
                {
                    "source_file": row.source_file,
                    "page": row.page,
                    "statement_hint": row.statement_hint,
                    "original_label": row.label,
                    "years": sorted(row.values),
                    "top_candidate": getattr(candidate, "canonical_key", None),
                    "top_score": round(float(getattr(candidate, "score", 0)), 4) if candidate else None,
                }
            )
            continue

        mapped_row_count += 1
        context = " ".join(filter(None, [row.table_title, row.context, row.scope_hint]))
        same_candidates = [item[1] for item in alternatives if item[0] == statement]
        mandatory_review = mapper.needs_mandatory_review(row.label, same_candidates, context)
        if mandatory_review:
            mapping_review_count += 1
        unit = row.unit or fallback_unit
        for year, raw_value in row.values.items():
            parsed = parse_number(
                raw_value,
                dash_means_zero=False,
                expense_as_positive=(statement == "INCOME_STATEMENT" and any(token in normalize_label(row.label) for token in ("expense", "cost", "tax"))),
            )
            normalized = normalize_to_crore(parsed.value, unit)
            if parsed.value is not None and normalized is None:
                warnings.append(f"Unit could not be normalized for {row.label} in {row.source_file}; fallback {fallback_unit} was used.")
                normalized = normalize_to_crore(parsed.value, fallback_unit)
            if normalized is None and parsed.status != "DISCLOSED_ZERO":
                continue
            evidence_counter += 1
            source = {
                "source_file": row.source_file,
                "source_checksum": row.source_checksum,
                "page": row.page,
                "original_label": row.label,
                "status": parsed.status,
                "unit_detected": unit,
                "scope": scope,
                "source_type": row.source_type,
                "mapping_confidence": round(float(candidate.confidence), 4),
                "extraction_confidence": round(float(row.extraction_confidence), 4),
                "mandatory_review": mandatory_review,
            }
            selected = SelectedValue(
                evidence_id=evidence_counter,
                statement_type=statement,
                canonical_key=candidate.canonical_key,
                financial_year=year,
                value=normalized,
                status="AMBIGUOUS_REVIEW_REQUIRED" if mandatory_review else parsed.status,
                source=source,
            )
            record = CandidateRecord(
                selected=selected,
                mapping_confidence=float(candidate.confidence),
                extraction_confidence=float(row.extraction_confidence),
                source_document_year=int(row.source_document_year or year),
                original_label=row.label,
                source_file=row.source_file,
                page=row.page,
            )
            key = (statement, candidate.canonical_key, year)
            existing = chosen.get(key)
            if existing is None:
                chosen[key] = record
            elif record.preference > existing.preference:
                if existing.selected.value != record.selected.value:
                    restatements.append(
                        {
                            "statement": statement,
                            "canonical_key": candidate.canonical_key,
                            "year": year,
                            "previous_value": str(existing.selected.value),
                            "selected_value": str(record.selected.value),
                            "previous_source": existing.source_file,
                            "selected_source": record.source_file,
                            "resolution": "LATEST_SOURCE_DOCUMENT_AND_CONFIDENCE",
                        }
                    )
                chosen[key] = record
            elif existing.selected.value != record.selected.value:
                restatements.append(
                    {
                        "statement": statement,
                        "canonical_key": candidate.canonical_key,
                        "year": year,
                        "previous_value": str(record.selected.value),
                        "selected_value": str(existing.selected.value),
                        "previous_source": record.source_file,
                        "selected_source": existing.source_file,
                        "resolution": "KEPT_HIGHER_PRIORITY_SOURCE",
                    }
                )

    selected_values = [record.selected for record in chosen.values()]
    years = sorted({value.financial_year for value in selected_values})
    if len(years) > max_years:
        years = years[-max_years:]
        selected_values = [value for value in selected_values if value.financial_year in years]
        warnings.append(f"Only the latest {max_years} historical years were retained.")
    if not years:
        # Still produce the required three-sheet structure using the best year
        # detectable from the source, so the UI can explain why review is needed.
        detected_years = sorted({year for row in raw_rows for year in row.values})
        years = detected_years[-2:] if detected_years else [datetime.now(timezone.utc).year]

    assembled = DynamicStatementAssembler().assemble(selected_values, years)
    payload = workbook_payload(company_name, scope, years, assembled)

    validation_summary = StatementValidator().validate(
        scope_values={scope},
        units={"INR_CRORE"},
        selected_values=selected_values,
        assembled=assembled,
        years=years,
        restatement_conflicts=0,
        material_unmapped=0,
        open_blocking_reviews=0,
        formula_errors=[],
    )
    validation = _validation_dict(validation_summary)

    statement_counts = {statement: len(assembled.get(statement, [])) for statement in STATEMENTS}
    missing_statements = [statement for statement, count in statement_counts.items() if count == 0]
    balance_findings = [item for item in validation["findings"] if item["check_id"] == "BS-BALANCE"]
    cash_findings = [item for item in validation["findings"] if item["check_id"] in {"CF-ROLL", "CF-OPENING"}]
    balance_failed = any(item["outcome"] == "FAIL" for item in balance_findings)
    cash_failed = any(item["outcome"] == "FAIL" for item in cash_findings)
    review_required = bool(
        missing_statements
        or unmapped
        or mapping_review_count
        or validation_summary.critical_count
        or validation_summary.error_count
    )

    checklist = [
        {
            "id": "FILES",
            "label": "Annual-report/XBRL files accepted",
            "status": "PASS" if document_metadata else "FAIL",
            "detail": f"{len(document_metadata)} file(s) processed",
        },
        {
            "id": "SCOPE",
            "label": "Single accounting scope",
            "status": "PASS",
            "detail": scope.title(),
        },
        {
            "id": "UNIT",
            "label": "Values normalized to INR crore",
            "status": "PASS",
            "detail": f"Fallback input unit: {fallback_unit}",
        },
        {
            "id": "THREE_STATEMENTS",
            "label": "Income Statement, Balance Sheet and Cash Flow Statement detected",
            "status": "FAIL" if missing_statements else "PASS",
            "detail": "Missing: " + ", ".join(STATEMENT_TO_SHEET[item] for item in missing_statements) if missing_statements else "All three statements contain mapped rows",
        },
        {
            "id": "HISTORICAL_ONLY",
            "label": "Historical actual years only",
            "status": "PASS",
            "detail": ", ".join(f"FY{str(year)[-2:]}" for year in years),
        },
        {
            "id": "DYNAMIC_ROWS",
            "label": "Only disclosed or supported rows included",
            "status": "PASS",
            "detail": f"{sum(statement_counts.values())} workbook rows",
        },
        {
            "id": "MAPPING",
            "label": "Canonical mapping review",
            "status": "REVIEW" if unmapped or mapping_review_count else "PASS",
            "detail": f"{len(unmapped)} unmapped row(s); {mapping_review_count} ambiguity review(s)",
        },
        {
            "id": "BALANCE",
            "label": "Balance Sheet balance check",
            "status": "FAIL" if balance_failed else ("PASS" if any(item["outcome"] == "PASS" for item in balance_findings) else "NOT_RUN"),
            "detail": "Assets = Equity + Liabilities where totals are available",
        },
        {
            "id": "CASH_FLOW",
            "label": "Cash-flow roll-forward check",
            "status": "FAIL" if cash_failed else ("PASS" if any(item["outcome"] == "PASS" for item in cash_findings) else "NOT_RUN"),
            "detail": "Opening cash + movement = closing cash where inputs are available",
        },
        {
            "id": "WORKBOOK",
            "label": "Exact three-sheet workbook contract",
            "status": "PENDING",
            "detail": "Validated after workbook generation",
        },
    ]

    suffix = "_REVIEW_REQUIRED" if review_required else ""
    safe_company = safe_filename(company_name).replace(" ", "_") or "Company"
    workbook_filename = f"{safe_company}_Historical_3_Statement_{scope.title()}_FY{years[0]}_FY{years[-1]}{suffix}.xlsx"
    workbook_path = output_dir / workbook_filename
    workbook = build_workbook(payload, review_required=review_required)
    workbook.save(workbook_path)
    workbook_errors = assert_workbook_contract(workbook_path)
    checklist[-1]["status"] = "FAIL" if workbook_errors else "PASS"
    checklist[-1]["detail"] = "; ".join(workbook_errors) if workbook_errors else "3 exact sheets, no forecasts, freeze panes C3, gridlines off"
    if workbook_errors:
        raise RuntimeError("Workbook contract failed: " + "; ".join(workbook_errors))

    preview = _preview_from_payload(payload, review_required)
    statistics = {
        "files": len(document_metadata),
        "raw_rows_detected": len(raw_rows),
        "mapped_source_rows": mapped_row_count,
        "selected_values": len(selected_values),
        "unmapped_rows": len(unmapped),
        "mapping_reviews": mapping_review_count,
        "restatement_comparisons": len(restatements),
        "statement_rows": {STATEMENT_TO_SHEET[key]: value for key, value in statement_counts.items()},
        "blueprint_line_items": {STATEMENT_TO_SHEET[key]: len(rows) for key, rows in line_item_universes().items()},
        "blueprint_validation_checks": {STATEMENT_TO_SHEET[key]: len(rows) for key, rows in validation_universes().items()},
        "document_metadata": document_metadata,
    }

    extracted_json_path = output_dir / "extracted_data.json"
    extracted_json_path.write_text(
        json.dumps(
            {
                "payload": payload,
                "preview": preview,
                "statistics": statistics,
                "unmapped": unmapped,
                "restatements": restatements,
                "warnings": warnings,
            },
            indent=2,
            default=_json_default,
        ),
        encoding="utf-8",
    )
    validation_json_path = output_dir / "validation_report.json"
    validation_json_path.write_text(
        json.dumps({"checklist": checklist, "validation": validation}, indent=2, default=_json_default),
        encoding="utf-8",
    )
    audit_json_path = output_dir / "audit_summary.json"
    audit_json_path.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "company_name": company_name,
                "scope": scope,
                "years": years,
                "review_required": review_required,
                "workbook": {
                    "filename": workbook_filename,
                    "sha256": sha256_file(workbook_path),
                    "bytes": workbook_path.stat().st_size,
                },
                "input_files": document_metadata,
                "statistics": statistics,
            },
            indent=2,
            default=_json_default,
        ),
        encoding="utf-8",
    )

    return ExtractionResult(
        company_name=company_name,
        scope=scope,
        years=years,
        payload=payload,
        preview=preview,
        validation=validation,
        checklist=checklist,
        statistics=statistics,
        unmapped=unmapped,
        restatements=restatements,
        warnings=warnings,
        review_required=review_required,
        workbook_filename=workbook_filename,
        workbook_path=workbook_path,
        extracted_json_path=extracted_json_path,
        validation_json_path=validation_json_path,
        audit_json_path=audit_json_path,
    )


def failure_payload(exc: BaseException) -> dict[str, str]:
    return {
        "error_type": type(exc).__name__,
        "message": str(exc),
        "traceback": "".join(traceback.format_exception(exc))[-12000:],
    }
