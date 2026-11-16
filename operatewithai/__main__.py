"""Command line: ``python -m operatewithai serve`` starts the API and console;
``night`` runs a scenario night in the terminal; ``operating-model`` prints the role document."""

from __future__ import annotations

import argparse

from .agents.team import operating_model
from .env import Environment
from .service import Service
from .sim.scenarios import PRESETS, SCENARIOS


def main() -> None:
    p = argparse.ArgumentParser(prog="operatewithai", description="AI agents and people running a data platform managed service")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="Start the API and web console")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8090)
    s.add_argument("--scenarios", default="typical", help="Preset (quiet, typical, everything) or a comma list such as S01,S06")
    s.add_argument("--until", default="", help="Run the night to this time before serving (HH:MM); default: stop at the first detection")
    s.add_argument("--fast", action="store_true", help="Agents hand off to each other without a person confirming each step")
    s.add_argument("--autotick", type=float, default=0, help="Advance the simulated clock 5 minutes every N seconds")
    s.add_argument("--client", default="demo", help="Client estate: demo, lakehouse, awsdw, gcp")
    b = sub.add_parser("benchmark", help="Inject every scenario of every client estate and grade triage and diagnosis against the injected cause")
    n = sub.add_parser("night", help="Run a night without people and print what the agents did and what waits on whom")
    n.add_argument("--scenarios", default="typical")
    n.add_argument("--until", default="08:00")
    n.add_argument("--client", default="demo")
    sub.add_parser("scenarios", help="List the scenario library")
    sub.add_parser("operating-model", help="Print the operating model document")
    a = p.parse_args()

    svc = Service(getattr(a, 'client', 'demo')) if a.cmd in ('serve', 'night') else None

    def pick(text: str) -> list[str]:
        presets = svc.library()["presets"]
        return presets[text]["scenarios"] if text in presets else [x.strip() for x in text.split(",") if x.strip()]

    if a.cmd == "operating-model":
        print(operating_model(Environment.load()), end="")
    elif a.cmd == "scenarios":
        for c in Environment.clients():
            print(f"\n{c['id']}: {c['name']}\n  {c['stack']}")
            for sc in Service(c["id"]).library()["scenarios"]:
                print(f"  {sc['id']}  {sc['title']}")
    elif a.cmd == "benchmark":
        from .benchmark import run
        raise SystemExit(0 if run() else 1)
    elif a.cmd == "night":
        svc.restart(pick(a.scenarios))
        svc.run_until(a.until)
        for t in svc.ops.tickets():
            print(f"{t['id']} {t['priority']} {t['state']:<11} {t['title']}")
            for x in t["timeline"]:
                print(f"    {x['at'][11:16]} {x['actor']}: {x['text']}")
        print("\nWaiting on people:")
        for note in svc.ops.notes():
            print(f"    {note['escalated_to'] or note['to']}: {note['title']}")
        print("\nBusiness services:")
        for sv in svc.ops.projection["services"].values():
            print(f"    {sv['name']}: {sv['status']} (now {sv['est']}, promised {sv['due']})")
    else:
        import uvicorn

        from .api.app import create_app
        svc.restart(pick(a.scenarios), guided=not a.fast)
        if a.until:
            svc.run_until(a.until)
        else:
            svc.run_to_first_issue()
        uvicorn.run(create_app(svc, autotick=a.autotick or None), host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
