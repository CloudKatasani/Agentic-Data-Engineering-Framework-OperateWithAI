"""Local stand-in for the shared ``adef-core`` package.

``adef-core`` and ``SUITE_SPEC.md`` are not in this repository yet. This module holds
the minimum the Operate runtime needs (hashing, hash-chained logs, autonomy levels and
action classes, errors). When adef-core is available, replace these imports with it.
"""

from .autonomy import ActionClass, Level
from .chainlog import ChainLog
from .errors import (
    AdefError,
    ApprovalError,
    BudgetExceeded,
    ExecutorBlocked,
    IllegalTransition,
    GuardViolation,
    ValidationFailed,
    WriteNotAllowed,
)
from .hashing import canonical_hash, file_hash, text_hash

__all__ = [
    "ActionClass", "Level", "ChainLog", "AdefError", "ApprovalError", "BudgetExceeded",
    "ExecutorBlocked", "IllegalTransition", "GuardViolation", "ValidationFailed",
    "WriteNotAllowed", "canonical_hash", "file_hash", "text_hash",
]
