"""Stable hashing for artifacts, records and approved text."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def text_hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_hash(obj: Any) -> str:
    """Hash of a JSON-serialisable object with sorted keys (order-independent)."""
    return text_hash(json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str))


def file_hash(path: str | Path) -> str:
    return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()
