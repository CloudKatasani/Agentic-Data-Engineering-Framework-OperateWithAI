"""Stack adapters. Each exposes an *operate section* (spec section 9)."""

from .base import ActionSpec, OperateAdapter, SignalSpec
from .simulator import SimulatorAdapter

__all__ = ["ActionSpec", "OperateAdapter", "SignalSpec", "SimulatorAdapter"]
