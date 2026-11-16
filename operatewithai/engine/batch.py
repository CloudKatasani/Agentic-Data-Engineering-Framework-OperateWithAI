"""Batch projection: when will each job finish and each business service be delivered,
given what the schedulers show now and the incidents that are open. This is the nightly
question a managed service is paid to answer early: "will the 07:00 dashboard be there?"."""

from __future__ import annotations

from datetime import datetime, timedelta

from ..env import hhmm

AT_RISK_MARGIN = 30   # minutes of slack below which a service is "at risk"
RETRY_MINUTES = 20


def fix_done(inc: dict) -> bool:
    steps = (inc.get("plan") or {}).get("steps", [])
    return bool(steps) and all(s["status"] in ("done", "skipped") for s in steps if s["kind"] != "recovery")


def project(ops) -> dict:
    env, bs = ops.env, ops.estate.batch_status()
    now: datetime = bs["now"]
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    incs = ops.tickets("incident", open_only=True)
    past = now - timedelta(minutes=1)
    memo: dict[str, datetime] = {}

    def fix_eta(node: str):
        best = None
        for inc in incs:
            if node in inc.get("root_nodes", []):
                typical = (ops.kb.get(inc.get("ke")) or {}).get("typical_fix_minutes", 45)
                t = now if fix_done(inc) else max(now + timedelta(minutes=10), datetime.fromisoformat(inc["opened_at"]) + timedelta(minutes=typical))
                best = max(best, t) if best else t
        return best

    def run_end(job: str) -> datetime:
        st = bs["jobs"][job]
        if st["pct"] and st["pct"] >= 0.1:
            return max(now + timedelta(minutes=1), st["started"] + (now - st["started"]) / st["pct"])
        return max(now + timedelta(minutes=1), st["started"] + timedelta(minutes=st["run_minutes"]))

    def avail(node: str) -> datetime:
        if node in memo:
            return memo[node]
        n = env.nodes[node]
        kind = n["kind"]
        if kind == "source":
            out = past
        elif kind == "file":
            received = bs.get("files", {}).get(node, bs["file_received"])
            out = past if received else (fix_eta(node) or now + timedelta(minutes=60))
        elif kind in ("replication", "stream"):
            lag = bs.get("lags", {}).get(node, bs["lag_minutes"])
            out = past if bs["ready"].get(node) and lag <= n["max_lag_minutes"] else (fix_eta(node) or now) + timedelta(minutes=lag / 4 + 5)
        elif kind == "job":
            out = job_end(node)
        elif bs["ready"].get(node):
            out = past
        elif env.builder_of(node):
            out = mart_avail(node, env.builder_of(node))
        else:
            out = max(avail(u) for u in env.up[node])
        memo[node] = out
        return out

    def job_end(job: str) -> datetime:
        st, n = bs["jobs"][job], env.nodes[job]
        if st["state"] == "succeeded":
            return st["ended"]
        if st["state"] == "running":
            return run_end(job)
        ins = max([avail(u) for u in env.up[job]] + [now]) if not n.get("builds") else now
        start = max(ins, fix_eta(job) or now, st["planned"] if st["state"] != "failed" else now)
        return start + timedelta(minutes=n["duration"])

    def mart_avail(mart: str, job: str) -> datetime:
        st, n = bs["jobs"][job], env.nodes[job]
        ins = max(avail(u) for u in env.up[mart])
        fe = fix_eta(job) or now
        retry = timedelta(minutes=RETRY_MINUTES if bs["built"] else n["duration"])
        if st["state"] in ("scheduled", "waiting"):
            start = max(st["planned"], now, fe)
            end = start + timedelta(minutes=n["duration"])
            return end if ins <= start else max(end, ins) + timedelta(minutes=RETRY_MINUTES)
        if st["state"] == "running":
            end = run_end(job)
            return end if ins <= st["started"] else max(end, ins) + timedelta(minutes=RETRY_MINUTES)
        return max(now, ins, fe) + retry

    jobs = {}
    for job, st in bs["jobs"].items():
        n = env.nodes[job]
        end = job_end(job)
        planned_end = st["planned"] + timedelta(minutes=n["duration"])
        jobs[job] = {"id": job, "name": n["name"], "ci": n["ci"], "state": st["state"], "planned": st["planned"].isoformat(),
                     "planned_end": planned_end.isoformat(), "started": st["started"].isoformat() if st["started"] else None,
                     "ended": st["ended"].isoformat() if st["ended"] else None, "eta": end.isoformat(), "attempt": st["attempt"],
                     "pct": st["pct"], "late_minutes": max(0, round((end - planned_end).total_seconds() / 60)), "error": st["error"]}
    services = {}
    for sid, s in env.services.items():
        job = s["delivered_by"]
        chain = set(env.chain(sid))
        blocking = [i["id"] for i in incs if chain & set(i.get("impact_nodes", []))]
        common = {"id": sid, "name": s["name"], "tier": s.get("tier", 2), "regulated": s.get("regulated", False), "audience": s["audience"],
                  "delivered_by": job, "pattern": s.get("pattern", "")}
        if not s.get("deadline"):            # a freshness promise: "never more than N minutes behind"
            stream = next((n for n in env.chain(sid) if env.nodes[n]["kind"] in ("stream", "replication")), job)
            lag = bs.get("lags", {}).get(stream, bs["lag_minutes"])
            limit = s["max_lag_minutes"]
            back = avail(job)
            services[sid] = {**common, "kind": "freshness", "deadline": now.isoformat(), "eta": max(back, now).isoformat(),
                             "status": "on track" if lag <= limit else "breached", "slack_minutes": round(limit - lag),
                             "blocking": blocking, "due": f"within {limit} min", "est": f"{lag:.0f} min behind"}
            continue
        deadline = hhmm(day, s["deadline"])
        eta = datetime.fromisoformat(jobs[job]["eta"])
        if jobs[job]["state"] == "succeeded":
            status = "delivered" if eta <= deadline else "delivered late"
        elif now > deadline:
            status = "breached"
        elif eta > deadline:
            status = "breach predicted"
        elif eta > deadline - timedelta(minutes=AT_RISK_MARGIN) or (blocking and eta > deadline - timedelta(minutes=60)):
            status = "at risk"
        else:
            status = "on track"
        services[sid] = {**common, "kind": "deadline", "deadline": deadline.isoformat(), "eta": eta.isoformat(), "status": status,
                         "slack_minutes": round((deadline - eta).total_seconds() / 60), "blocking": blocking if "delivered" not in status else [],
                         "due": s["deadline"], "est": eta.strftime("%H:%M")}
    return {"now": now.isoformat(), "jobs": jobs, "services": services, "ready": bs["ready"], "built": bs["built"],
            "lag_minutes": bs["lag_minutes"], "lags": bs.get("lags", {})}
