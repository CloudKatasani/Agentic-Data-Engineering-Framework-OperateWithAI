"""Autonomy levels and action classes.

SUITE_SPEC.md section 2 is not in this repository; the definitions below are the
working assumption used by the hook and must be confirmed against it.

Levels (per product and action type):
  L0  stopped: agents of the product may not act or request anything.
  L1  recommend: the agent proposes; the executor refuses; people decide and execute.
  L2  execute with approval: the executor runs the action only with a valid,
      matching per-action gate approval.
  L3  execute within pre-approval: the executor runs the action when the hook's
      preconditions pass and OG1 standing approval is valid.

Action classes:
  A1  read-only.
  A2  reversible operational action on production (catalogue, pre-approved).
  A3  high-impact or hard-to-reverse; always needs a per-action gate.
"""

from __future__ import annotations

from enum import Enum, IntEnum


class Level(IntEnum):
    L0 = 0
    L1 = 1
    L2 = 2
    L3 = 3

    @classmethod
    def parse(cls, value: "str | int | Level") -> "Level":
        if isinstance(value, Level):
            return value
        if isinstance(value, int):
            return cls(value)
        return cls[str(value).upper()]


class ActionClass(str, Enum):
    A1 = "A1"
    A2 = "A2"
    A3 = "A3"
