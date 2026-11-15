"""Ops workspace creator and per-identity write allowlists (backlog O1-2, spec section 10).

Every write into ``$ADEF_HOME/ops/<dp_id>/`` goes through :meth:`Workspace.write` or
:meth:`Workspace.append`, which check the writing identity against its allowlist. Each
artifact folder has exactly one writer.
"""

from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from ..core.chainlog import ChainLog
from ..core.errors import WriteNotAllowed

FOLDERS = [
    "control/approval-requests", "control/autonomy-proposals",
    "service/tuning-proposals",
    "signals/groups", "signals/monitor-results",
    "cases", "change-requests", "problems", "finops/findings",
    "governance/drift", "knowledge/runbook-changes", "evals/replays", "health", "shadow",
]

# Agent identities (one per skill) and code identities.
AGENTS = ("commander", "onboarding", "sentinel", "diagnosis", "remediation",
          "finops", "governance", "knowledge")
CODE = ("collector", "monitor_engine", "case_store", "executor", "approvals", "registry",
        "escalations", "council", "shadow", "notifier")

ALLOW: dict[str, list[str]] = {
    "commander": ["cases/*/case.yaml", "cases/*/notice-draft.md", "health/**",
                  "control/approval-requests/**", "control/service.yaml",
                  "control/handover-pack.yaml", "control/start-here.yaml"],
    "onboarding": ["service/**"],
    "sentinel": ["signals/groups/**", "signals/noise-report-*.md", "service/tuning-proposals/**"],
    "diagnosis": ["cases/*/impact.md", "cases/*/evidence/**", "cases/*/diagnosis.md",
                  "cases/*/diagnosis.yaml"],
    "remediation": ["cases/*/options.md", "cases/*/actions/**", "cases/*/verification.md",
                    "change-requests/**"],
    "finops": ["finops/**"],
    "governance": ["governance/**"],
    "knowledge": ["cases/*/post-incident.md", "problems/**", "knowledge/**", "evals/**",
                  "control/autonomy-proposals/**"],
    # Code identities.
    "collector": ["signals/*.jsonl"],
    "monitor_engine": ["signals/monitor-results/**"],
    "case_store": ["cases/*/case.yaml"],
    "executor": ["control/action-log.jsonl", "control/executor-counters.yaml"],
    "approvals": ["control/approvals.jsonl", "control/decision-log.md",
                  "control/approval-requests/**"],
    "registry": ["control/registry.yaml", "control/budget-ledger.jsonl"],
    "escalations": ["control/escalations.jsonl"],
    "council": ["cases/*/council.yaml"],
    "shadow": ["shadow/**"],
    "notifier": ["control/notifications.jsonl"],
}

DENY: dict[str, list[str]] = {
    "onboarding": ["service/tuning-proposals/**"],
}


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    out = ""
    i = 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile(out + r"\Z")


def may_write(identity: str, rel_path: str) -> bool:
    rel = str(PurePosixPath(rel_path))
    if any(_glob_to_regex(p).match(rel) for p in DENY.get(identity, [])):
        return False
    return any(_glob_to_regex(p).match(rel) for p in ALLOW.get(identity, []))


def adef_home() -> Path:
    return Path(os.environ.get("ADEF_HOME", Path.home() / ".adef"))


class Workspace:
    """One product's ops workspace."""

    def __init__(self, dp_id: str, home: str | Path | None = None):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", dp_id):
            raise ValueError(f"invalid dp_id {dp_id!r}")
        self.dp_id = dp_id
        self.root = Path(home or adef_home()) / "ops" / dp_id

    # -- creation -----------------------------------------------------------------
    def create(self) -> "Workspace":
        for folder in FOLDERS:
            (self.root / folder).mkdir(parents=True, exist_ok=True)
        return self

    def exists(self) -> bool:
        return (self.root / "control").is_dir()

    # -- paths ----------------------------------------------------------------------
    def _resolve(self, rel_path: str) -> Path:
        p = PurePosixPath(rel_path)
        if p.is_absolute() or ".." in p.parts:
            raise WriteNotAllowed(f"path {rel_path!r} escapes the workspace")
        return self.root / p

    def path(self, rel_path: str) -> Path:
        return self._resolve(rel_path)

    def _check(self, identity: str, rel_path: str) -> Path:
        target = self._resolve(rel_path)
        if not may_write(identity, rel_path):
            raise WriteNotAllowed(
                f"identity {identity!r} may not write {rel_path!r} in ops/{self.dp_id}")
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    # -- writes ---------------------------------------------------------------------
    def write(self, identity: str, rel_path: str, content: str) -> Path:
        target = self._check(identity, rel_path)
        target.write_text(content, encoding="utf-8")
        return target

    def write_yaml(self, identity: str, rel_path: str, data: Any) -> Path:
        return self.write(identity, rel_path, yaml.safe_dump(data, sort_keys=False))

    def append(self, identity: str, rel_path: str, line: str) -> Path:
        target = self._check(identity, rel_path)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(line.rstrip("\n") + "\n")
        return target

    def chainlog(self, identity: str, rel_path: str) -> ChainLog:
        """A hash-chained log the identity may append to."""
        return ChainLog(self._check(identity, rel_path))

    # -- reads ----------------------------------------------------------------------
    def read(self, rel_path: str) -> str:
        return self._resolve(rel_path).read_text(encoding="utf-8")

    def read_yaml(self, rel_path: str, default: Any = None) -> Any:
        p = self._resolve(rel_path)
        if not p.exists():
            return default
        return yaml.safe_load(p.read_text(encoding="utf-8"))

    def has(self, rel_path: str) -> bool:
        return self._resolve(rel_path).exists()
