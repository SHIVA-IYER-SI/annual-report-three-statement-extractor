"""Disclosure-driven dynamic row assembly and canonical formula definitions."""
from __future__ import annotations
from dataclasses import dataclass,field
from decimal import Decimal
from typing import Any,Iterable
from .resources import line_item_universes

SHEET_BY_STATEMENT={"INCOME_STATEMENT":"INCOME STATEMENT","BALANCE_SHEET":"BALANCE SHEET","CASH_FLOW_STATEMENT":"CASH FLOW STATEMENT"}
@dataclass
class SelectedValue:
    evidence_id:int|None; statement_type:str; canonical_key:str; financial_year:int; value:Decimal|None; status:str; source:dict[str,Any]=field(default_factory=dict)
@dataclass
class AssembledRow:
    key:str; label:str; row_type:str; value_type:str; hierarchy_level:int; parent_key:str|None; display_order:int
    values:dict[str,Decimal|None]=field(default_factory=dict); statuses:dict[str,str]=field(default_factory=dict); evidence_ids:dict[str,int|None]=field(default_factory=dict)
    formulas:dict[str,dict[str,Any]]=field(default_factory=dict); sources:dict[str,list[dict[str,Any]]]=field(default_factory=dict); inclusion_reason:str="DISCLOSED"; blank_after:bool=False

class DynamicStatementAssembler:
    def __init__(self,tolerance:Decimal=Decimal("0.01")):
        self.universes=line_item_universes(); self.tolerance=tolerance
    def assemble(self,selected:Iterable[SelectedValue],years:list[int])->dict[str,list[AssembledRow]]:
        selected=list(selected); by_statement={s:[] for s in self.universes}
        for s in by_statement: by_statement[s]=[v for v in selected if v.statement_type==s]
        result={}
        for statement,universe in self.universes.items(): result[statement]=self._assemble_one(statement,universe,by_statement[statement],years)
        return result
    def _assemble_one(self,statement,universe,values,years):
        meta={r["canonical_key"]:r for r in universe}; order={r["canonical_key"]:i for i,r in enumerate(universe)}
        value_map={(v.canonical_key,v.financial_year):v for v in values if v.canonical_key in meta}
        included={v.canonical_key for v in values if v.canonical_key in meta}
        # include all ancestors of disclosed rows
        changed=True
        while changed:
            changed=False
            for key in list(included):
                parent=meta[key].get("parent_key") if key in meta else None
                if parent and parent not in included: included.add(parent);changed=True
        # include required principal totals/checks when direct children reconcile or row explicitly selected
        children={k:[] for k in meta}
        for r in universe:
            if r.get("parent_key") in children:children[r["parent_key"]].append(r["canonical_key"])
        rows=[]
        for key in sorted(included,key=lambda k:order[k]):
            m=meta[key]; row=AssembledRow(key,m["display_label"],m.get("row_type","component"),m.get("value_type","currency"),int(m.get("hierarchy_level",0)),m.get("parent_key") or None,order[key],blank_after=False)
            for year in years:
                y=f"FY{str(year)[-2:]}"; v=value_map.get((key,year))
                if v:
                    row.values[y]=v.value;row.statuses[y]=v.status;row.evidence_ids[y]=v.evidence_id;row.sources[y]=[v.source] if v.source else []
            # formula only when all included direct children have values and reconcile to reported total (if any), per year
            direct=[c for c in children.get(key,[]) if c in included and meta[c].get("row_type") not in {"section_header","subgroup_header","supplement_header"}]
            for year in years:
                y=f"FY{str(year)[-2:]}"
                child_vals=[value_map.get((c,year)) for c in direct]
                complete=bool(direct) and all(cv is not None and cv.value is not None for cv in child_vals)
                if complete:
                    child_sum=sum((cv.value for cv in child_vals if cv and cv.value is not None),Decimal(0))
                    reported=value_map.get((key,year))
                    if reported is None or reported.value is None or abs(child_sum-reported.value)<=self.tolerance:
                        row.formulas[y]={"type":"sum_keys","keys":direct}
                        row.statuses[y]="CALCULATED_FROM_DISCLOSED_COMPONENTS"
                        if reported:row.evidence_ids[y]=reported.evidence_id
            rows.append(row)
        # remove empty headings (ancestors logic normally prevents them); mark blank separation at top-level group endings
        child_presence={r.key:False for r in rows}
        for r in rows:
            if r.parent_key in child_presence:child_presence[r.parent_key]=True
        rows=[r for r in rows if r.row_type not in {"section_header","subgroup_header","supplement_header"} or child_presence.get(r.key,False)]
        return rows

def workbook_payload(company_name:str,scope:str,years:list[int],assembled:dict[str,list[AssembledRow]])->dict[str,Any]:
    payload={"company_name":company_name,"scope":scope.lower(),"currency_unit":"INR_crore","years":[f"FY{str(y)[-2:]}" for y in years],"statements":{}}
    for statement,rows in assembled.items():
        sheet=SHEET_BY_STATEMENT[statement]; payload["statements"][sheet]=[]
        for r in rows:
            payload["statements"][sheet].append({"key":r.key,"label":r.label,"row_type":r.row_type,"value_type":r.value_type,"hierarchy_level":r.hierarchy_level,"include":True,"has_included_children":True,"blank_after":r.blank_after,"values":{k:(str(v) if v is not None else None) for k,v in r.values.items()},"formulas":r.formulas,"sources":r.sources,"statuses":r.statuses,"evidence_ids":r.evidence_ids})
    return payload
