/* DataCrew- OperateWithAI console - dependency-free single-page app (OPERATE_SPEC section 11).
   Views: Fleet health, Case room, Service page, FinOps, Governance, Learning, plus the
   approvals inbox and onboarding. Every decision goes through the API, which enforces the
   same guards as the engine; the console never decides anything itself. */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const main = () => $("#main");
const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* storage unavailable */ } },
};
let CATALOG = null, FLEET = null, WHO = null;

// ------------------------------------------------------------------ api + ui helpers
async function api(method, path, body) {
  const headers = {};
  const token = store.get("op_token", "");
  if (token) headers.Authorization = `Bearer ${token}`;
  const opts = { method, headers };
  if (body !== undefined) { headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  const r = await fetch(`/ops${path}`, opts);
  if (r.status === 401) {
    const t = prompt("This console needs your API token (ADEF_API_TOKEN, or your personal token):");
    if (t) { store.set("op_token", t); return api(method, path, body); }
  }
  if (!r.ok) {
    let msg = r.statusText;
    try { const j = await r.json(); msg = j.detail || msg; if (j.guardrail) msg = `Guardrail: ${msg}`; } catch { /* not json */ }
    throw new Error(msg);
  }
  const ct = r.headers.get("content-type") || "";
  return ct.includes("json") ? r.json() : r.text();
}
function toast(msg, kind = "") {
  const t = document.createElement("div");
  t.className = `t ${kind}`;
  t.textContent = msg;
  $("#toast").appendChild(t);
  setTimeout(() => t.remove(), kind === "err" ? 8000 : 3500);
}
async function guard(fn, okMsg) {
  try { const r = await fn(); if (okMsg) toast(okMsg, "ok"); return r; } catch (e) { toast(e.message, "err"); throw e; }
}
function modal(html) { $("#modalBody").innerHTML = html; $("#modal").classList.remove("hidden"); }
function closeModal() { $("#modal").classList.add("hidden"); }
$("#modal").addEventListener("click", (e) => { if (e.target.id === "modal") closeModal(); });
window.closeModal = closeModal;

const BADGE = { pass: "b-ok", fail: "b-bad", matching: "b-ok", drifted: "b-bad", waived: "b-violet", "not verifiable": "b-warn",
  approved: "b-ok", rejected: "b-bad", pending: "b-warn", high: "b-ok", medium: "b-warn", low: "b-bad",
  closed: "b-ok", recovered: "b-ok", reviewed: "b-ok", failed: "b-bad", "awaiting-approval": "b-warn", "awaiting-decision": "b-warn",
  executing: "b-info", verifying: "b-info", diagnosing: "b-info", "options-ready": "b-info", triaged: "b-info", detected: "b-info" };
const badge = (s, label, cls) => `<span class="badge ${cls || BADGE[s] || ""}">${esc(label ?? s)}</span>`;
const sev = (s) => (s ? badge(s, s, `sev sev-${s}`) : "");
const trust = (t) => badge(t || "trusted", t || "trusted", `trust-${t || "trusted"}`);
const mode = (m) => badge(m, m, `mode-${m}`);
const fmtTime = (iso) => (iso ? new Date(iso).toLocaleString() : "");
const agentShort = (a) => String(a || "").replace(/^adef-ops-/, "");

function inline(s) {
  return esc(s).replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
}
function md(src) {
  if (!src) return '<div class="muted small">Nothing yet.</div>';
  const lines = String(src).replace(/\r/g, "").split("\n");
  let out = "", i = 0;
  while (i < lines.length) {
    const l = lines[i];
    if (/^\|.*\|\s*$/.test(l) && /^\|[\s:|-]+\|\s*$/.test(lines[i + 1] || "")) {
      const cells = (r) => r.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
      const head = cells(l); i += 2; const rows = [];
      while (i < lines.length && /^\|/.test(lines[i])) rows.push(cells(lines[i++]));
      out += `<div class="table-wrap"><table><thead><tr>${head.map((h) => `<th>${inline(h)}</th>`).join("")}</tr></thead><tbody>${rows.map((r) => `<tr>${r.map((c) => `<td>${inline(c).replace(/&lt;br&gt;/g, "<br>")}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
      continue;
    }
    const h = l.match(/^(#{1,4})\s+(.*)$/);
    if (h) { out += `<h${h[1].length + 1}>${inline(h[2])}</h${h[1].length + 1}>`; i++; continue; }
    if (/^\s*[-*]\s+/.test(l)) {
      const items = [];
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) items.push(lines[i++].replace(/^\s*[-*]\s+/, ""));
      out += `<ul>${items.map((x) => `<li>${inline(x)}</li>`).join("")}</ul>`; continue;
    }
    if (/^\s*\d+[.)]\s+/.test(l)) {
      const items = [];
      while (i < lines.length && /^\s*\d+[.)]\s+/.test(lines[i])) items.push(lines[i++].replace(/^\s*\d+[.)]\s+/, ""));
      out += `<ol>${items.map((x) => `<li>${inline(x)}</li>`).join("")}</ol>`; continue;
    }
    if (!l.trim()) { i++; continue; }
    const buf = [];
    while (i < lines.length && lines[i].trim() && !/^(#|\||\s*[-*]\s|\s*\d+[.)]\s)/.test(lines[i])) buf.push(lines[i++]);
    if (!buf.length) buf.push(lines[i++]);
    out += `<p>${buf.map(inline).join("<br>")}</p>`;
  }
  return `<div class="md">${out}</div>`;
}
const table = (head, rows, empty = "None.") => (rows.length
  ? `<div class="table-wrap"><table><thead><tr>${head.map((h) => `<th>${esc(h)}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table></div>`
  : `<div class="empty">${esc(empty)}</div>`);

// ------------------------------------------------------------------ routing
const routes = {};
const route = (name, fn) => { routes[name] = fn; };
const product = () => store.get("op_product", "") || (FLEET?.products[0]?.dp_id ?? "");
async function navigate() {
  const [, name = "fleet", ...args] = location.hash.split("/");
  $$("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.route === (name === "case" ? "cases" : name)));
  main().innerHTML = '<div class="loading">Loading…</div>';
  try { await (routes[name] || routes.fleet)(...args.map(decodeURIComponent)); }
  catch (e) { main().innerHTML = `<div class="card"><div class="notice bad">${esc(e.message)}</div></div>`; }
  refreshCounts();
}
window.addEventListener("hashchange", navigate);
// guard() already shows every failure as a toast; do not report it twice as an uncaught error.
window.addEventListener("unhandledrejection", (e) => e.preventDefault());
async function refreshFleet() {
  FLEET = await api("GET", "/fleet");
  const pick = $("#productPick");
  const cur = product();
  pick.innerHTML = FLEET.products.map((p) => `<option value="${esc(p.dp_id)}" ${p.dp_id === cur ? "selected" : ""}>${esc(p.dp_id)}</option>`).join("")
    || '<option value="">no products yet</option>';
  return FLEET;
}
async function refreshCounts() {
  try { const a = await api("GET", "/approvals"); $("#apprCount").innerHTML = a.length ? badge("", a.length, "b-warn") : ""; } catch { /* ignore */ }
}
$("#productPick").addEventListener("change", (e) => {
  store.set("op_product", e.target.value);
  const [, name] = location.hash.split("/");
  if (["service", "finops", "governance", "learning"].includes(name)) location.hash = `#/${name}/${e.target.value}`;
});
const person = (fallback = "") => WHO?.person || store.get("op_person", "") || fallback;
const personField = (fallback, id = "actAs") => (WHO?.person
  ? `<div class="small muted">Acting as <b>${esc(WHO.person)}</b> (bound to your token)</div>`
  : `<label>Acting as (a named person)</label><input id="${id}" value="${esc(person(fallback))}" placeholder="Your name as recorded in service.yaml">`);
const readPerson = (id = "actAs") => { const el = $(`#${id}`); const v = el ? el.value.trim() : WHO?.person; if (el) store.set("op_person", v); return v; };

// ------------------------------------------------------------------ Fleet health
route("fleet", async () => {
  const f = await refreshFleet();
  const t = f.totals;
  main().innerHTML = `
    ${f.global_stop ? `<div class="stopbar"><div><b>Global emergency stop.</b> ${esc(f.global_stop)}</div><button class="btn bad" data-act="clearstop">Lift stop…</button></div>` : ""}
    <div class="spread"><div><h1>Fleet health</h1><div class="sub">Every live data product under Operate, by service health, open work and items waiting for a person.</div></div>
      <div class="row"><button class="btn bad" data-act="stop">Emergency stop…</button></div></div>
    <div class="grid g4">
      <div class="card kpi"><div class="l">Products</div><div class="v">${t.products}</div></div>
      <div class="card kpi"><div class="l">Open S1 / S2</div><div class="v" style="color:${t.open_s1_s2 ? "var(--bad)" : "inherit"}">${t.open_s1_s2}</div></div>
      <div class="card kpi"><div class="l">Waiting on a person</div><div class="v">${t.pending_approvals}</div></div>
      <div class="card kpi"><div class="l">Escalations</div><div class="v">${t.escalations}</div></div>
    </div>
    <div class="card" style="margin-top:16px"><h2>Products</h2>
    ${table(["Product", "Mode", "Stack", "Trust", "Open cases", "Findings", "OG1", "Budget today", "Waiting"], f.products.map((p) => `
      <tr class="click" data-href="#/service/${esc(p.dp_id)}">
        <td><b>${esc(p.dp_id)}</b><div class="small muted">${esc(p.name)} · ${esc(p.domain_pack)} · ${esc(p.client_id || "")}</div>
          ${p.stop ? `<div class="small" style="color:var(--bad)">Stopped: ${esc(p.stop)}</div>` : ""}</td>
        <td>${mode(p.mode)}</td><td class="small">${esc(p.stack_profile || "simulator")}</td><td>${trust(p.trust_status)}</td>
        <td>${["S1", "S2", "S3", "S4"].filter((s) => p.open_cases[s]).map((s) => `${sev(s)} ${p.open_cases[s]}`).join(" ") || '<span class="muted">none</span>'}</td>
        <td class="small">${Object.entries(p.open_findings).filter(([, n]) => n).map(([k, n]) => `${k} ${n}`).join(", ") || "-"}</td>
        <td>${p.onboarded ? badge(p.og1.valid ? "approved" : "rejected", p.og1.valid ? "valid" : "not valid") : badge("pending", "not onboarded")}</td>
        <td class="small">${Object.entries(p.budgets).map(([k, b]) => `${esc(k.replace(/_/g, " "))}: ${b.spent}/${b.cap ?? "-"}`).join("<br>")}</td>
        <td>${p.pending_approvals ? badge("pending", p.pending_approvals) : "-"}</td></tr>`), "No products yet. Onboard one from Setup.")}
    </div>`;
  main().onclick = async (e) => {
    const row = e.target.closest("[data-href]");
    if (row) { location.hash = row.dataset.href; return; }
    const a = e.target.closest("[data-act]")?.dataset.act;
    if (a === "stop") stopDialog();
    if (a === "clearstop") {
      const by = prompt("Your name (lifting a stop is recorded in every product's escalation log):", person());
      if (by) { await guard(() => api("DELETE", `/stop?by=${encodeURIComponent(by)}`), "Stop lifted"); navigate(); }
    }
  };
});
function stopDialog(dp) {
  modal(`<h2>Emergency stop${dp ? ` - ${esc(dp)}` : " - every product"}</h2>
    <div class="notice bad">Sets ${dp ? "this product" : "every product"} to L0: agents route nothing, request nothing and execute nothing until a person lifts the stop.</div>
    ${personField("", "stopBy")}<label>Reason</label><input id="stopReason" placeholder="What made you stop it">
    <div class="row" style="margin-top:14px"><button class="btn bad" id="stopGo">Stop</button><button class="btn" onclick="closeModal()">Cancel</button></div>`);
  $("#stopGo").onclick = async () => {
    await guard(() => api("POST", "/stop", { by: readPerson("stopBy"), reason: $("#stopReason").value || "emergency stop", dp }), "Stopped");
    closeModal(); navigate();
  };
}

// ------------------------------------------------------------------ Case queue
route("cases", async () => {
  await refreshFleet();
  const f = store.get("op_case_filter", { dp: "", state: "open", severity: "", type: "" });
  const q = new URLSearchParams(Object.entries(f).filter(([, v]) => v)).toString();
  const cases = await api("GET", `/cases?${q}`);
  main().innerHTML = `<h1>Case queue</h1><div class="sub">One case per cause. Severity is set by rule and the rule is shown.</div>
    <div class="card tight"><div class="field-row">
      <div><label>Product</label><select data-f="dp"><option value="">All</option>${FLEET.products.map((p) => `<option ${f.dp === p.dp_id ? "selected" : ""}>${esc(p.dp_id)}</option>`).join("")}</select></div>
      <div><label>State</label><select data-f="state">${[["open", "Open"], ["", "All"], ["closed", "Closed"]].map(([v, l]) => `<option value="${v}" ${f.state === v ? "selected" : ""}>${l}</option>`).join("")}</select></div>
      <div><label>Severity</label><select data-f="severity"><option value="">Any</option>${["S1", "S2", "S3", "S4"].map((s) => `<option ${f.severity === s ? "selected" : ""}>${s}</option>`).join("")}</select></div>
      <div><label>Type</label><select data-f="type"><option value="">Any</option>${["INC", "FIN", "GOV", "PRB", "CHG"].map((s) => `<option ${f.type === s ? "selected" : ""}>${s}</option>`).join("")}</select></div>
    </div></div>
    <div class="card">${table(["Case", "Product", "Severity and rule", "State", "Trust", "Owner", "Next action", "Updated"], cases.map((c) => `
      <tr class="click" data-href="#/case/${esc(c.dp_id)}/${esc(c.id)}"><td><b>${esc(c.id)}</b>${c.escalated ? ` ${badge("", "escalated", "b-bad")}` : ""}</td><td class="small">${esc(c.dp_id)}</td>
      <td>${sev(c.severity)} <span class="small">${esc(c.severity_rule || c.title || "")}</span></td><td>${badge(c.state)}</td>
      <td>${c.trust_status ? trust(c.trust_status) : "-"}</td><td class="small">${esc(agentShort(c.owner) || "-")}</td>
      <td class="small">${esc(c.next_action || "")}</td><td class="small muted">${fmtTime(c.updated)}</td></tr>`), "No cases match.")}</div>`;
  main().onchange = (e) => { const k = e.target.dataset.f; if (k) { f[k] = e.target.value; store.set("op_case_filter", f); navigate(); } };
  main().onclick = (e) => { const r = e.target.closest("[data-href]"); if (r) location.hash = r.dataset.href; };
});

// ------------------------------------------------------------------ Case room
const INC_STATES = ["detected", "triaged", "contained", "diagnosing", "options-ready", "awaiting-approval", "executing", "verifying", "recovered", "reviewed", "closed"];
route("case", async (dp, id) => {
  const d = await api("GET", `/cases/${encodeURIComponent(id)}?dp=${encodeURIComponent(dp)}`);
  const c = d.case;
  const tab = store.get("op_case_tab", "overview");
  const seen = new Set(c.timeline.map((t) => (t.event.match(/^-> ([a-z-]+)/) || [])[1]).filter(Boolean));
  const life = c.type === "INC" ? `<div class="lifecycle">${INC_STATES.map((s) => `<span class="st ${s === c.state ? "now" : seen.has(s) || s === "detected" ? "done" : ""}">${s}</span>`).join("")}${c.state === "failed" ? '<span class="st bad">failed</span>' : ""}</div>` : "";
  const dg = d.diagnosis;
  const shadowNote = d.mode === "shadow" ? '<div class="notice" style="margin-bottom:12px"><b>Shadow mode.</b> Operate observes and proposes only: nothing is executed, requested or sent. Compare with your team\'s own response.</div>' : "";
  main().innerHTML = `
    <div class="spread"><div><div class="small muted"><a href="#/cases">Case queue</a> / ${esc(dp)}</div>
      <h1>${esc(c.id)} ${sev(c.severity)} ${c.trust_status ? trust(c.trust_status) : ""} ${c.escalated ? badge("", "escalated", "b-bad") : ""}</h1>
      <div class="sub">${esc(c.severity_rule || c.timeline[0]?.event || "")}${c.severity_would_lower ? `<br><span class="small">Would lower: ${esc(c.severity_would_lower)}</span>` : ""}</div></div>
      <div class="row">${c.type === "INC" ? ["diagnose", "options", "verify", "post-incident", "close"].map((s) => `<button class="btn small" data-step="${s}">${s.replace("-", " ")}</button>`).join("") : ""}</div></div>
    ${shadowNote}${life}
    ${c.next_action ? `<div class="now ai"><div><b>Next:</b> ${esc(c.next_action)}<div class="small">Owner: ${esc(agentShort(c.owner) || "-")} · First failing object: ${esc(c.first_failing_object || "-")} · Failed attempts: ${c.failed_attempts}</div></div></div>` : ""}
    ${c.open_questions.length ? `<div class="notice warn" style="margin-bottom:14px">${c.open_questions.map(esc).join("<br>")}</div>` : ""}
    <div class="tabs">${[["overview", "Timeline and impact"], ["diagnosis", "Diagnosis and evidence"], ["options", "Options"], ["approvals", `Approvals (${d.approvals.filter((a) => a.status === "pending").length})`], ["learning", "Verification and learning"]]
      .map(([k, l]) => `<div class="tab ${tab === k ? "active" : ""}" data-tab="${k}">${l}</div>`).join("")}</div>
    <div id="tabBody"></div>`;
  const body = {
    overview: () => `<div class="grid g2"><div class="card"><h2>Timeline</h2><div class="timeline">${c.timeline.map((t) => `<div class="ev ${t.event.startsWith("->") ? "tr" : ""}"><div class="ts">${fmtTime(t.at)}</div><span class="by">${esc(agentShort(t.by))}</span> ${esc(t.event)}</div>`).join("")}</div></div>
      <div><div class="card"><h2>Impact</h2>${md(d.impact)}</div>
      <div class="card"><h2>Signals ${d.group ? `<span class="small muted">${esc(d.group.id)}</span>` : ""}</h2>
      ${table(["Signal", "Class", "Object", "Observed", "Expected", "Label"], d.signals.map((s) => `<tr><td class="mono small">${esc(s.id)}</td><td>${esc(s.signal_class)}</td><td class="small">${esc(s.object)}</td><td>${esc(s.observed)}</td><td>${esc(s.expected)}</td><td class="lbl-${esc(s.label)} small">${esc(s.label || "")}</td></tr>`))}
      ${d.group?.known_events?.length ? `<h3>Known events (leads, not conclusions)</h3><ul class="small">${d.group.known_events.map((k) => `<li>${esc(k.kind)}: ${esc(k.source || k.release || "")} ${esc(k.status || k.object || "")}</li>`).join("")}</ul>` : ""}
      </div></div></div>`,
    diagnosis: () => (dg ? `<div class="grid g2"><div class="card"><h2>Conclusion</h2><div class="kv">
      <div class="k">Cause class</div><div><b>${esc(dg.cause_class)}</b></div><div class="k">Confidence</div><div>${badge(dg.confidence)}</div>
      <div class="k">Statement</div><div>${esc(dg.statement)}</div><div class="k">Would confirm</div><div>${esc(dg.confirm_by)}</div>
      <div class="k">Reasoner</div><div class="small">${esc(dg.reasoner)}</div></div>
      ${dg.council_required ? `<div class="notice warn" style="margin-top:10px">S1: council-of-three review ${badge(d.council_status)}. Options wait for a majority.</div>` : ""}
      ${dg.untrusted_reports?.length ? `<div class="notice bad" style="margin-top:10px">Instruction-like text in logs was treated as data and not followed:<br>${dg.untrusted_reports.map(esc).join("<br>")}</div>` : ""}</div>
      <div class="card"><h2>Evidence (metadata only)</h2>${table(["ID", "Evidence", "Summary"], d.evidence.map((e) => `<tr><td class="mono small">${esc(e.id)}</td><td><b>${esc(e.kind)}</b><div class="small muted">${esc(e.source)}</div></td><td class="small" style="min-width:45%">${esc(e.summary)}${e.untrusted ? ` ${badge("", "untrusted", "b-violet")}` : ""}</td></tr>`))}</div></div>
      <div class="card"><h2>Hypotheses tested</h2>${table(["Cause class", "Status", "Score", "For", "Against"], dg.hypotheses.map((h) => `<tr><td>${esc(h.cause_class)}</td><td class="hyp-${esc(h.status)}">${esc(h.status)}</td><td>${h.score}</td><td class="small">${h.supporting.map(esc).join("<br>") || "-"}</td><td class="small">${h.against.map(esc).join("<br>") || "-"}</td></tr>`))}</div>`
      : `<div class="card empty">Not diagnosed yet. <button class="btn primary small" data-step="diagnose">Diagnose</button></div>`),
    options: () => `${dg?.council_required && d.council_status !== "agreed" ? `<div class="card"><h2>Council of three</h2><div class="small muted">Independent reviewers (people, not team agents). ${d.council.length} of 3 recorded.</div>
        ${d.council.map((v) => `<div class="small">${esc(v.reviewer)}: ${v.agrees ? badge("approved", "agrees") : badge("rejected", "disagrees")} ${esc(v.note)}</div>`).join("")}
        <div class="field-row">${WHO?.person ? "" : `<div><label>Reviewer</label><input id="councilWho" value="${esc(person())}"></div>`}<div><label>Note</label><input id="councilNote"></div></div>
        <div class="row" style="margin-top:10px"><button class="btn ok small" data-council="1">Agree with the diagnosis</button><button class="btn bad small" data-council="0">Disagree</button></div></div>` : ""}
      <div class="card"><h2>Options, ranked by safety</h2>${d.options.length ? table(["#", "Kind", "Option", "Effect", "Risk", "Reversibility", "Approval", ""], d.options.map((o) => `<tr>
        <td>${o.no}${o.recommended ? ` ${badge("", "recommended", "b-ok")}` : ""}</td><td>${esc(o.kind)}</td><td><b>${esc(o.title)}</b>${o.note ? `<div class="small muted">${esc(o.note)}</div>` : ""}</td>
        <td class="small">${esc(o.effect)}</td><td class="small">${esc(o.risk)}</td><td class="small">${esc(o.reversibility)}</td><td class="small">${esc(o.approval)}</td>
        <td>${o.kind === "trade-off" ? '<span class="small muted">FinOps, OG4</span>' : `<button class="btn small ${o.executable_now ? "primary" : ""}" data-option="${o.no}">${o.executable_now ? "Execute" : o.kind === "wait" ? "Wait" : o.action_type && o.kind !== "code-fix" ? "Request" : "Raise CHG"}</button>`}</td></tr>`))
        : '<div class="empty">No options yet. <button class="btn primary small" data-step="options">Build options</button></div>'}</div>
      ${d.actions.length ? `<div class="card"><h2>Actions</h2>${table(["Action", "Type", "Status", "Approval", ""], d.actions.map((a) => `<tr><td class="mono">${esc(a.id)}</td><td>${esc(a.action_type)}</td><td>${badge(a.status)}${a.reason ? `<div class="small muted">${esc(a.reason)}</div>` : ""}</td><td class="mono small">${esc(a.approval_id || "-")}</td>
        <td>${a.status === "approved-awaiting-people" ? `<button class="btn small" data-executed="${esc(a.id)}">I ran it</button>` : ""}</td></tr>`))}</div>` : ""}`,
    approvals: () => `<div class="card"><h2>Gate requests for this case</h2>${d.approvals.length ? d.approvals.map((a) => approvalCard({ ...a, dp_id: dp, case_id: c.id, roles: a.roles.map((r) => ({ role: r, named: d.approvers[r], decided: a.decisions.some((x) => x.role === r) })) }, a.status)).join("") : '<div class="empty">No gate requests.</div>'}</div>`,
    learning: () => `<div class="grid g2"><div class="card"><h2>Verification</h2>${md(d.verification)}</div>
      <div class="card"><h2>Confirm the cause</h2><div class="small muted">A person's confirmed cause overrides the agent's in every record and evaluation label.</div>
        ${c.confirmed_cause ? `<div style="margin-top:8px">Confirmed by <b>${esc(c.confirmed_by)}</b>: ${esc(c.confirmed_cause)}</div>` : `
        ${personField(d.approvers.service_owner, "causeBy")}<label>Cause class</label><select id="causeClass">${["source data", "upstream outage", "code defect", "configuration", "capacity", "platform", "access", "unknown"].map((x) => `<option ${dg?.cause_class === x ? "selected" : ""}>${x}</option>`).join("")}</select>
        <div class="row" style="margin-top:10px"><button class="btn primary small" data-step="confirm-cause">Confirm</button></div>`}</div></div>
      <div class="card"><h2>Post-incident note</h2>${md(d.post_incident)}</div>`,
  };
  $("#tabBody").innerHTML = (body[tab] || body.overview)();
  main().onclick = async (e) => {
    const t = e.target.closest("[data-tab]");
    if (t) { store.set("op_case_tab", t.dataset.tab); return navigate(); }
    const s = e.target.closest("[data-step]")?.dataset.step;
    const q = `?dp=${encodeURIComponent(dp)}`;
    if (s) {
      const payload = s === "confirm-cause" ? { cause_class: $("#causeClass").value, by: readPerson("causeBy") } : {};
      await guard(() => api("POST", `/cases/${id}/steps/${s}${q}`, payload), `${s.replace("-", " ")}: done`);
      return navigate();
    }
    const o = e.target.closest("[data-option]")?.dataset.option;
    if (o) { const r = await guard(() => api("POST", `/cases/${id}/steps/act${q}`, { option: Number(o) })); toast(`Option ${o}: ${r.status}${r.approval ? ` (${r.approval})` : ""}`, "ok"); return navigate(); }
    const cv = e.target.closest("[data-council]")?.dataset.council;
    if (cv !== undefined) {
      await guard(() => api("POST", `/cases/${id}/steps/council${q}`, { reviewer: WHO?.person || $("#councilWho").value, agrees: cv === "1", note: $("#councilNote").value }), "Verdict recorded");
      return navigate();
    }
    const ex = e.target.closest("[data-executed]")?.dataset.executed;
    if (ex) {
      const by = WHO?.person || prompt("Who ran it? (a named person)", person());
      if (by) { await guard(() => api("POST", `/cases/${id}/actions/${ex}/executed${q}`, { by }), "Recorded"); navigate(); }
      return;
    }
    if (await handleDecision(e, () => navigate())) return;
  };
});

// ------------------------------------------------------------------ approvals (inbox and shared card)
function approvalCard(a, status = "pending") {
  const open = a.roles.filter((r) => !r.decided);
  return `<div class="card gate-card ${status === "approved" ? "approved" : status === "rejected" ? "rejected" : ""}" style="margin-top:10px">
    <div class="spread"><div><b>${esc(a.id)} · ${esc(a.gate)}</b> ${badge(status)} <span class="small muted">${esc(a.dp_id)}${a.case_id ? ` · <a href="#/case/${esc(a.dp_id)}/${esc(a.case_id)}">${esc(a.case_id)}</a>` : ""}</span>
      <div>${esc(a.decision_asked)}</div>
      ${a.evidence?.length ? `<div class="small muted">Evidence: ${a.evidence.map(esc).join(", ")}</div>` : ""}
      ${a.risk ? `<div class="small muted">Risk: ${esc(a.risk)}</div>` : ""}${a.recommendation ? `<div class="small muted">Recommendation: ${esc(a.recommendation)}</div>` : ""}
      ${a.expires ? `<div class="small muted">Expires ${fmtTime(a.expires)}</div>` : ""}</div>
      <div class="small muted">${a.any_of ? "Any one of the roles" : "Every role must decide"}</div></div>
    ${status === "pending" ? open.map((r) => `<div class="role-row"><div><b>${esc(r.role.replace(/_/g, " "))}</b><div class="small muted">Named holder: ${esc(r.named || "none named")}</div></div>
      <div>${WHO?.person ? `<span class="small">as ${esc(WHO.person)}</span>` : `<input data-who="${esc(a.id)}:${esc(r.role)}" value="${esc(r.named || "")}" aria-label="Approver name" title="Only the named holder of this role can decide">`}
        <input data-why="${esc(a.id)}:${esc(r.role)}" placeholder="Reason (returned word for word on rejection)" style="margin-top:4px"></div>
      <div class="row"><button class="btn ok small" data-decide="approved" data-req="${esc(a.id)}" data-role="${esc(r.role)}" data-dp="${esc(a.dp_id)}" data-case="${esc(a.case_id || "")}">Approve</button>
        <button class="btn bad small" data-decide="rejected" data-req="${esc(a.id)}" data-role="${esc(r.role)}" data-dp="${esc(a.dp_id)}" data-case="${esc(a.case_id || "")}">Reject</button></div></div>`).join("")
      : (a.decisions || []).map((x) => `<div class="small">${esc(x.role)}: ${esc(x.approver)} ${badge(x.decision)} ${esc(x.reason || "")}</div>`).join("")}
  </div>`;
}
async function handleDecision(e, after) {
  const b = e.target.closest("[data-decide]");
  if (!b) return false;
  const key = `${b.dataset.req}:${b.dataset.role}`;
  const approver = WHO?.person || $(`[data-who="${CSS.escape(key)}"]`)?.value.trim();
  const reason = $(`[data-why="${CSS.escape(key)}"]`)?.value || "";
  if (approver) store.set("op_person", approver);
  const body = { request_id: b.dataset.req, approver, role: b.dataset.role, decision: b.dataset.decide, reason };
  const path = b.dataset.case ? `/cases/${b.dataset.case}/approvals?dp=${encodeURIComponent(b.dataset.dp)}` : `/approvals/${b.dataset.dp}/${b.dataset.req}`;
  const r = await guard(() => api("POST", path, body));
  toast(`${r.request} ${r.gate}: ${r.status}${r.follow_up?.status ? ` · ${r.follow_up.status}` : ""}`, "ok");
  after();
  return true;
}
route("approvals", async () => {
  const items = await api("GET", "/approvals");
  main().innerHTML = `<h1>Approvals</h1><div class="sub">Every open gate request across the fleet. Only the named holder of a role can decide, and agents never can.</div>
    ${items.length ? items.map((a) => approvalCard(a)).join("") : '<div class="card empty">Nothing is waiting on a person.</div>'}`;
  main().onclick = (e) => handleDecision(e, () => navigate());
});

// ------------------------------------------------------------------ Service page
const LEVELS = ["L0", "L1", "L2", "L3"];
route("service", async (dpArg) => {
  await refreshFleet();
  const dp = dpArg || product();
  if (!dp) { main().innerHTML = '<div class="card empty">No products yet. <a href="#/onboard">Onboard one.</a></div>'; return; }
  store.set("op_product", dp); $("#productPick").value = dp;
  const s = await api("GET", `/services/${dp}`);
  const p = s.summary;
  const tab = store.get("op_service_tab", "autonomy");
  main().innerHTML = `
    ${p.stop ? `<div class="stopbar"><div><b>Stopped (L0).</b> ${esc(p.stop)}</div><button class="btn bad small" data-act="clearstop">Lift stop…</button></div>` : ""}
    <div class="spread"><div><h1>${esc(dp)} ${mode(p.mode)} ${trust(p.trust_status)}</h1><div class="sub">${esc(p.name)} · ${esc(p.domain_pack)} · stack ${esc(p.stack_profile || "simulator")} · ${esc(p.data_origin || "")}</div></div>
      <div class="row"><button class="btn small" data-act="tick">${p.mode === "demo" ? "Run a tick" : "Process new snapshots"}</button><button class="btn bad small" data-act="stop">Stop this product…</button></div></div>
    <div class="grid g4">
      <div class="card kpi"><div class="l">OG1 readiness</div><div class="v small" style="font-size:15px">${p.og1.valid ? badge("approved", "valid") : badge("rejected", "not valid")}</div><div class="small muted">${esc(p.og1.detail)}</div></div>
      <div class="card kpi"><div class="l">Open cases</div><div class="v">${Object.values(p.open_cases).reduce((a, b) => a + b, 0)}</div><div class="small">${["S1", "S2", "S3", "S4"].map((x) => `${x} ${p.open_cases[x]}`).join(" · ")}</div></div>
      <div class="card kpi"><div class="l">Waiting on a person</div><div class="v">${p.pending_approvals}</div></div>
      <div class="card kpi"><div class="l">Budget today</div><div class="small">${Object.entries(p.budgets).map(([k, b]) => `${esc(k.replace(/_/g, " "))}: <b>${b.spent}</b> / ${b.cap}`).join("<br>")}</div></div>
    </div>
    ${p.mode === "demo" && s.faults.length ? `<div class="card" style="margin-top:16px"><h2>Demo Simulator</h2><div class="row">
      <select id="faultPick" style="max-width:420px">${s.faults.map((f) => `<option value="${esc(f.id)}">${esc(f.id)} - ${esc(f.description)}</option>`).join("")}</select>
      <button class="btn primary small" data-act="inject">Inject and tick</button><button class="btn small" data-act="resolve">Source delivers (F-01)</button><button class="btn small" data-act="reset">Reset to healthy</button></div>
      <div class="small muted" style="margin-top:6px">Synthetic faults on the golden product only. Real products never accept simulated faults.</div></div>` : ""}
    <div class="card" style="margin-top:16px"><h2>Ask the Commander</h2><div class="small muted">Requests are classified by rule and answered with the governed route; nothing is executed from here.</div>
      <div class="desk" style="margin-top:8px"><input id="deskText" placeholder="e.g. just mute that freshness alert"><button class="btn small" data-act="desk">Ask</button></div><div id="deskOut"></div></div>
    <div class="tabs" style="margin-top:16px">${[["autonomy", "Autonomy ladder"], ["monitors", `Monitors (${s.monitors.length})`], ["runbook", "Runbook"], ["readiness", "Readiness"], ["health", "Health summary"], ["binding", "Stack binding"], ["escalations", `Escalations (${s.escalations.length})`]]
      .map(([k, l]) => `<div class="tab ${tab === k ? "active" : ""}" data-tab="${k}">${l}</div>`).join("")}</div><div id="tabBody"></div>`;
  const tabs = {
    autonomy: () => `<div class="card"><h2>Autonomy ladder</h2><div class="small muted" style="margin-bottom:10px">Filled: effective level the hook enforces now. Outlined: stored level. Dashed: above the action's ceiling. Promotions happen only at OG7 from the platform's track record${p.mode !== "demo" ? ", and only after an approved go-live decision" : ""}.</div>
      <div class="ladder"><div class="hd">Action type</div>${LEVELS.map((l) => `<div class="hd" style="text-align:center">${l}</div>`).join("")}<div class="hd">Why · track record</div>
      ${s.autonomy.map((a) => `<div><b>${esc(a.action_type)}</b> <span class="small muted">${esc(a.class)}${a.gate ? ` · ${esc(a.gate)}` : ""}</span></div>
        ${LEVELS.map((l) => `<div class="lv ${l === a.effective ? "on" : l === a.stored ? "stored" : ""} ${LEVELS.indexOf(l) > LEVELS.indexOf(a.highest) ? "cap off" : ""}">${l}</div>`).join("")}
        <div class="small muted">${esc(a.reason)}${a.executed_by_people ? " · executed by people" : ""} · verified ${a.track_record.verified}, failed ${a.track_record.failed}</div>`).join("")}</div></div>`,
    monitors: () => `<div class="card">${table(["Monitor", "Class", "Object", "Rule", "Severity hint", "Owner", "Runbook"], s.monitors.map((m) => `<tr><td class="mono small">${esc(m.id)}</td><td>${esc(m.signal_class)}</td><td class="small">${esc(m.object)}</td><td class="mono small">${esc(JSON.stringify(m.rule))}</td><td>${sev(m.severity_hint)}</td><td class="small">${esc(m.owner)}</td><td class="small">${esc(m.runbook_entry)}</td></tr>`))}</div>`,
    runbook: () => `<div class="card">${md(s.runbook)}</div>`,
    readiness: () => `<div class="card">${md(s.readiness)}</div>`,
    health: () => `<div class="card">${md(s.health)}</div>`,
    binding: () => `<div class="card">${s.binding ? table(["Operate slot", "Build slot", "Tool"], Object.entries(s.binding).flatMap(([os, b]) => Object.entries(b).map(([bs, t]) => `<tr><td>${esc(os)}</td><td class="mono small">${esc(bs)}</td><td>${esc(t)}</td></tr>`))) : '<div class="empty">Canonical Demo Simulator (no Stack Profile bound).</div>'}</div>`,
    escalations: () => `<div class="card">${table(["To", "By", "Case", "Reason"], s.escalations.slice().reverse().map((e) => `<tr><td>${esc(e.to)}</td><td class="small">${esc(e.by)}</td><td>${esc(e.case_id || "-")}</td><td class="small">${esc(e.reason)}</td></tr>`))}</div>`,
  };
  $("#tabBody").innerHTML = (tabs[tab] || tabs.autonomy)();
  main().onclick = async (e) => {
    const t = e.target.closest("[data-tab]");
    if (t) { store.set("op_service_tab", t.dataset.tab); return navigate(); }
    const a = e.target.closest("[data-act]")?.dataset.act;
    if (!a) return;
    if (a === "tick") { const r = await guard(() => api("POST", `/services/${dp}/tick`)); toast(`Tick: ${r.signals ?? r.snapshots} ${r.signals !== undefined ? "signal(s)" : "snapshot(s)"}, ${(r.outcomes || r.opened || []).length} outcome(s)`, "ok"); }
    if (a === "inject") { const r = await guard(() => api("POST", "/simulate/faults", { dp, fault_id: $("#faultPick").value })); const o = r.tick.outcomes; toast(`${r.injected.fault}: ${r.tick.signals} signal(s) -> ${o.map((x) => `${x.outcome} ${x.case || x.routed_to || ""}`).join(", ") || "no new outcome"}`, "ok"); }
    if (a === "reset") await guard(() => api("POST", "/simulate/reset", { dp }), "Simulator reset to healthy");
    if (a === "resolve") await guard(() => api("POST", "/simulate/resolve-source", { dp, fault_id: "F-01" }), "Source delivered");
    if (a === "stop") return stopDialog(dp);
    if (a === "clearstop") { const by = prompt("Your name:", person()); if (by) await guard(() => api("DELETE", `/stop?dp=${dp}&by=${encodeURIComponent(by)}`), "Stop lifted"); }
    if (a === "desk") {
      const by = WHO?.person || prompt("Your name:", person()); if (!by) return;
      const r = await guard(() => api("POST", "/requests", { dp, text: $("#deskText").value, by }));
      $("#deskOut").innerHTML = `<div class="notice ${r.decision === "refused" ? "warn" : ""}" style="margin-top:8px"><b>${esc(r.decision)}</b> (${esc(r.intent)}): ${esc(r.reason)}<br>Route: ${esc(r.alternative)}${r.gate ? ` · gate ${esc(r.gate)}` : ""}</div>`;
      return;
    }
    navigate();
  };
});

// ------------------------------------------------------------------ FinOps
route("finops", async (dpArg) => {
  await refreshFleet();
  const dp = dpArg || product();
  const f = await api("GET", `/finops/${dp}`);
  main().innerHTML = `<div class="spread"><div><h1>FinOps · ${esc(dp)}</h1><div class="sub">Actual against estimate, variances with evidence, findings with the arithmetic shown. Recommend-only: decisions at OG4.</div></div>
    <button class="btn primary small" data-act="review">Run review</button></div>
    <div class="grid g2"><div class="card"><h2>Latest review</h2>${md(f.latest_review)}</div>
    <div class="card"><h2>Savings ledger</h2>${table(["Finding", "Decision", "Estimated (month)", "Realised", "Period"], f.ledger.map((r) => `<tr><td>${esc(r.finding)}</td><td>${badge(r.decision === "accepted" ? "approved" : r.decision === "pending" ? "pending" : "rejected", r.decision)}</td><td>${esc(r.estimated_monthly_low)} to ${esc(r.estimated_monthly_high)}</td><td>${esc(r.realised_monthly || "-")}</td><td class="small">${esc(r.measurement_period || "-")}</td></tr>`))}</div></div>
    ${f.findings.map((x) => `<div class="card"><div class="spread"><h2>${esc(x.id)} ${badge(x.state)}</h2><a class="small" href="#/approvals">Decide at OG4</a></div>${md(x.body)}</div>`).join("")}`;
  main().onclick = async (e) => { if (e.target.closest("[data-act=review]")) { const r = await guard(() => api("POST", `/finops/${dp}/review`)); toast(`Variance ${r.variance_pct}% · ${r.findings.length} finding(s)`, "ok"); navigate(); } };
});

// ------------------------------------------------------------------ Governance
route("governance", async (dpArg) => {
  await refreshFleet();
  const dp = dpArg || product();
  const g = await api("GET", `/governance/${dp}`);
  main().innerHTML = `<div class="spread"><div><h1>Governance · ${esc(dp)}</h1><div class="sub">Live controls against the approved baseline. Recommend-only: no agent grants, revokes, masks or tags; corrections at OG5, executed by people.</div></div>
    <div class="row"><input id="period" value="${new Date().toISOString().slice(0, 7)}" style="width:110px"><button class="btn small" data-act="pack">Evidence pack</button><button class="btn primary small" data-act="review">Run review</button></div></div>
    <div class="card"><h2>Controls</h2>${table(["Control", "Status", "Detail"], Object.entries(g.controls).map(([k, v]) => `<tr><td>${esc(k)}</td><td>${badge(v.status)}</td><td class="small">${esc(v.detail)}</td></tr>`))}</div>
    ${g.findings.map((x) => `<div class="card"><h2>${esc(x.id)} ${badge(x.state)}</h2>${md(x.body)}</div>`).join("") || '<div class="card empty">No drift findings.</div>'}`;
  main().onclick = async (e) => {
    const a = e.target.closest("[data-act]")?.dataset.act;
    if (a === "review") { const r = await guard(() => api("POST", `/governance/${dp}/review`)); toast(`${r.raised.length} new finding(s)`, "ok"); navigate(); }
    if (a === "pack") { const t = await guard(() => api("GET", `/governance/${dp}/evidence-pack?period=${encodeURIComponent($("#period").value)}`)); modal(`${md(t)}<div class="row" style="margin-top:12px"><button class="btn" onclick="closeModal()">Close</button></div>`); }
  };
});

// ------------------------------------------------------------------ Learning
route("learning", async (dpArg) => {
  await refreshFleet();
  const dp = dpArg || product();
  const l = await api("GET", `/learning/${dp}`);
  const acc = l.accuracy;
  main().innerHTML = `<div class="spread"><div><h1>Learning · ${esc(dp)}</h1><div class="sub">Problems, noise, evaluation replays and autonomy proposals. Every control change is a proposal for OG7.</div></div>
    <div class="row"><button class="btn small" data-act="noise-report">Noise report</button><button class="btn small" data-act="propose-autonomy">Propose autonomy changes</button></div></div>
    <div class="grid g4"><div class="card kpi"><div class="l">Replays</div><div class="v">${l.replays.length}</div></div>
      <div class="card kpi"><div class="l">Diagnosis accuracy (confirmed)</div><div class="v">${acc.accuracy ?? "-"}</div><div class="small muted">${acc.correct} of ${acc.scored} · ${acc.unconfirmed} unconfirmed, not scored</div></div>
      <div class="card kpi"><div class="l">Open problems</div><div class="v">${l.problems.filter((p) => p.state !== "closed").length}</div></div>
      <div class="card kpi"><div class="l">Proposals</div><div class="v">${l.autonomy_proposals.length + l.tuning_proposals.length}</div></div></div>
    <div class="grid g2" style="margin-top:16px"><div class="card"><h2>Evaluation replays</h2>${table(["Replay", "Severity", "Label", "Agent", "Confirmed"], l.replays.map((r) => `<tr><td class="mono small">${esc(r.id)}</td><td>${sev(r.severity)}</td><td>${esc(r.label)}</td><td>${esc(r.agent_cause)}</td><td>${r.confirmed ? badge("approved", "yes") : badge("pending", "no")}</td></tr>`))}</div>
      <div class="card"><h2>Platform track record</h2>${table(["Action type", "Executions", "L2 approved", "Verified", "Failed", "Blocked"], Object.entries(l.track_record).map(([k, v]) => `<tr><td>${esc(k)}</td><td>${v.executions}</td><td>${v.l2_approved_executions}</td><td>${v.verified}</td><td>${v.failed_verification}</td><td>${v.blocked}</td></tr>`))}</div></div>
    ${l.problems.map((p) => `<div class="card"><h2>${esc(p.id)} ${badge(p.state)}</h2>${md(p.body)}</div>`).join("")}
    <div class="card"><h2>Latest noise report</h2>${md(l.noise_report)}</div>
    ${[...l.autonomy_proposals, ...l.tuning_proposals].map((t) => `<div class="card">${md(t)}</div>`).join("")}`;
  main().onclick = async (e) => {
    const a = e.target.closest("[data-act]")?.dataset.act;
    if (a) { await guard(() => api("POST", `/learning/${dp}/${a}`, {}), "Done"); navigate(); }
  };
});

// ------------------------------------------------------------------ Onboarding
route("onboard", async () => {
  await refreshFleet();
  const notOnboarded = FLEET.products.filter((p) => !p.onboarded);
  main().innerHTML = `<h1>Onboard a product</h1><div class="sub">A product enters operations from Build stage 7's Operations Handover Pack. OG0 accepts it; onboarding drafts monitors, runbook and bindings, proves them, and asks for OG1.</div>
    <div class="grid g2"><div class="card"><h2>Demo Simulator golden product</h2>
      <label>Golden product</label><select id="pack">${CATALOG.packs.map((p) => `<option>${esc(p)}</option>`).join("")}</select>
      <label>Stack Profile (from Build-With-AI)</label><select id="profile"><option value="">Canonical simulator</option>${CATALOG.profiles.map((p) => `<option value="${esc(p.id)}">${esc(p.id)} - ${esc(p.label)}</option>`).join("")}</select>
      <div class="row" style="margin-top:12px"><button class="btn primary" data-act="demo">Release into operations</button></div></div>
    <div class="card"><h2>Real product in shadow mode</h2><div class="small muted">Read-only, from metadata exports. See SHADOW_MODE.md. Paste the handover pack and START HERE answers as JSON.</div>
      <label>Stack Profile</label><select id="sprofile">${CATALOG.profiles.map((p) => `<option value="${esc(p.id)}">${esc(p.id)}</option>`).join("")}</select>
      <label>Export root (server path)</label><input id="exports" placeholder="/data/operate-exports">
      <label>Handover pack (JSON)</label><textarea id="spack"></textarea><label>START HERE answers (JSON)</label><textarea id="sanswers"></textarea>
      <div class="row" style="margin-top:12px"><button class="btn primary" data-act="shadow">Release in shadow mode</button></div></div></div>
    <div class="card"><h2>Waiting for onboarding</h2>${table(["Product", "Mode", "Next"], notOnboarded.map((p) => `<tr><td><b>${esc(p.dp_id)}</b></td><td>${mode(p.mode)}</td><td>${p.pending_approvals ? `<a href="#/approvals">Decide OG0 first</a>` : `<button class="btn small primary" data-onboard="${esc(p.dp_id)}">Onboard to OG1</button>`}</td></tr>`), "Every product is onboarded.")}</div>
    <div id="out"></div>`;
  main().onclick = async (e) => {
    const a = e.target.closest("[data-act]")?.dataset.act;
    if (a === "demo") {
      const r = await guard(() => api("POST", "/services", { mode: "demo", pack: $("#pack").value, profile: $("#profile").value || null }));
      toast(`${r.dp_id} released; ${r.og0_request} (OG0) waiting`, "ok"); store.set("op_product", r.dp_id); navigate();
    }
    if (a === "shadow") {
      let pack, answers;
      try { pack = JSON.parse($("#spack").value); answers = JSON.parse($("#sanswers").value); } catch { toast("Pack and answers must be valid JSON", "err"); return; }
      const r = await guard(() => api("POST", "/services", { mode: "shadow", profile: $("#sprofile").value, exports: $("#exports").value, pack, answers }));
      toast(`${r.dp_id} in shadow mode; ${r.og0_request} waiting`, "ok"); navigate();
    }
    const dp = e.target.closest("[data-onboard]")?.dataset.onboard;
    if (dp) {
      const r = await guard(() => api("POST", `/services/${dp}/onboard`));
      $("#out").innerHTML = `<div class="card"><h2>${esc(dp)}: ${r.ready ? "ready" : "not ready"}</h2>${r.ready ? `OG1 requested: <a href="#/approvals">${esc(r.og1_request)}</a>` : esc(r.problems.join("; "))}</div>`;
      refreshFleet();
    }
  };
});

// ------------------------------------------------------------------ boot
(async function boot() {
  const theme = store.get("op_theme", "");
  if (theme) document.documentElement.dataset.theme = theme;
  $("#themeToggle").onclick = () => {
    const dark = document.documentElement.dataset.theme === "dark" || (!document.documentElement.dataset.theme && matchMedia("(prefers-color-scheme: dark)").matches);
    document.documentElement.dataset.theme = dark ? "light" : "dark";
    store.set("op_theme", document.documentElement.dataset.theme);
  };
  try {
    const h = await api("GET", "/health");
    $("#version").textContent = `v${h.version}`;
    $("#brandName").textContent = h.brand.name; $("#brandSub").textContent = h.brand.tagline;
    [CATALOG, WHO] = await Promise.all([api("GET", "/catalog"), api("GET", "/whoami")]);
    await refreshFleet();
  } catch (e) { main().innerHTML = `<div class="card"><div class="notice bad">${esc(e.message)}</div></div>`; return; }
  navigate();
})();
