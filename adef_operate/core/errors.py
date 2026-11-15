"""Errors raised by enforcement code. Each carries a specific, human-readable reason."""


class AdefError(Exception):
    """Base class for every enforcement error."""

    def __init__(self, message: str, *, escalate: bool = False):
        super().__init__(message)
        self.escalate = escalate


class ValidationFailed(AdefError):
    """A record failed schema validation."""


class WriteNotAllowed(AdefError):
    """An identity tried to write outside its allowlist."""


class IllegalTransition(AdefError):
    """The case store rejected a lifecycle transition."""


class GuardViolation(AdefError):
    """A close or recover guard (or another case-store guard) was not satisfied."""


class ExecutorBlocked(AdefError):
    """The action executor refused an ACT record."""


class ApprovalError(AdefError):
    """An approval was missing, expired, mismatched, or not given by a named human."""


class BudgetExceeded(AdefError):
    """A per-product daily budget was breached."""
