"""Adapter contract: the operate section.

* ``signals``: per signal class, the read call, its cost cap, and how the reading maps
  into a SIG record (the monitor engine does the mapping; adapters return metadata).
* ``actions``: per catalogue action type, the call, action class, preconditions,
  verification query and reverse action.
* ``limits``: timeouts, row caps, retry caps.

No vendor name appears outside an adapter.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass(frozen=True)
class SignalSpec:
    signal_class: str
    read: Callable[[str, dict], dict]
    cost_cap: float = 0.0


@dataclass(frozen=True)
class ActionSpec:
    action_type: str
    action_class: str
    call: Callable[[str, dict], dict]
    verification: str
    reverse: Optional[str] = None
    preconditions: tuple[str, ...] = ()


@dataclass
class OperateAdapter(ABC):
    name: str = "adapter"
    limits: dict[str, Any] = field(default_factory=lambda: {
        "timeout_s": 60, "row_cap": 0, "retry_cap_per_object_per_day": 2})

    @property
    @abstractmethod
    def signals(self) -> dict[str, SignalSpec]: ...

    @property
    @abstractmethod
    def actions(self) -> dict[str, ActionSpec]: ...

    # Facts the executor hook re-reads at execution time (never trusted from the agent).
    @abstractmethod
    def latest_window(self, product: str, pipeline: str) -> str: ...

    @abstractmethod
    def run_logs(self, product: str, pipeline: str) -> list[str]: ...

    def read(self, signal_class: str, product: str, monitor: dict) -> dict:
        spec = self.signals.get(signal_class)
        if spec is None:
            raise KeyError(f"adapter {self.name} has no signal read for class {signal_class!r}")
        return spec.read(product, monitor)

    def run_action(self, action_type: str, product: str, params: dict) -> dict:
        spec = self.actions.get(action_type)
        if spec is None:
            raise KeyError(f"adapter {self.name} has no action {action_type!r}")
        return spec.call(product, params)
