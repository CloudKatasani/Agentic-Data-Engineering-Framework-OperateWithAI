"""The language tasks the agents may hand to a model. Each is off unless switched on under
"AI models", runs on the provider and model chosen there, and returns a draft or suggestion
that a person reviews. With a function off, or on any failure, the agent's rule-based output
is used unchanged. Prompts carry status, log and ticket text only, redacted."""

from __future__ import annotations

import json

from ..llm import router

ESTATE = ("a data platform managed service (Oracle, Db2, GoldenGate, Informatica, Snowflake, dbt, Denodo, Power BI, Cognos on AWS and Azure)")


def _audit(ops, agent: str, ref: str, what: str, model: str) -> None:
    ops.audit(agent, f"Used a language model: {what}", ref, model)


def suggest(ops, inc: dict, ev: dict, evidence: list[dict]):
    """Diagnosis Agent: hypotheses for an error that matches no known error."""
    out = router.complete_json(
        "diagnosis.hypotheses",
        f"You help an operations engineer in {ESTATE}. One alert matches no known error. Propose up to three likely causes, each with the "
        "reason, and up to four read-only checks to run next. Do not propose changes. JSON keys: hypotheses (list of {cause, why}), next_checks (list of strings).",
        f"Tool: {ev['source']}\nSystem: {ops.env.cis[ev['ci']]['name']}\n<data>\n{ev['message']}\n"
        + json.dumps([{"check": e["check"], "result": e["result"]} for e in evidence], indent=1) + "\n</data>")
    if not out or not isinstance(out.get("hypotheses"), list):
        return None
    _audit(ops, "Diagnosis Agent", inc["id"], "hypotheses for an unknown error (unverified suggestion)", out["_model"])
    return {"model": out["_model"], "hypotheses": [h for h in out["hypotheses"] if isinstance(h, dict) and "cause" in h][:3],
            "next_checks": [str(c) for c in out.get("next_checks", [])][:4]}


def route_hint(ops, inc: dict, ev: dict):
    """Triage Agent: likely cause area for an alert no known error matches."""
    towers = ", ".join(t["name"] for t in ops.env.data["towers"])
    out = router.complete_json(
        "triage.route_hint",
        f"You help triage alerts in {ESTATE}. Suggest which support team most likely owns the cause. Teams: {towers}. "
        "JSON keys: team (one of the teams), area (a few words), why (one sentence).",
        f"Tool: {ev['source']}\nSystem: {ops.env.cis[ev['ci']]['name']}\n<data>\n{ev['message']}\n</data>")
    if not out or "team" not in out:
        return None
    _audit(ops, "Triage Agent", inc["id"], "suggested the owning team for an unrecognised alert", out["_model"])
    return {"model": out["_model"], "team": str(out["team"])[:60], "area": str(out.get("area", ""))[:80], "why": str(out.get("why", ""))[:300]}


def rewrite_message(ops, inc: dict, draft: str):
    """Comms Agent: the drafted message in plain business language, facts unchanged."""
    res = router.complete(
        "comms.rewrite",
        "You write incident updates for business stakeholders of a data platform. Rewrite the draft in plain language for people who are not "
        "engineers. Keep every fact, time, ticket number, service name and owner exactly; add nothing that is not in the draft; keep the Subject "
        "line and the section order; no apology padding. Return only the message.",
        f"<data>\n{draft}\n</data>")
    if not res:
        return None
    _audit(ops, "Comms Agent", inc["id"], "rewrote the stakeholder message draft", res.model)
    return {"text": res.text.strip(), "model": res.model}


def review_summary(ops, inc: dict, review: str):
    """Problem and Knowledge Agent: management summary for the post-incident review."""
    res = router.complete(
        "knowledge.review_summary",
        "Write a management summary of this post-incident review in at most four sentences: what happened, business impact, how it was "
        "fixed, and the one thing to change. Use only facts from the review. Return only the summary.",
        f"<data>\n{review}\n</data>", max_tokens=400)
    if not res:
        return None
    _audit(ops, "Problem and Knowledge Agent", inc["id"], "wrote the review summary", res.model)
    return {"text": res.text.strip(), "model": res.model}


def known_error(ops, inc: dict, message: str, notes: str):
    """Problem and Knowledge Agent: a clean cause and fix from the engineer's notes."""
    out = router.complete_json(
        "knowledge.known_error",
        "Turn an engineer's resolution notes into a known error entry. JSON keys: title (short, starts with the tool), cause (one or two "
        "sentences), fix (imperative steps in one paragraph). Use only what the notes and the alert say.",
        f"<data>\nAlert: {message}\nEngineer's notes: {notes}\n</data>", max_tokens=500)
    if not out or not all(k in out for k in ("title", "cause", "fix")):
        return None
    _audit(ops, "Problem and Knowledge Agent", inc["id"], "drafted the known error wording", out["_model"])
    return {"title": str(out["title"])[:120], "cause": str(out["cause"])[:500], "fix": str(out["fix"])[:800], "model": out["_model"]}


def handover_summary(ops, handover: str):
    """Service Reporting Agent: opening paragraph for the shift handover."""
    res = router.complete(
        "reporting.handover_summary",
        "Write the opening summary of a shift handover in at most five sentences for the incoming shift lead: what is open, what is waiting "
        "on whom, which business deliverables are at risk. Use only facts from the handover. Return only the summary.",
        f"<data>\n{handover}\n</data>", max_tokens=400)
    if not res:
        return None
    _audit(ops, "Service Reporting Agent", "", "wrote the handover summary", res.model)
    return {"text": res.text.strip(), "model": res.model}
