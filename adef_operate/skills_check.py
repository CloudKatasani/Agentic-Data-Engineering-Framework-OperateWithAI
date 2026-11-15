"""Validate the ``skills/*/SKILL.md`` contracts (backlog O1-1: skills pass validation)."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REQUIRED_SECTIONS = ["Mission", "Use when", "Do not use for", "Inputs", "Tasks", "Decision rules",
                     "Do not", "Escalate to the human when", "Enterprise controls",
                     "Self-review before handover", "Outputs", "Definition of done",
                     "Worked example", "Common failure modes"]
REQUIRED_META = ["name", "description", "license", "compatibility", "metadata"]
EXPECTED = ["adef-ops-commander", "adef-ops-service-onboarding", "adef-ops-signal-sentinel",
            "adef-ops-incident-diagnosis", "adef-ops-remediation", "adef-ops-finops-performance",
            "adef-ops-governance-compliance", "adef-ops-problem-knowledge"]


def validate_skill(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        return [f"{path}: no YAML front matter"]
    meta = yaml.safe_load(m.group(1)) or {}
    problems = [f"{path}: front matter lacks {k}" for k in REQUIRED_META if k not in meta]
    if meta.get("name") != path.parent.name:
        problems.append(f"{path}: name {meta.get('name')!r} does not match folder {path.parent.name!r}")
    if len(str(meta.get("description", ""))) > 1024:
        problems.append(f"{path}: description longer than 1024 characters")
    headings = re.findall(r"^## (.+?)\s*$", text, re.M)
    for s in REQUIRED_SECTIONS:
        if s not in headings:
            problems.append(f"{path}: missing section '## {s}'")
    order = [h for h in headings if h in REQUIRED_SECTIONS]
    if order != [s for s in REQUIRED_SECTIONS if s in order]:
        problems.append(f"{path}: sections out of order: {order}")
    return problems


def validate_all(root: str | Path = "skills") -> list[str]:
    root = Path(root)
    problems = [f"missing skill {n}" for n in EXPECTED if not (root / n / "SKILL.md").exists()]
    for p in sorted(root.glob("*/SKILL.md")):
        problems += validate_skill(p)
    return problems


if __name__ == "__main__":
    import sys
    errs = validate_all(sys.argv[1] if len(sys.argv) > 1 else "skills")
    print("\n".join(errs) or "all skills valid")
    sys.exit(1 if errs else 0)
