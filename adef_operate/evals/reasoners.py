"""Compare diagnosis reasoners on the fault library (offline, Demo Simulator).

The rule reasoner decides; the candidate runs as a challenger on the same evidence. The
report gives each one's cause-class accuracy against the library labels, agreement, how
often the candidate fell back, and the model cost. A model candidate spends real money:
the CLI asks for ``--confirm-spend`` before running it.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Callable, Optional

from ..agents.diagnosis import DiagnosisAgent
from ..agents.reasoning import RuleReasoner
from ..connectors.simulator import FAULTS
from ..simulation import Scenario

# Rough per-call size for an estimate shown before spending: ~4k input, ~3k output tokens.
EST_TOKENS_IN, EST_TOKENS_OUT = 4_000, 3_000


def estimate_usd(model: str, runs: int) -> float:
    from ..agents.model_reasoner import PRICES, DEFAULT_MODEL
    pin, pout, _, _ = PRICES.get(model, PRICES[DEFAULT_MODEL])
    return round(runs * (EST_TOKENS_IN * pin + EST_TOKENS_OUT * pout) / 1_000_000, 2)


def library_runs() -> int:
    return sum(len(f.packs) for f in FAULTS.values())


def compare_reasoners(candidate_factory: Callable[[], Any], home: Optional[str | Path] = None,
                      profile: Optional[str] = None) -> dict:
    base = Path(home or tempfile.mkdtemp(prefix="adef-reasoners-"))
    rows = []
    for fid, fault in FAULTS.items():
        for pack in fault.packs:
            sc = Scenario(base / f"{fid}-{pack}", pack, profile)
            sc.sim.inject(fid, pack)
            out = sc.tick()
            agent = DiagnosisAgent(sc.ws, sc.adapter, reasoner=RuleReasoner(), challenger=candidate_factory())
            o = out["outcomes"][0]
            r = agent.diagnose(o["case"]) if o.get("case") else agent.diagnose_group(out["groups"][0])
            ch = r.challenger or {}
            rows.append({"fault": fid, "pack": pack, "label": fault.cause_class, "rule": r.cause_class,
                         "candidate": ch.get("cause_class"), "candidate_reasoner": ch.get("reasoner"),
                         "fell_back": "unavailable" in (ch.get("reasoner") or "") or bool(ch.get("note")),
                         "cost_usd": ch.get("cost_usd", 0.0)})
    n = len(rows)
    acc = lambda k: round(sum(r[k] == r["label"] for r in rows) / n, 3)
    return {"runs": n, "rule_accuracy": acc("rule"), "candidate_accuracy": acc("candidate"),
            "agreement": round(sum(r["rule"] == r["candidate"] for r in rows) / n, 3),
            "candidate_fallbacks": sum(r["fell_back"] for r in rows),
            "model_cost_usd": round(sum(r["cost_usd"] for r in rows), 4), "rows": rows}


def render(res: dict, candidate: str) -> str:
    lines = [f"# Reasoner comparison - rule-reasoner-v1 against {candidate}", "",
             "Same evidence for both; the rule reasoner decides and the candidate is recorded as a challenger.", "",
             "| Measure | Value |", "|---|---|",
             f"| Runs (fault and pack pairs) | {res['runs']} |", f"| Rule reasoner accuracy | {res['rule_accuracy']} |",
             f"| Candidate accuracy | {res['candidate_accuracy']} |", f"| Agreement | {res['agreement']} |",
             f"| Candidate fell back to the rule reasoner | {res['candidate_fallbacks']} |",
             f"| Model cost | ${res['model_cost_usd']:.2f} |", "",
             "The library was written with the rule reasoner, so it favours it. Decide on a switch from "
             "shadow-mode results against people's conclusions, not from this table alone.", "",
             "| Fault | Pack | Label | Rule | Candidate |", "|---|---|---|---|---|",
             *[f"| {r['fault']} | {r['pack']} | {r['label']} | {r['rule']} | {r['candidate']} |" for r in res["rows"]], ""]
    return "\n".join(lines)
