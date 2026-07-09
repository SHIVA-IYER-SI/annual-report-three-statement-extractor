"""Verified blueprint resource access. Resources are packaged with Block 20A."""
from __future__ import annotations
import hashlib, json
from functools import lru_cache
from pathlib import Path
from typing import Any

RESOURCE_DIR=Path(__file__).with_name("resources")
EXPECTED_COUNTS={"income_statement":159,"balance_sheet":193,"cash_flow":85,"income_validation":16,"balance_validation":27,"cash_flow_validation":15}

@lru_cache(maxsize=None)
def load_json(name:str)->Any:
    return json.loads((RESOURCE_DIR/name).read_text(encoding="utf-8"))

def line_item_universes()->dict[str,list[dict[str,Any]]]:
    return {
      "INCOME_STATEMENT":load_json("income_statement_line_item_universe.json"),
      "BALANCE_SHEET":load_json("balance_sheet_line_item_universe.json"),
      "CASH_FLOW_STATEMENT":load_json("cash_flow_line_item_universe.json"),
    }

def validation_universes()->dict[str,list[dict[str,Any]]]:
    return {
      "INCOME_STATEMENT":load_json("income_statement_validation_checks.json"),
      "BALANCE_SHEET":load_json("balance_sheet_validation_checks.json"),
      "CASH_FLOW_STATEMENT":load_json("cash_flow_validation_checks.json"),
    }

def verify_runtime_resources()->list[str]:
    manifest=load_json("blueprint_resource_manifest.json")
    errors=[]
    for item in manifest["resources"]:
        path=RESOURCE_DIR/Path(item["runtime_path"]).name
        if not path.exists(): errors.append(f"missing:{path.name}"); continue
        digest=hashlib.sha256(path.read_bytes()).hexdigest()
        if digest!=item["sha256"]: errors.append(f"sha256:{path.name}")
        if path.stat().st_size!=item["bytes"]: errors.append(f"bytes:{path.name}")
    universes=line_item_universes(); checks=validation_universes()
    actual={"income_statement":len(universes["INCOME_STATEMENT"]),"balance_sheet":len(universes["BALANCE_SHEET"]),"cash_flow":len(universes["CASH_FLOW_STATEMENT"]),"income_validation":len(checks["INCOME_STATEMENT"]),"balance_validation":len(checks["BALANCE_SHEET"]),"cash_flow_validation":len(checks["CASH_FLOW_STATEMENT"])}
    for key,expected in EXPECTED_COUNTS.items():
        if actual[key]!=expected: errors.append(f"count:{key}:{actual[key]}!={expected}")
    return errors
