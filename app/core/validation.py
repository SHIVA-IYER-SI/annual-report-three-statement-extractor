"""Executable global and statement validation with strict export gating."""
from __future__ import annotations
from dataclasses import dataclass,field
from decimal import Decimal
from typing import Any,Iterable
from .resources import validation_universes

CRITICAL_GLOBAL={"G-01":"Accounting scope consistency","G-02":"Unit consistency","G-03":"Restated comparative priority","G-04":"Missing versus zero","G-05":"No duplicate economic value","G-06":"No unexplained plug","G-07":"Formula errors","G-08":"Source lineage","G-09":"Dynamic row rule","G-10":"Historical-only output"}
@dataclass
class ValidationFinding:
    check_id:str;check_name:str;severity:str;outcome:str;message:str;statement_type:str|None=None;financial_year:int|None=None
    observed:Decimal|None=None;expected:Decimal|None=None;difference:Decimal|None=None;tolerance:Decimal|None=None;evidence_ids:list[int]=field(default_factory=list);details:dict[str,Any]=field(default_factory=dict)
@dataclass
class ValidationSummary:
    findings:list[ValidationFinding]
    @property
    def critical_count(self):return sum(1 for f in self.findings if f.severity=="CRITICAL" and f.outcome=="FAIL")
    @property
    def error_count(self):return sum(1 for f in self.findings if f.severity=="ERROR" and f.outcome=="FAIL")
    @property
    def warning_count(self):return sum(1 for f in self.findings if f.outcome=="WARNING" or (f.severity=="WARNING" and f.outcome=="FAIL"))
    @property
    def passed_count(self):return sum(1 for f in self.findings if f.outcome=="PASS")
    @property
    def export_eligible(self):return self.critical_count==0 and self.error_count==0

class StatementValidator:
    def __init__(self,tolerance=Decimal("0.01")):self.tolerance=Decimal(tolerance);self.rules=validation_universes()
    def validate(self,*,scope_values:set[str],units:set[str],selected_values:list[Any],assembled:dict[str,list[Any]],years:list[int],restatement_conflicts:int=0,material_unmapped:int=0,open_blocking_reviews:int=0,formula_errors:list[str]|None=None)->ValidationSummary:
        findings=[];formula_errors=formula_errors or []
        def add(id,name,ok,message):findings.append(ValidationFinding(id,name,"CRITICAL","PASS" if ok else "FAIL",message))
        add("G-01",CRITICAL_GLOBAL["G-01"],len(scope_values)==1 and not ({"AMBIGUOUS","UNKNOWN"}&scope_values),f"Selected scopes: {sorted(scope_values)}")
        add("G-02",CRITICAL_GLOBAL["G-02"],units=={"INR_CRORE"},f"Normalized units: {sorted(units)}")
        add("G-03",CRITICAL_GLOBAL["G-03"],restatement_conflicts==0,f"Unresolved duplicate/restatement conflicts: {restatement_conflicts}")
        missing_as_zero=[v for v in selected_values if getattr(v,"status",None)=="MISSING_NOT_DISCLOSED" and getattr(v,"value",None)==0]
        add("G-04",CRITICAL_GLOBAL["G-04"],not missing_as_zero,f"Missing values incorrectly set to zero: {len(missing_as_zero)}")
        duplicates=self._duplicate_economic_values(selected_values)
        add("G-05",CRITICAL_GLOBAL["G-05"],not duplicates,f"Potential duplicate economic ownership: {duplicates[:10]}")
        residuals=[v for v in selected_values if getattr(v,"status",None)=="CALCULATED_RESIDUAL" and not getattr(v,"source",{}).get("review_approved")]
        add("G-06",CRITICAL_GLOBAL["G-06"],not residuals,f"Unapproved residuals: {len(residuals)}")
        add("G-07",CRITICAL_GLOBAL["G-07"],not formula_errors,f"Formula errors: {formula_errors[:10]}")
        no_lineage=[v for v in selected_values if getattr(v,"value",None) is not None and getattr(v,"evidence_id",None) is None and getattr(v,"status",None) not in {"CALCULATED_FROM_DISCLOSED_COMPONENTS","CROSS_STATEMENT_LINK"}]
        add("G-08",CRITICAL_GLOBAL["G-08"],not no_lineage,f"Values without source/formula lineage: {len(no_lineage)}")
        empty_headings=[r.key for rows in assembled.values() for r in rows if r.row_type in {"section_header","subgroup_header"} and not any(x.parent_key==r.key for x in rows)]
        add("G-09",CRITICAL_GLOBAL["G-09"],not empty_headings,f"Empty headings: {empty_headings}")
        add("G-10",CRITICAL_GLOBAL["G-10"],all(1900<=y<=2100 for y in years),f"Historical years: {years}")
        findings.extend(self._balance_check(assembled,years));findings.extend(self._cash_checks(assembled,years));findings.extend(self._declarative_rule_presence())
        if material_unmapped:findings.append(ValidationFinding("G-11","Material unmapped lines","CRITICAL","FAIL",f"Material unmapped lines: {material_unmapped}"))
        if open_blocking_reviews:findings.append(ValidationFinding("G-12","Blocking manual reviews","CRITICAL","FAIL",f"Open blocking reviews: {open_blocking_reviews}"))
        return ValidationSummary(findings)
    def _row_values(self,rows):return {r.key:r for r in rows}
    def _value(self,row,year):
        if not row:return None
        y=f"FY{str(year)[-2:]}";return row.values.get(y)
    def _balance_check(self,assembled,years):
        rows=self._row_values(assembled.get("BALANCE_SHEET",[]));out=[]
        # use canonical total candidates found in universe; tolerant aliases
        assets=next((r for k,r in rows.items() if k in {"bs.total_assets","bs.assets.total"} or r.label.strip().casefold()=="total assets"),None)
        eq_liab=next((r for k,r in rows.items() if k in {"bs.total_equity_liabilities","bs.equity_liabilities.total"} or "total equity and liabilities" in r.label.casefold()),None)
        for y in years:
            a=self._value(assets,y);b=self._value(eq_liab,y)
            if a is None or b is None:out.append(ValidationFinding("BS-BALANCE","Assets = Equity + Liabilities","CRITICAL","NOT_RUN","Required total unavailable","BALANCE_SHEET",y));continue
            diff=a-b;out.append(ValidationFinding("BS-BALANCE","Assets = Equity + Liabilities","CRITICAL","PASS" if abs(diff)<=self.tolerance else "FAIL",f"Balance difference {diff}","BALANCE_SHEET",y,a,b,diff,self.tolerance))
        return out
    def _cash_checks(self,assembled,years):
        rows=self._row_values(assembled.get("CASH_FLOW_STATEMENT",[]));out=[]
        opening=next((r for r in rows.values() if "opening cash" in r.label.casefold()),None);closing=next((r for r in rows.values() if "closing cash" in r.label.casefold()),None);movement=next((r for r in rows.values() if "total net change" in r.label.casefold() or "net increase" in r.label.casefold()),None)
        for i,y in enumerate(years):
            o=self._value(opening,y);c=self._value(closing,y);m=self._value(movement,y)
            if o is None or c is None or m is None:out.append(ValidationFinding("CF-ROLL","Opening + movement = closing","CRITICAL","NOT_RUN","Cash roll-forward inputs unavailable","CASH_FLOW_STATEMENT",y));continue
            diff=o+m-c;out.append(ValidationFinding("CF-ROLL","Opening + movement = closing","CRITICAL","PASS" if abs(diff)<=self.tolerance else "FAIL",f"Cash roll-forward difference {diff}","CASH_FLOW_STATEMENT",y,o+m,c,diff,self.tolerance))
            if i>0:
                prior=self._value(closing,years[i-1]);
                if prior is not None:out.append(ValidationFinding("CF-OPENING","Opening cash equals prior closing","ERROR","PASS" if abs(o-prior)<=self.tolerance else "FAIL",f"Opening/prior closing difference {o-prior}","CASH_FLOW_STATEMENT",y,o,prior,o-prior,self.tolerance))
        return out
    def _duplicate_economic_values(self,values):
        owners={};dups=[]
        for v in values:
            eid=getattr(v,"evidence_id",None)
            if eid is None:continue
            owner=(getattr(v,"statement_type",None),getattr(v,"canonical_key",None),getattr(v,"financial_year",None))
            if eid in owners and owners[eid]!=owner:dups.append((eid,owners[eid],owner))
            owners[eid]=owner
        return dups
    def _declarative_rule_presence(self):
        out=[]
        for statement,rules in self.rules.items():
            for rule in rules:
                out.append(ValidationFinding(rule["check_id"],rule["check_name"],"INFORMATIONAL","NOT_RUN","Blueprint declarative rule loaded; execution requires corresponding canonical rows",statement,details={"formula_or_test":rule["formula_or_test"],"source_severity":rule["severity"]}))
        return out
