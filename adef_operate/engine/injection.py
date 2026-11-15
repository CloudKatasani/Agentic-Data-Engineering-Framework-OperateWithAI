"""Treat log, error and ticket text as data: wrap it and flag instruction-like content."""

from __future__ import annotations

import re
from dataclasses import dataclass

PATTERNS = [
    r"ignore (all |any )?(previous|prior) instructions",
    r"\b(disable|mute|turn off|silence)\b.{0,40}\b(monitor|monitoring|alert|alerts)\b",
    r"\bgrant\b.{0,30}\b(admin|owner|superuser|all privileges)\b",
    r"\b(note|message|instruction)s? (to|for) (the )?(ai|agent|assistant|model)\b",
    r"\brevoke\b.{0,30}\baccess\b",
    r"\bdrop (table|schema|database)\b",
]
_RX = [re.compile(p, re.IGNORECASE) for p in PATTERNS]


@dataclass(frozen=True)
class ScanResult:
    suspicious: bool
    matches: tuple[str, ...]
    wrapped: str


def scan(text: str, *, source: str) -> ScanResult:
    matches = tuple(m.group(0) for rx in _RX for m in [rx.search(text)] if m)
    wrapped = f'<untrusted source="{source}">\n{text}\n</untrusted>'
    return ScanResult(bool(matches), matches, wrapped)
