"""Fault-injection benchmark (the idea is from Aurora Lab): inject each known fault into each
client estate, give the agents only what the tools report, and grade them against the injected
cause. It checks three things per scenario: one incident for one cause, the right known error,
and the incident routed to a tower that exists. Run it after changing connectors, the known
error database or the correlation rules: ``python -m operatewithai benchmark``."""

from __future__ import annotations

from .env import Environment
from .service import Service
from .sim.scenarios import SCENARIOS

EXPECT = {"S01": "KE0001", "S02": "KE0002", "S03": "KE0003", "S04": "KE0004", "S05": "KE0005", "S06": "KE0006", "S07": "KE0007",
          "S08": None, "S09": "KE0008", "S10": "KE0009"}


def grade() -> list[dict]:
    rows = []
    for c in Environment.clients():
        svc = Service(c["id"])
        for sc in svc.library()["scenarios"]:
            expect = sc.get("expect_ke", EXPECT.get(sc["id"]))
            svc.restart([sc["id"]])
            svc.run_until("08:00")           # nobody answers: detection, correlation, diagnosis and routing only
            incs = svc.ops.tickets("incident")
            first = incs[0] if incs else {}
            alerts = sum(len(i["events"]) for i in incs)
            rows.append({"client": c["id"], "scenario": sc["id"], "title": sc["title"], "expected": expect or "unknown error", "incidents": len(incs),
                         "alerts": alerts, "diagnosed": first.get("ke") or ("unknown error" if incs else "nothing detected"),
                         "status": (first.get("diagnosis") or {}).get("status", ""), "tower": svc.env.tower_name(first["tower"]) if incs else "",
                         "ok": len(incs) == 1 and first.get("ke") == expect and bool(first.get("assignee"))})
    return rows


def run() -> bool:
    rows = grade()
    for r in rows:
        print(f"{'PASS' if r['ok'] else 'FAIL'}  {r['client']:<10} {r['scenario']}  {r['alerts']} alert(s) -> {r['incidents']} incident(s), "
              f"diagnosed {r['diagnosed']} ({r['status']}), expected {r['expected']}, routed to {r['tower']}")
    good = sum(r["ok"] for r in rows)
    print(f"\n{good} of {len(rows)} injected causes were isolated to one incident and diagnosed correctly.")
    return good == len(rows)
