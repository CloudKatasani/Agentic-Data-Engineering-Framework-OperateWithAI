"""Stack binding for Operate: Build-With-AI Stack Profiles, Operate capability slots,
logical-to-physical naming, per-tool telemetry translators, and the stack adapter.

Agents and records speak logical names (``bronze.meter_interval_reads``) and slot names
(``orchestration``); only this package knows tools. Rebinding to another profile changes
no case record, monitoring specification or runbook (OPERATE_SPEC section 9, O3-4).
"""

from .adapter import StackAdapter
from .native import NativePlatform
from .profiles import OPERATE_SLOTS, list_profiles, load_profile, operate_binding, validate_operate

__all__ = ["StackAdapter", "NativePlatform", "OPERATE_SLOTS", "list_profiles", "load_profile",
           "operate_binding", "validate_operate"]
