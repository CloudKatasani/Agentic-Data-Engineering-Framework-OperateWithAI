"""Append-only, hash-chained JSONL log (decisions, approvals, actions).

Each entry stores the hash of the previous entry, so any edit or deletion is detected
by :meth:`ChainLog.verify`.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .hashing import canonical_hash

GENESIS = "sha256:" + "0" * 64


class ChainLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)

    def entries(self) -> Iterator[dict[str, Any]]:
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    yield json.loads(line)

    def _last_hash(self) -> str:
        last = GENESIS
        for entry in self.entries():
            last = entry["hash"]
        return last

    def append(self, record: dict[str, Any], *, at: datetime | None = None) -> dict[str, Any]:
        entry = {
            "at": (at or datetime.now(timezone.utc)).isoformat(),
            "prev": self._last_hash(),
            "record": record,
        }
        entry["hash"] = canonical_hash({k: entry[k] for k in ("at", "prev", "record")})
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True, default=str) + "\n")
        return entry

    def verify(self) -> bool:
        prev = GENESIS
        for entry in self.entries():
            if entry["prev"] != prev:
                return False
            if canonical_hash({k: entry[k] for k in ("at", "prev", "record")}) != entry["hash"]:
                return False
            prev = entry["hash"]
        return True
