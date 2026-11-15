"""Client isolation: artifacts, logs and learned knowledge never cross client boundaries.

Products of the same client may reuse each other's operational artifacts, but only as a
draft that goes through the receiving product's own gate (OG1 for service files).
"""

from __future__ import annotations

from ..core.errors import GuardViolation
from .workspace import Workspace

RULE = ("Client isolation (suite rule): runbooks, monitoring specifications, cases, evidence and "
        "evaluation replays of one client are never read into, copied to, or used for another client")


def client_of(ws: Workspace) -> str:
    return (ws.read_yaml("control/service.yaml") or {}).get("client_id", "default")


def check_same_client(src: Workspace, dst: Workspace) -> None:
    a, b = client_of(src), client_of(dst)
    if a != b:
        raise GuardViolation(f"refused: {src.dp_id} belongs to {a}, {dst.dp_id} to {b}. {RULE}")


def read_for(dst: Workspace, src: Workspace, rel_path: str) -> str:
    """Read another product's artifact on behalf of ``dst`` (same client only)."""
    check_same_client(src, dst)
    return src.read(rel_path)
