"""Stack Profiles (the Build-With-AI format) and Operate's capability slots.

Operate's slots (OPERATE_SPEC section 9) are bound through the same Stack Profile Build
uses, so one profile serves both products:

| Operate slot          | Build slot(s) it reads                                   |
|-----------------------|----------------------------------------------------------|
| orchestration         | orchestration                                            |
| data_platform         | table_format + lake_catalog (bronze, silver); compute_engine (gold) |
| data_quality          | data_quality                                             |
| cost_billing          | compute_engine (platform billing) on cloud               |
| access_audit          | compute_engine (grants, masking, access history)         |
| catalog (tags, retention) | catalog_governance                                   |
| lineage               | lineage                                                  |
| incident_ticketing    | itsm (optional)                                          |
| paging_chat           | approval_channel (optional)                              |
| observability_tool    | observability (optional)                                 |
"""

from __future__ import annotations

from importlib import resources
from typing import Any

import yaml

OPERATE_SLOTS: dict[str, dict[str, Any]] = {
    "orchestration": {"build": ["orchestration"], "required": True},
    "data_platform": {"build": ["table_format", "lake_catalog", "compute_engine"], "required": True},
    "data_quality": {"build": ["data_quality"], "required": True},
    "cost_billing": {"build": ["compute_engine", "cloud"], "required": True},
    "access_audit": {"build": ["compute_engine"], "required": True},
    "catalog": {"build": ["catalog_governance"], "required": True},
    "lineage": {"build": ["lineage"], "required": True},
    "incident_ticketing": {"build": ["itsm"], "required": False},
    "paging_chat": {"build": ["approval_channel"], "required": False},
    "observability_tool": {"build": ["observability"], "required": False},
}


def list_profiles() -> list[str]:
    return sorted(p.name[:-5] for p in resources.files(__package__).joinpath("profiles").iterdir()
                  if p.name.endswith(".yaml"))


def load_profile(profile_id: str) -> dict:
    path = resources.files(__package__).joinpath("profiles", f"{profile_id}.yaml")
    if not path.is_file():
        raise KeyError(f"unknown Stack Profile {profile_id!r}; known: {list_profiles()}")
    return yaml.safe_load(path.read_text("utf-8"))


def tool(profile: dict, slot: str) -> str:
    return ((profile.get("slots") or {}).get(slot) or {}).get("tool", "none")


def operate_binding(profile: dict) -> dict[str, dict[str, str]]:
    return {s: {b: tool(profile, b) for b in spec["build"]} for s, spec in OPERATE_SLOTS.items()}


def validate_operate(profile: dict) -> dict[str, list[str]]:
    """Operate's receiver check on a Stack Profile: required slots bound, translator present."""
    from . import translators as T
    blocking, warnings = [], []
    b = operate_binding(profile)
    checks = [("orchestration", "orchestration", T.ORCHESTRATION), ("data_platform", "table_format", T.TABLE_FORMATS),
              ("data_platform", "compute_engine", T.ENGINES), ("data_quality", "data_quality", T.QUALITY),
              ("catalog", "catalog_governance", T.CATALOGS)]
    for oslot, bslot, registry in checks:
        t = b[oslot][bslot]
        if t in ("none", None):
            blocking.append(f"Operate slot `{oslot}` needs Build slot `{bslot}` bound (found none)")
        elif t not in registry:
            blocking.append(f"No Operate telemetry adapter for {bslot}={t}; add one before onboarding")
    if b["lineage"]["lineage"] == "none":
        blocking.append("Operate slot `lineage` is required for impact analysis (bind catalog-native or openlineage)")
    for oslot in ("incident_ticketing", "paging_chat", "observability_tool"):
        if all(v == "none" for v in b[oslot].values()):
            warnings.append(f"optional slot `{oslot}` not bound; the console is used instead")
    return {"blocking": blocking, "warnings": warnings}
