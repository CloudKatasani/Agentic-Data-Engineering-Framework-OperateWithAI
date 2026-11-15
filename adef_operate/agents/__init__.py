"""Operate agents. Each is the runtime of one ``skills/adef-ops-*/SKILL.md`` contract and
writes only inside its allowlist. Day 6 agents (Commander, Onboarding, Sentinel) are
rule-based: severity, grouping and onboarding are decided by rule in the spec. Agents
that need model judgement (Diagnosis onward) add a model seam on top of the same tools.
"""

from .commander import CommanderAgent
from .onboarding import OnboardingAgent
from .sentinel import SentinelAgent

__all__ = ["CommanderAgent", "OnboardingAgent", "SentinelAgent"]
