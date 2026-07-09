"""Document, statement, scope and table-title detection."""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable
from .parsing import detect_currency, detect_unit, parse_financial_year

STATEMENT_PATTERNS={
 "INCOME_STATEMENT":[r"statement of profit and loss",r"statement of comprehensive income",r"profit\s*(?:and|&)\s*loss account"],
 "BALANCE_SHEET":[r"balance sheet",r"statement of financial position"],
 "CASH_FLOW_STATEMENT":[r"statement of cash flows?",r"cash flow statement"],
}

@dataclass
class StatementDetection:
    statement_type:str; page_number:int; title:str; scope:str; confidence:float; continued_pages:list[int]=field(default_factory=list)

@dataclass
class DocumentClassification:
    document_type:str; company_name:str|None; financial_year:int|None; scope:str; available_scopes:list[str]
    currency:str|None; reporting_unit:str|None; page_count:int; native_pdf:bool|None; scanned_pages:list[int]
    xbrl_kind:str|None; confidence:float; review_reason:str|None=None

def detect_scope(text:str)->tuple[str,list[str],float]:
    consolidated=bool(re.search(r"\bconsolidated\b|subsidiar(?:y|ies)|non[- ]controlling interests?|joint ventures?",text,re.I))
    standalone=bool(re.search(r"\bstandalone\b|separate financial statements?",text,re.I))
    available=[]
    if consolidated: available.append("CONSOLIDATED")
    if standalone: available.append("STANDALONE")
    if len(available)==1:return available[0],available,0.92
    if len(available)>1:return "AMBIGUOUS",available,0.55
    return "UNKNOWN",[],0.2

def detect_statement_pages(page_texts:Iterable[str])->list[StatementDetection]:
    found=[]
    for page_no,text in enumerate(page_texts,start=1):
        low=text.casefold()
        for statement,patterns in STATEMENT_PATTERNS.items():
            match=next((p for p in patterns if re.search(p,low,re.I)),None)
            if not match: continue
            scope,_,scope_conf=detect_scope(text)
            title_line=next((line.strip() for line in text.splitlines() if re.search(match,line,re.I)),statement.replace("_"," "))
            confidence=min(0.99,0.75+scope_conf*0.2)
            found.append(StatementDetection(statement,page_no,title_line,scope,confidence))
    return found

def classify_text_document(path:Path,page_texts:list[str],scanned_pages:list[int],xbrl_kind:str|None=None)->DocumentClassification:
    sample="\n".join(page_texts[:12])
    year=parse_financial_year(sample)
    scope,available,scope_conf=detect_scope(sample)
    company=None
    for line in sample.splitlines()[:100]:
        if re.search(r"\b(limited|ltd\.?|corporation|company)\b",line,re.I) and 3<len(line.strip())<180:
            company=line.strip(); break
    suffix=path.suffix.lower()
    if xbrl_kind: dtype=xbrl_kind
    elif suffix==".pdf" and re.search(r"annual report",sample,re.I): dtype="ANNUAL_REPORT"
    elif suffix==".pdf" and re.search(r"quarter|three months|nine months",sample,re.I): dtype="QUARTERLY_RESULTS"
    elif suffix==".pdf": dtype="ANNUAL_RESULTS" if year else "OTHER_FINANCIAL_DOCUMENT"
    else: dtype="UNKNOWN"
    unit=detect_unit(sample); currency=detect_currency(sample)
    native=None if suffix!=".pdf" else len(scanned_pages)<max(1,len(page_texts)//2)
    reason=None
    if scope in {"AMBIGUOUS","UNKNOWN"}: reason="ACCOUNTING_SCOPE_REQUIRES_REVIEW"
    if unit is None: reason=(reason+";" if reason else "")+"REPORTING_UNIT_NOT_CONFIRMED"
    confidence=max(0.1,min(0.98,0.3+0.25*(year is not None)+0.2*(company is not None)+0.15*(unit is not None)+0.1*scope_conf))
    return DocumentClassification(dtype,company,year,scope,available,currency,unit,len(page_texts),native,scanned_pages,xbrl_kind,confidence,reason)
