"""Synthetic Domain Packs and golden products for the Demo Simulator."""

from __future__ import annotations

import copy
from functools import lru_cache
from importlib import resources
from typing import Any

import yaml

PACKS = ("utilities", "telco")


@lru_cache(maxsize=None)
def _parsed(name: str) -> dict[str, Any]:
    return yaml.safe_load(resources.files(__package__).joinpath(f"{name}.yaml").read_text("utf-8"))


def load_pack(name: str) -> dict[str, Any]:
    if name not in PACKS:
        raise KeyError(f"unknown pack {name!r}; known: {PACKS}")
    return copy.deepcopy(_parsed(name))


def handover_pack(name: str) -> dict[str, Any]:
    return load_pack(name)["handover_pack"]


def domain_pack(pack_id: str) -> dict[str, Any]:
    """The Domain Pack section of a pack, by its id (``utilities``, ``telco``)."""
    for name in PACKS:
        dp = load_pack(name)["domain_pack"]
        if dp["id"] == pack_id:
            return dp
    return {"id": pack_id, "severity_overrides": []}
