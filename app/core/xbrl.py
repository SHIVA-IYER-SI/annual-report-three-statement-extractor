"""Functional XML XBRL and Inline XBRL fact extraction using stdlib XML."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date
from decimal import Decimal,InvalidOperation
from pathlib import Path
from typing import Any
import re, xml.etree.ElementTree as ET

@dataclass
class XBRLContext:
    context_id:str; entity_identifier:str|None; instant:date|None; start_date:date|None; end_date:date|None; dimensions:dict[str,str]
@dataclass
class XBRLFact:
    concept:str; context_id:str|None; unit_id:str|None; decimals:str|None; raw_value:str|None; numeric_value:Decimal|None; nil:bool; scale:int|None; source_xpath:str; attributes:dict[str,Any]
@dataclass
class XBRLResult:
    kind:str; contexts:dict[str,XBRLContext]; units:dict[str,str]; facts:list[XBRLFact]

def _local(tag:str)->str:return tag.rsplit("}",1)[-1]
def _date(text:str|None)->date|None:
    if not text:return None
    try:return date.fromisoformat(text[:10])
    except ValueError:return None

def parse_xbrl(path:Path)->XBRLResult:
    tree=ET.parse(path); root=tree.getroot(); kind="INLINE_XBRL" if any(_local(e.tag) in {"nonFraction","nonNumeric"} for e in root.iter()) else "XBRL"
    contexts={}; units={}
    for e in root.iter():
        name=_local(e.tag)
        if name=="context":
            cid=e.attrib.get("id",""); identifier=next((x.text for x in e.iter() if _local(x.tag)=="identifier"),None)
            instant=next((_date(x.text) for x in e.iter() if _local(x.tag)=="instant"),None)
            start=next((_date(x.text) for x in e.iter() if _local(x.tag)=="startDate"),None)
            end=next((_date(x.text) for x in e.iter() if _local(x.tag)=="endDate"),None)
            dims={x.attrib.get("dimension",""):x.text or "" for x in e.iter() if _local(x.tag) in {"explicitMember","typedMember"}}
            contexts[cid]=XBRLContext(cid,identifier,instant,start,end,dims)
        elif name=="unit":
            uid=e.attrib.get("id",""); units[uid]=" ".join((x.text or "") for x in e.iter() if _local(x.tag) in {"measure","unitNumerator","unitDenominator"})
    facts=[]
    structural={"xbrl","context","entity","identifier","period","instant","startDate","endDate","scenario","segment","unit","measure","schemaRef","footnoteLink"}
    for e in root.iter():
        name=_local(e.tag)
        if name in structural:continue
        context=e.attrib.get("contextRef")
        if kind=="INLINE_XBRL" and name in {"nonFraction","nonNumeric"}: concept=e.attrib.get("name",name)
        elif context: concept=e.tag
        else: continue
        raw="".join(e.itertext()).strip() or None
        nil=e.attrib.get("{http://www.w3.org/2001/XMLSchema-instance}nil","").lower()=="true"
        scale=int(e.attrib["scale"]) if re.fullmatch(r"-?\d+",e.attrib.get("scale","")) else None
        numeric=None
        if raw is not None and not nil:
            cleaned=raw.replace(",","").replace("₹","").strip()
            if cleaned.startswith("(") and cleaned.endswith(")"):cleaned="-"+cleaned[1:-1]
            try:
                numeric=Decimal(cleaned)
                if scale:numeric*=Decimal(10)**scale
                if e.attrib.get("sign")=="-":numeric=-abs(numeric)
            except InvalidOperation:pass
        facts.append(XBRLFact(concept,context,e.attrib.get("unitRef"),e.attrib.get("decimals"),raw,numeric,nil,scale,concept,dict(e.attrib)))
    return XBRLResult(kind,contexts,units,facts)
