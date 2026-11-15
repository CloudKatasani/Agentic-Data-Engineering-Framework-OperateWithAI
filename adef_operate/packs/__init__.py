"""Synthetic Domain Packs and golden products for the Demo Simulator."""

from __future__ import annotations

from importlib import resources
from typing import Any

import yaml

PACKS = ("utilities", "telco")


def load_pack(name: str) -> dict[str, Any]:
    if name not in PACKS:
        raise KeyError(f"unknown pack {name!r}; known: {PACKS}")
    return yaml.safe_load(resources.files(__package__).joinpath(f"{name}.yaml").read_text("utf-8"))


def handover_pack(name: str) -> dict[str, Any]:
    return load_pack(name)["handover_pack"]
