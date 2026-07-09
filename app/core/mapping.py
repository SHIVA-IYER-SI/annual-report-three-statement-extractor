"""Canonical mapping candidate generation with context and ambiguity controls."""
from __future__ import annotations
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any,Iterable
from .parsing import normalize_label
from .resources import line_item_universes

MANDATORY_AMBIGUITIES=(
 ("training",("revenue","expense")),("interest accrued",("asset","liability")),("security deposit",("asset","liability")),
 ("current maturit",("borrowings","maturity")),("lease liabilit",("borrowings","other financial liabilities")),
 ("unbilled revenue",("receivable","contract asset")),("deferred revenue",("contract liability","other liability")),
 ("cash credit",("borrowing","cash equivalent")),("dividend",("declared","paid")),("joint venture",("share of profit","dividend received")),
)

@dataclass(frozen=True)
class Candidate:
    canonical_key:str; score:float; confidence:float; reasons:tuple[str,...]; display_label:str; parent_key:str|None

class CanonicalMapper:
    def __init__(self,extra_rules:Iterable[dict[str,Any]]=()):
        self.universes=line_item_universes(); self.extra=list(extra_rules)
        self.by_statement={k:self._prepare(v) for k,v in self.universes.items()}
    def _prepare(self,rows):
        out=[]
        for row in rows:
            labels=[row["display_label"],*(row.get("common_synonyms") or [])]
            out.append((row,[normalize_label(x) for x in labels if x]))
        return out
    def candidates(self,statement_type:str,label:str,parent_context:str|None=None,note_context:str|None=None,classification:dict[str,Any]|None=None,limit:int=5)->list[Candidate]:
        normalized=normalize_label(label); parent=normalize_label(parent_context or ""); context=normalize_label(" ".join([parent_context or "",note_context or "",str(classification or {})]))
        scored=[]
        for rule in self.extra:
            if rule.get("statement_type")!=statement_type:continue
            if normalize_label(rule.get("original_label",""))==normalized:
                if rule.get("parent_context") and normalize_label(rule["parent_context"]) not in context:continue
                scored.append(Candidate(rule["canonical_key"],0.995,0.995,("approved_context_rule",),rule.get("display_label",rule["canonical_key"]),rule.get("parent_key")))
        for row,labels in self.by_statement.get(statement_type,[]):
            exact=normalized in labels
            sim=max((SequenceMatcher(None,normalized,x).ratio() for x in labels),default=0)
            token_a=set(normalized.split()); token_b=set(" ".join(labels).split()); overlap=len(token_a&token_b)/max(1,len(token_a|token_b))
            score=0.7*sim+0.3*overlap; reasons=[]
            if exact:score=0.97;reasons.append("exact_or_synonym_match")
            elif sim>=0.86:reasons.append("strong_synonym_similarity")
            elif overlap>=0.65:reasons.append("accounting_label_overlap")
            if parent and row.get("parent_key"):
                parent_row=next((r for r,_ in self.by_statement[statement_type] if r["canonical_key"]==row["parent_key"]),None)
                if parent_row and normalize_label(parent_row["display_label"]) in context:score+=0.05;reasons.append("parent_context_match")
            # context cues, not standalone proof
            section=normalize_label(row.get("section","")+" "+row.get("notes","")+" "+row.get("preferred_source",""))
            if any(token in context for token in section.split() if len(token)>4):score+=0.015
            score=min(score,0.99)
            if score>=0.42:scored.append(Candidate(row["canonical_key"],score,score,tuple(reasons or ["weak_label_match"]),row["display_label"],row.get("parent_key") or None))
        scored.sort(key=lambda c:(-c.score,c.canonical_key))
        return scored[:limit]
    def needs_mandatory_review(self,label:str,candidates:list[Candidate],context:str="")->bool:
        norm=normalize_label(label); ctx=normalize_label(context)
        for needle,oppositions in MANDATORY_AMBIGUITIES:
            if needle in norm and not any(cue in ctx for cue in oppositions): return True
        if len(candidates)>=2 and candidates[0].score-candidates[1].score<0.08:return True
        return not candidates or candidates[0].confidence<0.72
