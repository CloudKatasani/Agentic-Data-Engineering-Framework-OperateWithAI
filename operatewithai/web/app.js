/* OperateWithAI console - dependency-free single-page app.
   Every page reads from /api; every human decision is a POST to /api/inbox/{id} made as the
   person chosen under "You are". The console decides nothing itself. */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const main = () => $("#main");
const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* storage unavailable */ } },
};
let META = null, PULSE = "", ACTIONS = {};
const me = () => META?.me || store.get("owai_me", "");

// ------------------------------------------------------------------ api + ui helpers
async function api(method, path, body, as) {
  const headers = { "X-Actor": as || me() };
  const token = store.get("owai_token", "");
  if (token) headers.Authorization = "Bearer " + token;
  const opts = { method, headers };
  if (body !== undefined) { headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  const r = await fetch("/api" + path, opts);
  if (r.status === 401) {
    const t = prompt("This console needs an API token (ADEF_API_TOKEN, or your personal token):");
    if (t) { store.set("owai_token", t); return api(method, path, body); }
  }
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch { /* not json */ }
    throw new Error(msg);
  }
  return r.json();
}
function toast(msg, kind = "") {
  const t = document.createElement("div");
  t.className = "t " + kind;
  t.textContent = msg;
  $("#toast").appendChild(t);
  setTimeout(() => t.remove(), kind === "err" ? 8000 : 3200);
}
async function guard(fn, okMsg) {
  try { const r = await fn(); if (okMsg) toast(okMsg, "ok"); return r; } catch (e) { toast(e.message, "err"); throw e; }
}
function modal(html) { $("#modalBody").innerHTML = html; $("#modal").classList.remove("hidden"); }
function closeModal() { $("#modal").classList.add("hidden"); }
$("#modal").addEventListener("click", (e) => { if (e.target.id === "modal") closeModal(); });
window.closeModal = closeModal;
window.addEventListener("unhandledrejection", (e) => e.preventDefault());

const hm = (iso) => (iso ? String(iso).slice(11, 16) : "");
const badge = (label, cls = "") => `<span class="badge ${cls}">${esc(label)}</span>`;
const pri = (p) => `<span class="pri ${esc(p)}">${esc(p)}</span>`;
const dot = (s) => `<span class="dot ${esc(s)}"></span>`;
const STATE_CLS = { Resolved: "b-ok", Closed: "b-ok", New: "b-bad", Assigned: "b-warn", "In Progress": "b-info", "On Hold": "b-violet" };
const SVC_CLS = { "on track": "b-ok", delivered: "b-ok", "at risk": "b-warn", "breach predicted": "b-bad", breached: "b-bad", "delivered late": "b-bad" };
const STEP_CLS = { done: "b-ok", approved: "b-ok", failed: "b-bad", pending: "", "waiting approval": "b-warn", "waiting for person": "b-warn", checking: "b-info", running: "b-info" };
const who = (name, human) => `<span class="who ${human ? "hu" : "ai"}">${human ? "Person" : "Agent"}</span>${esc(name)}`;
const isAgent = (name) => /Agent$|^Service$/.test(name || "");
const slug = (s) => String(s).replace(/\s+/g, "-");
const table = (head, rows, empty = "None.") => (rows.length
  ? `<div class="table-wrap"><table><thead><tr>${head.map((h) => `<th>${esc(h)}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table></div>`
  : `<div class="empty">${esc(empty)}</div>`);
function inline(s) { return esc(s).replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>"); }
function md(src) {
  const lines = String(src || "").split("\n");
  let out = "", i = 0;
  while (i < lines.length) {
    const l = lines[i];
    if (/^\|.*\|\s*$/.test(l) && /^\|[\s:|-]+\|\s*$/.test(lines[i + 1] || "")) {
      const cells = (r) => r.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
      const head = cells(l); i += 2; const rows = [];
      while (i < lines.length && /^\|/.test(lines[i])) rows.push(cells(lines[i++]));
      out += table(head, rows.map((r) => `<tr>${r.map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`));
      continue;
    }
    const h = l.match(/^(#{1,3})\s+(.*)$/);
    if (h) { out += `<h${h[1].length + 1}>${inline(h[2])}</h${h[1].length + 1}>`; i++; continue; }
    if (/^\s*(-|\d+\.)\s+/.test(l)) {
      const items = [];
      while (i < lines.length && /^\s*(-|\d+\.)\s+/.test(lines[i])) items.push(lines[i++].replace(/^\s*(-|\d+\.)\s+/, ""));
      out += `<ul>${items.map((x) => `<li>${inline(x)}</li>`).join("")}</ul>`; continue;
    }
    if (l.trim()) out += `<p>${inline(l)}</p>`;
    i++;
  }
  return `<div class="md">${out}</div>`;
}

// ------------------------------------------------------------------ asks (notifications) and actions
function askCard(n, opts = {}) {
  const seat = n.escalated_to || n.to;
  const mine = opts.asSeat || [n.to, n.escalated_to].includes(me());
  const late = n.status === "open" && n.escalated_to;
  ACTIONS[n.id] = n;
  const buttons = n.status !== "open" ? `<span class="muted small">${esc(n.done_by ? `${n.done_by}: ${n.done_action}` : n.done_action || n.status)}</span>`
    : mine ? n.actions.map((a) => `<button class="btn small ${a.style === "primary" ? "primary" : a.style === "bad" ? "bad" : ""}" data-act="${esc(n.id)}|${esc(a.id)}|${opts.asSeat ? esc(seat) : ""}">${esc(a.label)}${opts.asSeat ? ` <span style="opacity:.75;font-weight:400">· as the ${esc(seat)}</span>` : ""}</button>`).join(" ")
      : `<span class="muted small">Waiting for ${esc(n.escalated_to || n.to)}</span>`;
  return `<div class="card ask ${n.kind === "fyi" ? "fyi" : ""} ${late ? "late" : ""}">
    <div class="meta">${n.priority && n.ticket ? pri(n.priority) : ""}<span class="who ai">Agent</span>${esc(n.agent)} asks the ${esc(n.to)}
      <span>· sent ${hm(n.created)}</span>${n.kind !== "fyi" && n.status === "open" ? `<span>· respond by <b>${hm(n.due)}</b></span>` : ""}
      ${n.escalated_to ? badge("escalated to " + n.escalated_to, "b-bad") : ""}
      ${n.ticket && !opts.noLink ? `<a href="#/ticket/${esc(n.ticket)}">${esc(n.ticket)}</a>` : ""}</div>
    <h3>${esc(n.title)}</h3>
    ${n.kind === "pir" ? `<details><summary class="small">Read the review</summary>${md(n.body)}</details><p></p>` : `<pre>${esc(n.body)}</pre>`}
    <div class="row">${buttons}</div></div>`;
}
document.addEventListener("click", async (e) => {
  const b = e.target.closest("[data-act]");
  if (!b) return;
  const [id, actionId, seat] = b.dataset.act.split("|");
  const n = ACTIONS[id];
  const a = n.actions.find((x) => x.id === actionId);
  const send = async (text) => {
    await guard(() => api("POST", "/inbox/" + id, { action: actionId, text }, seat), "Recorded: " + a.label + (seat ? " (as the " + seat + ")" : ""));
    closeModal(); PULSE = ""; render();
  };
  if (!a.input) return send("");
  modal(`<h2>${esc(a.label)}</h2><p class="muted">${esc(n.title)}</p><label>${esc(a.input)}${a.required ? " (required)" : ""}</label>
    <textarea id="actText" class="${a.prefill ? "big" : ""}">${esc(a.prefill || "")}</textarea>
    <div class="row" style="margin-top:12px"><button class="btn primary" id="actGo">${esc(a.label)}</button><button class="btn ghost" onclick="closeModal()">Cancel</button></div>`);
  $("#actText").focus();
  $("#actGo").onclick = () => send($("#actText").value);
});

// ------------------------------------------------------------------ routing
const routes = {};
const AUTO = ["home", "inbox", "incidents", "ticket", "batch", "requests", "run"];
function current() { const [, name = "team", ...args] = location.hash.split("/"); return [routes[name] ? name : "team", args.map(decodeURIComponent)]; }
async function render(quiet) {
  const [name, args] = current();
  $$("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.route === (name === "ticket" ? "incidents" : name)));
  if (!quiet) main().innerHTML = '<div class="loading">Loading…</div>';
  ACTIONS = {};
  try { await routes[name](...args); } catch (e) { main().innerHTML = `<div class="card"><div class="notice bad">${esc(e.message)}</div></div>`; }
  refreshChrome();
}
window.addEventListener("hashchange", () => render());
async function refreshChrome() {
  try {
    const [p, inbox] = await Promise.all([api("GET", "/pulse"), api("GET", "/inbox?person=" + encodeURIComponent(me()))]);
    PULSE = p.now + p.notes + p.run;
    const open = inbox.items.filter((n) => n.kind !== "fyi").length;
    $("#inboxCount").innerHTML = open ? badge(open, "b-warn") : "";
    const d = new Date(p.now);
    $("#clockbar").innerHTML = `<span class="muted">Simulated night at ${esc(META.client.name)}</span>
      <span class="time">${d.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" })} ${hm(p.now)}</span><span class="sp"></span>
      <span class="muted small">Move the clock:</span>
      ${[[5, "+5 min"], [15, "+15 min"], [60, "+1 hour"]].map(([m, l]) => `<button class="btn small" data-adv="${m}">${l}</button>`).join("")}
      <a class="btn small ghost" href="#/simulator">Scenarios</a>`;
  } catch { /* shown by the page */ }
}
document.addEventListener("click", async (e) => {
  const b = e.target.closest("[data-adv]");
  if (!b) return;
  b.disabled = true;
  await guard(() => api("POST", "/sim/advance", { minutes: Number(b.dataset.adv) }));
  PULSE = ""; render(true);
});
setInterval(async () => {
  const [name] = current();
  if (document.hidden || !$("#modal").classList.contains("hidden") || !AUTO.includes(name)) return;
  if (["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName)) return;
  try { const p = await api("GET", "/pulse"); if (p.now + p.notes + p.run !== PULSE) render(true); } catch { /* offline */ }
}, 6000);

// ------------------------------------------------------------------ command center
routes.home = async () => {
  const o = await api("GET", "/overview");
  const open = o.incidents.filter((i) => i.state !== "Resolved");
  const risk = o.services.filter((s) => ["at risk", "breach predicted", "breached"].includes(s.status)).length;
  const w = o.watch;
  main().innerHTML = `
    <h1>Command center</h1><div class="sub">${esc(o.client)} · night batch and morning deliverables</div>
    <div class="grid g4">
      <div class="card kpi"><div class="v">${open.length}</div><div class="l">Open incidents${open.some((i) => i.priority === "P1") ? " · P1 active" : ""}</div></div>
      <div class="card kpi"><div class="v">${o.asks}</div><div class="l">Decisions waiting on people</div></div>
      <div class="card kpi"><div class="v">${risk}</div><div class="l">Business services at risk</div></div>
      <div class="card kpi"><div class="funnel"><span><b>${w.raw || 0}</b> tool reports</span>→<span><b>${w.events || 0}</b> alerts</span>→<span><b>${o.incidents.length}</b> incidents</span></div><div class="l"><a href="#/data">What the agents read tonight</a></div></div>
    </div>
    <h2>Business services due this morning</h2>
    <div class="grid g4">${o.services.map((s) => `<div class="card svc s-${slug(s.status)}">
      <div class="nm">${esc(s.name)}</div><div class="muted small">${esc(s.audience)}${s.regulated ? " · regulated" : ""}</div>
      <div class="big">${hm(s.eta)}</div><div class="small">${s.status.startsWith("delivered") ? "delivered" : "estimate"} · due ${hm(s.deadline)}</div>
      <div style="margin-top:8px">${badge(s.status, SVC_CLS[s.status])} ${s.blocking.map((b) => `<a href="#/ticket/${esc(b)}">${esc(b)}</a>`).join(" ")}</div></div>`).join("")}</div>
    <div class="grid" style="grid-template-columns: 2fr 1fr; margin-top: 18px">
      <div>
        <h2 style="margin-top:0">Incidents</h2>
        ${table(["", "Incident", "State", "Owner", "Next", "Opened"], o.incidents.map((i) => `<tr class="click" onclick="location.hash='#/ticket/${esc(i.id)}'">
          <td>${pri(i.priority)}</td><td><b>${esc(i.id)}</b><br>${esc(i.title)}<div class="muted small">${i.events} alert(s) in this one ticket · ${esc(i.tower_name)}</div></td>
          <td>${badge(i.state, STATE_CLS[i.state])}</td><td>${esc(i.assignee)}</td><td class="small">${esc(i.next)}</td><td class="mono">${hm(i.opened_at)}</td></tr>`), "No incidents tonight so far.")}
        <h2>Technology towers</h2>
        <div class="grid g3">${o.towers.filter((t) => t.systems.length).map((t) => `<div class="card tower"><h3>${dot(t.status)}${esc(t.name)}</h3>
          <div class="muted small">On call: ${esc(t.on_call || "-")} ${t.incidents.map((i) => `<a href="#/ticket/${esc(i)}">${esc(i)}</a>`).join(" ")}</div>
          ${t.systems.map((s) => `<div class="sys">${dot(s.status)}<span><b>${esc(s.name)}</b> <span class="muted">${esc(s.cloud)}</span><br><span class="muted">${esc(s.detail)}</span></span></div>`).join("")}</div>`).join("")}</div>
      </div>
      <div>
        <div class="card"><h3 style="margin-top:0">Waiting on people</h3>
          ${o.waiting.length ? o.waiting.map(([p, c]) => `<div class="spread" style="padding:4px 0"><a href="#/inbox" data-as="${esc(p)}">${esc(p)}</a>${badge(c, "b-warn")}</div>`).join("") : '<div class="muted small">Nothing is waiting on a person.</div>'}
          ${o.prevented.length ? `<div class="notice" style="margin-top:10px">Prevented tonight: the service account expiry never happened because the rotation request was approved in time.</div>` : ""}</div>
        ${o.advisories.length ? `<div class="card"><h3 style="margin-top:0">Advisories</h3>${o.advisories.map((e) => `<div class="small" style="padding:4px 0">${dot("amber")}${esc(e.message)}<div class="muted">${esc(e.handling)}</div></div>`).join("")}</div>` : ""}
        <div class="card"><h3 style="margin-top:0">Latest activity</h3><div class="feed">${o.activity.slice(0, 8).map((a) => `<div class="e"><span class="ts">${hm(a.at)}</span> ${who(a.actor, a.actor_type === "person")} ${a.ref ? `<a href="${/^(INC|RITM|PRB)/.test(a.ref) ? "#/ticket/" + esc(a.ref) : "#/audit"}">${esc(a.ref)}</a>` : ""}<br>${esc(a.action)}</div>`).join("")}</div></div>
      </div>
    </div>`;
};
document.addEventListener("click", (e) => { const a = e.target.closest("[data-as]"); if (a) setMe(a.dataset.as); });

// ------------------------------------------------------------------ inbox
routes.inbox = async () => {
  const all = store.get("owai_inbox_all", false);
  const r = await api("GET", `/inbox?person=${encodeURIComponent(me())}${all ? "&all=true" : ""}`);
  const asks = r.items.filter((n) => n.kind !== "fyi" && n.status === "open");
  const fyi = r.items.filter((n) => n.kind === "fyi" && n.status === "open");
  const past = r.items.filter((n) => n.status !== "open");
  const p = META.people.find((x) => x.name === me()) || {};
  main().innerHTML = `<div class="spread"><div><h1>My inbox</h1><div class="sub">You are the ${esc(me())}${p.tower_name ? " · on call for " + esc(p.tower_name) : ""}${p.role_text ? " · " + esc(p.role_text) : ""}</div></div>
      <label class="chk small"><input type="checkbox" id="inboxAll" ${all ? "checked" : ""}> Show handled</label></div>
    <h2>Needs your decision (${asks.length})</h2>${asks.map((n) => askCard(n)).join("") || '<div class="empty">Nothing needs you right now. The agents will ask when it does.</div>'}
    ${fyi.length ? `<h2>For your information (${fyi.length})</h2>${fyi.map((n) => askCard(n)).join("")}` : ""}
    ${past.length ? `<h2>Handled</h2>${past.map((n) => askCard(n)).join("")}` : ""}`;
  $("#inboxAll").onchange = (e) => { store.set("owai_inbox_all", e.target.checked); render(); };
};

// ------------------------------------------------------------------ incidents
routes.incidents = async () => {
  const rows = await api("GET", "/tickets");
  const sect = (type, title, cols, fn, empty) => `<h2>${title}</h2>${table(cols, rows.filter((t) => t.type === type).map(fn), empty)}`;
  main().innerHTML = `<h1>Incidents and problems</h1><div class="sub">Each is mirrored to the client's ServiceNow as the system of record</div>
    ${sect("incident", "Incidents", ["", "Incident", "State", "Tower / owner", "Response SLA", "Resolve by", "Known error"], (i) => `<tr class="click" onclick="location.hash='#/ticket/${esc(i.id)}'">
      <td>${pri(i.priority)}</td><td><b>${esc(i.id)}</b> ${i.major ? badge("major", "b-bad") : ""}<br>${esc(i.title)}</td><td>${badge(i.state, STATE_CLS[i.state])}</td>
      <td>${esc(i.tower_name)}<br><span class="muted">${esc(i.assignee)}</span></td>
      <td>${i.ack_at ? badge(i.sla.respond_met ? "met " + hm(i.ack_at) : "missed", i.sla.respond_met ? "b-ok" : "b-bad") : "due " + hm(i.sla.respond_due)}</td>
      <td>${i.resolved_at ? badge(i.sla.resolve_met ? "met " + hm(i.resolved_at) : "missed", i.sla.resolve_met ? "b-ok" : "b-bad") : hm(i.sla.resolve_due)}</td><td>${esc(i.ke || "not known")}</td></tr>`, "No incidents.")}
    ${sect("problem", "Problems (recurring causes)", ["Problem", "Stage", "Owner"], (i) => `<tr class="click" onclick="location.hash='#/ticket/${esc(i.id)}'"><td><b>${esc(i.id)}</b><br>${esc(i.title)}</td><td>${badge(i.stage || i.state)}</td><td>${esc(i.assignee)}</td></tr>`, "No problem records.")}`;
};

routes.ticket = async (id) => {
  const r = await api("GET", "/tickets/" + encodeURIComponent(id));
  const t = r.ticket, d = t.diagnosis, plan = t.plan;
  const sla = t.type === "incident" ? `<div class="kv" style="grid-template-columns:150px 1fr">
      <div class="k">Response SLA</div><div>${t.ack_at ? `${t.sla.respond_met ? badge("met", "b-ok") : badge("missed", "b-bad")} acknowledged ${hm(t.ack_at)} by ${esc(t.assignee)}` : `due ${hm(t.sla.respond_due)}`}</div>
      <div class="k">Resolution SLA</div><div>${t.resolved_at ? `${t.sla.resolve_met ? badge("met", "b-ok") : badge("missed", "b-bad")} resolved ${hm(t.resolved_at)}` : `due ${hm(t.sla.resolve_due)}`}</div>
      <div class="k">Impact / urgency</div><div>${esc(t.impact)} / ${esc(t.urgency)}</div><div class="k">Systems</div><div>${esc((t.cis || []).join(", "))}</div></div>` : "";
  const chains = (r.chains || []).map((c) => `<div class="small"><b>${esc(c.service.name)}</b> · due ${hm(c.service.deadline)} · estimate ${hm(c.service.eta)} ${badge(c.service.status, SVC_CLS[c.service.status])}</div>
    <div class="chain">${c.nodes.map((n, i) => `${i ? '<span class="arrow">›</span>' : ""}<div class="cn ${n.root ? "root" : n.affected ? "hit" : "ok"}"><div class="k">${esc(n.kind)}${n.root ? " · cause here" : ""}</div>${esc(n.name)}<div class="st ${slug(n.state)}">${esc(n.state)}${n.eta && !["succeeded"].includes(n.state) ? " · est. " + hm(n.eta) : ""}</div></div>`).join("")}</div>`).join("");
  const diag = d ? `<div class="card"><h3 style="margin-top:0">Diagnosis ${badge(d.status, { confirmed: "b-ok", likely: "b-warn", unknown: "b-bad", unconfirmed: "b-bad", recognised: "b-info" }[d.status])} ${t.ke ? `<a href="#/knowledge">${esc(t.ke)}</a>` : ""}</h3>
      <p>${esc(d.cause)}</p>${table(["Check (read-only)", "What it showed", ""], d.evidence.map((e) => `<tr><td>${esc(e.check)}<div class="muted small mono">${esc(e.probe)}</div></td><td>${esc(e.result)}</td><td>${e.confirms === true ? badge("confirms", "b-ok") : e.confirms === false ? badge("does not confirm", "b-bad") : badge("fact")}</td></tr>`))}
      ${d.suggestion ? `<div class="notice warn" style="margin-top:10px"><b>Language-model suggestion (unverified, ${esc(d.suggestion.model)})</b><ul>${d.suggestion.hypotheses.map((h) => `<li>${esc(h.cause)}: ${esc(h.why)}</li>`).join("")}</ul>Checks to try: ${esc(d.suggestion.next_checks.join("; "))}</div>` : ""}</div>` : "";
  const steps = plan ? `<div class="card"><h3 style="margin-top:0">Fix plan ${badge(plan.status, { done: "b-ok", accepted: "b-info", manual: "b-violet", blocked: "b-bad", proposed: "b-warn" }[plan.status])}
      <span class="muted small">${plan.source === "engineer" ? "worked by the engineer" : "from runbook " + esc(plan.source)}${plan.accepted_by ? " · accepted by " + esc(plan.accepted_by) : " · nothing runs until the engineer accepts"}</span></h3>
      <ul class="steps">${plan.steps.map((s) => `<li class="${s.status === "done" ? "done" : s.status === "failed" ? "failed" : s.status !== "pending" ? "live" : ""}"><span class="num">${s.n}</span>
        <div><b>${esc(s.title)}</b><div class="small">${s.kind === "manual" ? who(t.assignee, true) : who("Resolution Agent", false)}
          ${s.approver ? ` · approval: <b>${esc(s.approver)}</b> (${esc(s.approver_role.replace(/_/g, " "))})${s.approved_by ? " ✓ " + hm(s.approved_at) : ""}` : s.kind === "action" ? " · standard runbook step" : ""}
          ${s.how ? `<span class="muted"> · ${esc(s.how)}</span>` : ""}</div>${s.result ? `<div class="res">${esc(s.result)}</div>` : ""}${s.guidance && s.status !== "done" ? `<div class="res">${esc(s.guidance)}</div>` : ""}</div>
        <div>${badge(s.status, STEP_CLS[s.status])}</div></li>`).join("")}</ul></div>` : "";
  const comms = (t.comms || []).length ? `<div class="card"><h3 style="margin-top:0">Stakeholder messages</h3>${t.comms.map((c) => `<details><summary>${esc(c.stage)} ${badge(c.status, { sent: "b-ok", draft: "b-warn" }[c.status])} <span class="muted small">to ${esc(c.to.join(", "))}${c.sent_by ? " · approved by " + esc(c.sent_by) + " " + hm(c.sent_at) : ""}</span></summary><pre class="pre">${esc(c.text)}</pre></details>`).join("")}</div>` : "";
  const req = t.type === "request" ? `<div class="card"><div class="kv" style="grid-template-columns:150px 1fr"><div class="k">Requested by</div><div>${esc(t.requester)}</div><div class="k">Stage</div><div>${badge(t.stage)}</div>
      ${Object.entries(t.fields).map(([k, v]) => `<div class="k">${esc(k)}</div><div>${esc(META.nodes[v] || v)}</div>`).join("")}
      <div class="k">Approvals</div><div>${t.approvals.map((a) => `${esc(a.person)} (${esc(a.role.replace(/_/g, " "))}) ${badge(a.status, { approved: "b-ok", declined: "b-bad", pending: "b-warn" }[a.status])}`).join("<br>") || "None needed (standard request)"}</div></div></div>` : "";
  const prob = t.type === "problem" ? `<div class="card"><div class="kv" style="grid-template-columns:150px 1fr"><div class="k">Occurrences</div><div>${t.occurrences} in 30 days</div><div class="k">Stage</div><div>${badge(t.stage)}</div><div class="k">Permanent fix</div><div>${esc(t.permanent_fix)}</div></div></div>` : "";
  main().innerHTML = `<div class="muted small"><a href="#/${t.type === "request" ? "requests" : "incidents"}">← ${t.type === "request" ? "Service requests" : "Incidents"}</a></div>
    <h1>${pri(t.priority)} ${esc(t.id)} ${badge(t.state, STATE_CLS[t.state])} ${t.major ? badge("major incident", "b-bad") : ""}</h1>
    <div class="sub">${esc(t.title)} · ${esc(r.tower_name)} · owner ${esc(t.assignee || "-")}</div>
    ${r.asks.length ? `<h2 style="margin-top:0">Waiting on people</h2>${r.asks.map((n) => askCard(n, { noLink: true })).join("")}` : t.state === "Resolved" ? "" : `<div class="now ai"><div><b>With the agents.</b><div class="small">Nothing is waiting on a person for this ticket right now.</div></div></div>`}
    ${sla ? `<div class="card">${sla}</div>` : ""}${req}${prob}
    ${chains ? `<div class="card"><h3 style="margin-top:0">Business impact through the data chain</h3>${chains}</div>` : ""}
    ${diag}${steps}${comms}
    ${t.pir ? `<div class="card"><h3 style="margin-top:0">Post-incident review ${badge(t.pir.status, t.pir.status === "approved" ? "b-ok" : "b-warn")}</h3>${md(t.pir.text)}</div>` : ""}
    ${(r.events || []).length ? `<div class="card"><h3 style="margin-top:0">Alerts handled in this ticket (${r.events.length})</h3>${table(["First seen", "Tool", "Alert", "Codes", "Handling", "State"], r.events.map((e) => `<tr><td class="mono">${hm(e.first_seen)}</td><td>${esc(e.source)}</td><td>${esc(e.message)}${e.count > 1 ? ` <span class="muted">×${e.count}</span>` : ""}</td><td class="mono small">${esc(e.codes.join(" "))}</td><td class="small">${esc(e.handling)}</td><td>${badge(e.status, e.status === "cleared" ? "b-ok" : "b-bad")}</td></tr>`))}</div>` : ""}
    <div class="card"><h3 style="margin-top:0">Timeline</h3><div class="feed">${t.timeline.map((x) => `<div class="e"><span class="ts">${hm(x.at)}</span> ${who(x.actor, x.human)}<br>${esc(x.text)}</div>`).join("")}</div>
        <div class="row" style="margin-top:10px"><input id="cmt" placeholder="Add a work note as the ${esc(me())}"><button class="btn" id="cmtGo">Add</button></div>
        <details style="margin-top:12px"><summary class="small muted">ServiceNow record (${esc(t.servicenow.table)}) as it is mirrored to the client's system of record</summary><pre class="code">${esc(JSON.stringify(t.servicenow, null, 2))}</pre></details></div>`;
  $("#cmtGo").onclick = async () => { await guard(() => api("POST", `/tickets/${id}/comment`, { text: $("#cmt").value }), "Note added"); render(true); };
};

// ------------------------------------------------------------------ batch
routes.batch = async () => {
  const b = await api("GET", "/batch");
  const day = b.now.slice(0, 11), t0 = new Date(day + "00:30:00").getTime(), span = 8.5 * 3600e3;
  const pos = (iso) => Math.max(0, Math.min(100, ((new Date(iso).getTime() - t0) / span) * 100));
  const bar = (cls, a, z, title) => `<div class="gbar ${cls}" style="left:${pos(a)}%;width:${Math.max(pos(z) - pos(a), 0.6)}%" title="${esc(title)}"></div>`;
  const axis = [1, 2, 3, 4, 5, 6, 7, 8].map((h) => `<span style="left:${pos(`${day}0${h}:00:00`)}%">0${h}:00</span>`).join("");
  const deadlines = b.services.map((s) => `<div class="gdead" style="left:${pos(s.deadline)}%" title="${esc(s.name)} deadline ${hm(s.deadline)}"></div>`).join("");
  main().innerHTML = `<h1>Batch and SLA</h1><div class="sub">The Batch and SLA Agent re-plans the night every cycle from scheduler state and open incidents · replication lag ${b.lag_minutes} min</div>
    <div class="card">${table(["Business service", "Deadline", "Estimate", "Slack", "Status", "In the way"], b.services.map((s) => `<tr><td><b>${esc(s.name)}</b><div class="muted small">${esc(s.audience)}${s.regulated ? " · regulated" : ""}</div></td><td class="mono">${hm(s.deadline)}</td><td class="mono">${hm(s.eta)}</td>
      <td>${s.status.startsWith("delivered") ? "" : s.slack_minutes + " min"}</td><td>${badge(s.status, SVC_CLS[s.status])}</td><td>${s.blocking.map((x) => `<a href="#/ticket/${esc(x)}">${esc(x)}</a>`).join(" ")}</td></tr>`))}</div>
    <div class="card"><h3 style="margin-top:0">Tonight's jobs <span class="muted small">dashed box = plan · solid = actual or projected · blue line = now · red lines = service deadlines</span></h3>
      <div class="grow"><div></div><div class="gaxis">${axis}</div></div>
      ${b.jobs.map((j) => `<div class="grow"><div><b>${esc(j.name)}</b><div class="muted small">${esc(j.ci)} · ${esc(j.state)}${j.attempt > 1 ? " · attempt " + j.attempt : ""}${j.late_minutes > 5 && j.state !== "succeeded" ? ` · <span style="color:var(--bad)">${j.late_minutes} min behind</span>` : ""}</div></div>
        <div class="gtrack">${bar("plan", j.planned, j.planned_end, "planned " + hm(j.planned) + "-" + hm(j.planned_end))}
          ${j.started || j.state === "failed" ? bar(j.state, j.started || j.eta, j.ended || j.eta, j.state + " " + hm(j.started) + "-" + hm(j.ended || j.eta)) : ""}
          ${["scheduled", "waiting", "failed"].includes(j.state) ? bar("scheduled", new Date(new Date(j.eta).getTime() - (new Date(j.planned_end) - new Date(j.planned))).toISOString().slice(0, 19), j.eta, "projected to finish " + hm(j.eta)) : ""}
          ${deadlines}<div class="gnow" style="left:${pos(b.now)}%"></div></div></div>`).join("")}</div>`;
};

// ------------------------------------------------------------------ requests
routes.requests = async () => {
  const rows = (await api("GET", "/tickets?type=request"));
  main().innerHTML = `<div class="spread"><div><h1>Service requests</h1><div class="sub">Worked by the Service Request Agent: validate, collect approvals, fulfil, close</div></div><button class="btn primary" id="newReq">Raise a request</button></div>
    ${table(["Request", "Requested by", "Stage", "State"], rows.map((t) => `<tr class="click" onclick="location.hash='#/ticket/${esc(t.id)}'"><td><b>${esc(t.id)}</b><br>${esc(t.title)}</td><td>${esc(t.requester)}</td><td>${badge(t.stage)}</td><td>${badge(t.state, STATE_CLS[t.state])}</td></tr>`), "No requests yet. Raise one to see the agent work it.")}`;
  $("#newReq").onclick = () => {
    const draw = (cid) => {
      const c = META.requests.find((x) => x.id === cid);
      $("#reqFields").innerHTML = c.fields.map((f) => `<label>${esc(f.label)}</label>${f.options ? `<select data-f="${f.id}">${f.options.map((o) => `<option value="${esc(o)}">${esc(META.nodes[o] || o)}</option>`).join("")}</select>` : `<input data-f="${f.id}">`}`).join("");
    };
    modal(`<h2>Raise a request</h2><label>Catalogue item</label><select id="reqCat">${META.requests.map((c) => `<option value="${c.id}">${esc(c.title)}</option>`).join("")}</select>
      <label>Requested by</label><input id="reqBy" value="Business user"><div id="reqFields"></div>
      <div class="row" style="margin-top:12px"><button class="btn primary" id="reqGo">Submit</button><button class="btn ghost" onclick="closeModal()">Cancel</button></div>`);
    draw(META.requests[0].id);
    $("#reqCat").onchange = (e) => draw(e.target.value);
    $("#reqGo").onclick = async () => {
      const fields = Object.fromEntries($$("[data-f]").map((el) => [el.dataset.f, el.value]));
      const r = await guard(() => api("POST", "/requests", { catalogue: $("#reqCat").value, fields, requester: $("#reqBy").value }), "Request raised");
      closeModal(); location.hash = "#/ticket/" + r.id;
    };
  };
};

// ------------------------------------------------------------------ data needed from tools
routes.data = async () => {
  const tab = store.get("owai_dtab", "tools");
  const d = await api("GET", "/signals");
  const TYPE = { Log: "b-info", Telemetry: "b-ok", Audit: "b-violet", Metadata: "" };
  const feeds = d.tools.flatMap((t) => t.feeds.map((f) => ({ ...f, tool: t.tool })));
  const count = (ty) => feeds.filter((f) => f.type === ty).length;
  const body = {
    tools: () => d.tools.map((t) => `<div class="card"><div class="spread"><h3 style="margin:0">${esc(t.tool)}</h3>${t.received ? badge(t.received + (t.received === 1 ? " report" : " reports") + " read tonight", "b-info") : ""}</div>
      <div class="muted small" style="margin:2px 0 10px"><b>Access needed:</b> ${esc(t.access)}</div>
      ${table(["Feed", "Type", "What it tells the agents", "Used by", "What that makes possible"], t.feeds.map((f) => `<tr><td><b>${esc(f.name)}</b></td><td>${badge(f.type, TYPE[f.type])}</td><td>${esc(f.gives)}</td><td class="small">${f.agents.map((a) => esc(a)).join("<br>")}</td><td>${esc(f.enables)}</td></tr>`))}</div>`).join(""),
    agents: () => d.agents.map((name) => { const mine = feeds.filter((f) => f.agents.includes(name.replace(/ Agent$/, ""))); return mine.length ? `<div class="card"><h3 style="margin-top:0">${esc(name)} <span class="muted small">${mine.length} feeds from ${new Set(mine.map((f) => f.tool)).size} tools</span></h3>
      ${table(["Tool", "Feed", "Type", "What the agent does with it"], mine.map((f) => `<tr><td><b>${esc(f.tool)}</b></td><td>${esc(f.name)}</td><td>${badge(f.type, TYPE[f.type])}</td><td>${esc(f.enables)}</td></tr>`))}</div>` : ""; }).join(""),
    tonight: () => `<div class="card"><div class="funnel"><span><b>${d.watch.raw || 0}</b> tool reports read</span>→<span><b>${d.watch.alerts || 0}</b> alerting</span>→<span><b>${d.watch.events || 0}</b> after merging repeats</span>→<span><b>${new Set(d.events.map((e) => e.incident).filter(Boolean)).size}</b> incidents</span></div></div>
      ${table(["First seen", "Tool", "Alert", "Codes", "Seen", "What the agents did with it", "State"], d.events.map((e) => `<tr><td class="mono">${hm(e.first_seen)}</td><td>${esc(e.source)}</td><td>${esc(e.message)}</td><td class="mono small">${esc(e.codes.join(" "))}</td><td>×${e.count}</td>
      <td class="small">${esc(e.handling)} ${e.incident ? `<a href="#/ticket/${esc(e.incident)}">${esc(e.incident)}</a>` : ""}</td><td>${badge(e.status, e.status === "cleared" ? "b-ok" : e.status === "suppressed" ? "b-violet" : e.severity === "warning" ? "b-warn" : "b-bad")}</td></tr>`), "No alerts yet tonight.")}`,
  };
  main().innerHTML = `<h1>Data needed from tools</h1><div class="sub">The logs, telemetry, audit data and metadata each tool must provide for the agents to work. All of it is read-only status and metadata: no business data rows leave the client's systems.</div>
    <div class="grid g4"><div class="card kpi"><div class="v">${d.tools.length}</div><div class="l">Tools connected</div></div><div class="card kpi"><div class="v">${count("Log")}</div><div class="l">Log feeds</div></div>
      <div class="card kpi"><div class="v">${count("Telemetry")}</div><div class="l">Telemetry feeds</div></div><div class="card kpi"><div class="v">${count("Audit") + count("Metadata")}</div><div class="l">Audit and metadata feeds</div></div></div>
    <div class="tabs" style="margin-top:16px">${[["tools", "By tool"], ["agents", "By agent"], ["tonight", "Read tonight"]].map(([id, l]) => `<div class="tab ${tab === id ? "active" : ""}" data-tab="${id}">${l}</div>`).join("")}</div>${body[tab]()}`;
  $$("[data-tab]").forEach((el) => { el.onclick = () => { store.set("owai_dtab", el.dataset.tab); render(); }; });
};

// ------------------------------------------------------------------ knowledge
routes.knowledge = async () => {
  const k = await api("GET", "/knowledge");
  const tab = store.get("owai_ktab", "ke") === "actions" ? "actions" : "ke";
  const cls = { standard: "b-ok", approval: "b-warn" };
  const body = {
    ke: () => k.known_errors.map((e) => `<details class="ke"><summary>${esc(e.id)} · ${esc(e.title)} <span class="muted">· ${esc(e.tool)}${e.learned_from ? " · learned from " + esc(e.learned_from) : ""}${e.symptom ? " · symptom" : ""}</span></summary>
        <p><b>Matches:</b> <span class="mono small">${esc(e.signatures.join(", "))}</span></p><p><b>Cause:</b> ${esc(e.cause)}</p>
        ${(e.checks || []).length ? `<p><b>Checks the Diagnosis Agent runs (read-only)</b></p><ul>${e.checks.map((c) => `<li>${esc(c.says)} <span class="muted mono small">${esc(c.probe)}</span></li>`).join("")}</ul>` : ""}
        <p><b>Runbook</b></p><ol>${e.plan.map((s) => { const a = k.actions.find((x) => x.id === s.action); return `<li>${esc(s.title)} ${s.kind === "manual" ? badge("person") : s.kind === "verify" ? badge("agent verifies", "b-info") : a ? badge(a.class === "approval" ? "needs " + a.approver_role.replace(/_/g, " ") : "standard", cls[a.class]) : ""}${s.guidance ? `<div class="muted small">${esc(s.guidance)}</div>` : ""}</li>`; }).join("")}<li>Restart failed downstream jobs and confirm recovery ${badge("standard", "b-ok")}</li></ol>
        ${e.permanent_fix ? `<p><b>Permanent fix:</b> ${esc(e.permanent_fix)}</p>` : ""}</details>`).join(""),
    actions: () => `<p class="muted">The only things an agent can change in the client estate. Everything else is done by a person.</p>
      ${table(["Action", "Tool", "Who must say yes", "How it is done"], k.actions.map((a) => `<tr><td><b>${esc(a.title)}</b><div class="muted small">${esc(a.why)}</div></td><td>${esc(a.tool)}</td><td>${a.class === "approval" ? badge(a.approver_role.replace(/_/g, " ") + " approval", "b-warn") : badge("engineer accepts the plan", "b-ok")}</td><td class="small mono">${esc(a.how)}</td></tr>`))}
      <h2>Never done by an agent</h2>${table(["", "Why"], k.never.map((n) => `<tr><td><b>${esc(n.title)}</b></td><td>${esc(n.why)}</td></tr>`))}`,
  };
  main().innerHTML = `<h1>Known errors and runbooks</h1><div class="sub">What the agents know and what they are allowed to do. Edit the YAML files under <code>operatewithai/knowledge</code> for your client.</div>
    <div class="tabs">${[["ke", `Known errors (${k.known_errors.length})`], ["actions", "Action catalogue"]].map(([id, l]) => `<div class="tab ${tab === id ? "active" : ""}" data-tab="${id}">${l}</div>`).join("")}</div>${body[tab]()}`;
  $$("[data-tab]").forEach((el) => { el.onclick = () => { store.set("owai_ktab", el.dataset.tab); render(); }; });
};

// ------------------------------------------------------------------ reports
routes.reports = async () => {
  const tab = store.get("owai_rtab", "health");
  const r = await api("GET", "/reports/" + tab);
  const pct = (v) => (v === null ? "-" : v + "%");
  const body = {
    health: () => `<div class="grid g3"><div class="card kpi"><div class="v">${dot("green")}${r.summary.green}</div><div class="l">Healthy</div></div><div class="card kpi"><div class="v">${dot("amber")}${r.summary.amber}</div><div class="l">Watch</div></div><div class="card kpi"><div class="v">${dot("red")}${r.summary.red}</div><div class="l">In incident</div></div></div>
      ${table(["", "System", "Tower", "Where", "Finding"], r.systems.map((s) => `<tr><td>${dot(s.status)}</td><td><b>${esc(s.name)}</b><div class="muted small mono">${esc(s.ci)}</div></td><td>${esc(s.tower)}</td><td>${esc(s.cloud)}</td><td>${esc(s.detail)}</td></tr>`))}`,
    handover: () => `<div class="card">${md(r.markdown)}</div>`,
    review: () => `<p class="muted">${esc(r.period)}. History is sample data for the demo client; tonight's numbers come from the tickets.</p>
      <div class="grid g4"><div class="card kpi"><div class="v">${r.overall.count}</div><div class="l">Incidents</div></div><div class="card kpi"><div class="v">${pct(r.overall.respond_pct)}</div><div class="l">Response SLA met</div></div>
        <div class="card kpi"><div class="v">${pct(r.overall.resolve_pct)}</div><div class="l">Resolution SLA met</div></div><div class="card kpi"><div class="v">${r.overall.mttr} min</div><div class="l">Mean time to resolve</div></div></div>
      <div class="grid g2"><div class="card"><h3 style="margin-top:0">By priority</h3>${table(["", "Count", "Response SLA", "Resolution SLA", "Mean ack", "Mean resolve"], Object.entries(r.by_priority).map(([p, s]) => `<tr><td>${pri(p)}</td><td>${s.count}</td><td>${pct(s.respond_pct)}</td><td>${pct(s.resolve_pct)}</td><td>${s.mtta ?? "-"} min</td><td>${s.mttr ?? "-"} min</td></tr>`))}
        <h3>Tonight against the 30-day history</h3>${table(["", "Incidents", "Mean ack", "Mean resolve"], [["Last 30 days", r.history], ["Tonight", r.tonight]].map(([l, s]) => `<tr><td>${l}</td><td>${s.count}</td><td>${s.mtta ?? "-"} min</td><td>${s.mttr ?? "-"} min</td></tr>`))}</div>
        <div class="card"><h3 style="margin-top:0">Top known errors</h3>${table(["Known error", "Count", "Total minutes"], r.top_known_errors.map((k) => `<tr><td><b>${esc(k.ke)}</b> ${esc(k.title)}</td><td>${k.count}</td><td>${k.minutes}</td></tr>`))}
          <h3>By tower</h3>${table(["Tower", "Incidents"], r.by_tower.map(([t, c]) => `<tr><td>${esc(t)}</td><td>${c}</td></tr>`))}</div></div>
      <div class="card"><h3 style="margin-top:0">Tonight: who did the work</h3><div class="grid g4">
        <div class="kpi"><div class="v">${r.tonight_work.tool_reports_read}</div><div class="l">Tool reports read by agents</div></div><div class="kpi"><div class="v">${r.tonight_work.agent_actions}</div><div class="l">Agent actions logged</div></div>
        <div class="kpi"><div class="v">${r.tonight_work.human_decisions}</div><div class="l">Human decisions logged</div></div><div class="kpi"><div class="v">${r.tonight_work.steps_by_agents} / ${r.tonight_work.steps_by_people}</div><div class="l">Runbook steps by agents / by people</div></div></div></div>
      ${r.problems.length ? `<div class="card"><h3 style="margin-top:0">Problem records</h3>${table(["Problem", "Occurrences", "Stage"], r.problems.map((p) => `<tr class="click" onclick="location.hash='#/ticket/${esc(p.id)}'"><td><b>${esc(p.id)}</b> ${esc(p.title)}</td><td>${p.occurrences}</td><td>${badge(p.stage)}</td></tr>`))}</div>` : ""}`,
  };
  main().innerHTML = `<h1>Reports</h1><div class="sub">Written by the Service Reporting Agent from tickets, the audit log and the batch projection</div>
    <div class="tabs">${[["health", "Morning health check"], ["handover", "Shift handover"], ["review", "Service review"]].map(([id, l]) => `<div class="tab ${tab === id ? "active" : ""}" data-tab="${id}">${l}</div>`).join("")}</div>${body[tab]()}`;
  $$("[data-tab]").forEach((el) => { el.onclick = () => { store.set("owai_rtab", el.dataset.tab); render(); }; });
};

// ------------------------------------------------------------------ team
routes.team = async () => {
  const t = await api("GET", "/team");
  main().innerHTML = `<h1>Agents and people</h1><div class="sub">Agents watch, correlate, check, draft and carry out agreed runbook steps. People make every decision that changes the client's systems, spends money, touches business data or speaks to the client.</div>
    <h2 style="margin-top:0">How an incident moves</h2>
    <div class="flowrow">${t.flow.map(([s, k, w, what]) => `<div class="fstep ${k}"><b>${esc(s)}</b><span class="who ${k === "person" ? "hu" : "ai"}">${k === "person" ? "Person" : k === "both" ? "Agent + person" : "Agent"}</span><div>${esc(w)}</div><div class="muted">${esc(what)}</div></div>`).join("")}</div>
    <p class="muted small">If a person does not answer within the response time, the ask goes to the incident manager and then to the service delivery manager.</p>
    <h2>The agent team</h2>
    <div class="grid g3">${t.agents.map((a) => { const s = t.stats[a.name]; return `<div class="card agent"><h3>${esc(a.name)}</h3><div class="muted small">${esc(a.seat)}</div><p>${esc(a.mission)}</p>
      <div class="row small">${badge(s.actions + " actions tonight", "b-info")} ${s.open_asks ? badge(s.open_asks + " waiting on people", "b-warn") : ""}</div>
      ${s.last ? `<div class="muted small" style="margin-top:6px">Last: ${hm(s.last.at)} ${esc(s.last.action)}</div>` : ""}
      ${a.reads.length ? `<div class="lbl">Works from</div><div class="small">${esc(a.reads.join(", "))} · <a href="#/data">feeds</a></div>` : ""}
      <div class="lbl">Responsibilities</div><ul>${a.does.map((d) => `<li>${esc(d)}</li>`).join("")}</ul>
      <div class="lbl">May do alone</div><div class="small">${esc(a.alone)}</div>
      <div class="lbl">Never</div><ul>${a.never.map((d) => `<li>${esc(d)}</li>`).join("")}</ul>
      ${a.asks.length ? `<div class="lbl">Brings in</div><ul>${a.asks.map(([w, y]) => `<li><b>${esc(w)}</b> ${esc(y)}</li>`).join("")}</ul>` : ""}</div>`; }).join("")}</div>
    <h2>The people and what they decide</h2>
    ${table(["Seat", "Covers", "Decides", "Waiting on them now"], t.people.map((p) => `<tr><td><a href="#/inbox" data-as="${esc(p.name)}"><b>${esc(p.name)}</b></a></td><td>${esc(p.covers || p.group)}</td><td>${esc(p.decides)}</td><td>${p.open ? badge(p.open, "b-warn") : ""}</td></tr>`))}`;
};

// ------------------------------------------------------------------ audit
routes.audit = async () => {
  const a = await api("GET", "/audit?limit=500");
  main().innerHTML = `<h1>Audit log</h1><div class="sub">Every agent action and human decision, hash-chained. ${a.chain_intact ? badge("chain intact", "b-ok") : badge("chain broken", "b-bad")}</div>
    ${table(["Time", "Who", "Ticket", "What", "Detail"], a.entries.map((e) => `<tr><td class="mono">${hm(e.at)}</td><td>${who(e.actor, e.actor_type === "person")}</td><td>${/^(INC|RITM|PRB)/.test(e.ref) ? `<a href="#/ticket/${esc(e.ref)}">${esc(e.ref)}</a>` : esc(e.ref)}</td><td>${esc(e.action)}</td><td class="small muted">${esc(e.detail === e.action ? "" : e.detail)}</td></tr>`))}`;
};

// ------------------------------------------------------------------ simulator
routes.simulator = async () => {
  META = await api("GET", "/meta");
  main().innerHTML = `<h1>Scenario simulator</h1><div class="sub">A simulated night at the demo client. Tools report in their own formats; nothing here is real client data. Pick what goes wrong and start the night. The live run then shows each agent's output in turn and waits for the right person before the next agent takes over.</div>
    <div class="card"><div class="row">${Object.entries(META.presets).map(([id, p]) => `<button class="btn" data-preset="${id}">${esc(p.title)}</button>`).join("")}<span class="sp"></span>
      <select id="simMode" style="width:auto"><option value="guided">Step by step: a person confirms every hand-off</option><option value="fast">Fast: agents hand off, people approve plans and fixes</option></select>
      <button class="btn primary" id="simGo">Start a new night</button></div>
      ${META.scenarios.map((s) => `<label class="scn"><input type="checkbox" value="${s.id}" ${META.running.includes(s.id) ? "checked" : ""}><span><b>${s.id} · ${esc(s.title)}</b> ${s.tools.map((t) => badge(t)).join(" ")}<br><span class="muted">${esc(s.story)}</span></span></label>`).join("")}
      <p class="muted small">Known errors the team has added stay in place across nights, so running S08 twice shows the second occurrence being recognised.</p></div>`;
  $$("[data-preset]").forEach((b) => { b.onclick = () => $$(".scn input").forEach((c) => { c.checked = META.presets[b.dataset.preset].scenarios.includes(c.value); }); });
  $("#simGo").onclick = async () => {
    await guard(() => api("POST", "/sim/start", { scenarios: $$(".scn input:checked").map((c) => c.value), mode: $("#simMode").value }), "Night started");
    META = await api("GET", "/meta"); location.hash = "#/run";
  };
};

// ------------------------------------------------------------------ live run, step by step
routes.run = async () => {
  const r = await api("GET", "/run");
  const LBL = { done: "done", waiting: "waiting for a person", working: "agent working", blocked: "stopped", pending: "", skipped: "not needed" };
  const stage = (st, i) => `<div class="stage ${st.state}"><div class="num">${i + 1}</div><div>
      <h4>${esc(st.title)} <span class="who ai">Agent</span><span style="font-weight:400">${esc(st.agent)}</span> ${badge(LBL[st.state], { done: "b-ok", waiting: "b-warn", working: "b-info", blocked: "b-bad" }[st.state])}</h4>
      <div class="muted small">${esc(st.does)}</div>
      ${st.output.length ? `<ul>${st.output.map((o) => `<li>${esc(o)}</li>`).join("")}</ul>` : ""}
      ${st.people ? `<div class="hum"><span class="who hu">Person</span>${esc(st.people)}</div>` : ""}
      ${st.asks.map((n) => askCard(n, { asSeat: true, noLink: true })).join("")}</div></div>`;
  const busy = (i) => i.state !== "Resolved" || i.stages.some((st) => st.asks.length);
  const issue = (i) => { const shown = i.stages.filter((st) => !["pending", "skipped"].includes(st.state)); return `<div class="card">
      <div class="spread"><h3 style="margin:0">${i.priority ? pri(i.priority) + " " : ""}${i.ticket ? `<a href="#/ticket/${esc(i.ticket)}">${esc(i.ticket)}</a> · ` : ""}${esc(i.title)}</h3>${badge(i.state, STATE_CLS[i.state] || "b-warn")}</div>
      <div class="rail">${i.stages.map((st, n) => `<span class="${st.state}">${n + 1} ${esc(st.title)}</span>`).join("")}</div>
      ${!busy(i) ? `<details><summary class="small muted">Show the ${shown.length} completed stages</summary>${shown.map((st) => stage(st, i.stages.indexOf(st))).join("")}</details>`
        : i.state === "Resolved" ? `<details><summary class="small muted">Show the completed stages</summary>${shown.filter((st) => !st.asks.length).map((st) => stage(st, i.stages.indexOf(st))).join("")}</details>${shown.filter((st) => st.asks.length).map((st) => stage(st, i.stages.indexOf(st))).join("")}`
          : shown.map((st) => stage(st, i.stages.indexOf(st))).join("")}</div>`; };
  const open = r.issues.filter(busy), closed = r.issues.filter((i) => !busy(i));
  const next = r.waiting ? `<div class="nextbar hu"><div><b>${r.waiting} decision${r.waiting > 1 ? "s" : ""} waiting on people.</b><div class="small">Each button below answers as the role the agent asked. The next agent starts as soon as the person has decided.</div></div></div>`
    : r.over ? `<div class="nextbar ok"><div><b>The night is over.</b><div class="small">${r.services.map((x) => `${esc(x.name)}: ${esc(x.status)}`).join(" · ")}</div></div><a class="btn" href="#/simulator">Start another night</a></div>`
      : `<div class="nextbar"><div><b>${r.issues.length ? "The agents are working; nothing is waiting on a person." : "Quiet so far: the Watch Desk Agent has found nothing to report."}</b><div class="small">Move the clock to the next thing that happens.</div></div><button class="btn primary" id="runNext">Continue to the next event</button></div>`;
  main().innerHTML = `<div class="spread"><div><h1>Live run</h1><div class="sub">${r.guided ? "Step by step: a person confirms every hand-off from one agent to the next" : "Fast mode: agents hand off to each other; people approve plans, fixes and messages"} · ${r.watch.raw || 0} tool reports read so far</div></div>
      <div class="row">${r.services.map((x) => `<span title="${esc(x.name)} · due ${hm(x.deadline)} · estimate ${hm(x.eta)}">${badge(x.name.split(" (")[0] + ": " + x.status, SVC_CLS[x.status])}</span>`).join(" ")}</div></div>
    ${r.scenarios.length ? `<details style="margin-bottom:12px"><summary class="small muted">What goes wrong tonight (${r.scenarios.map((x) => x.id).join(", ")})</summary>${r.scenarios.map((x) => `<p class="small"><b>${esc(x.title)}.</b> ${esc(x.story)}</p>`).join("")}</details>` : ""}
    ${next}
    ${r.prevented.length ? `<div class="nextbar ok"><div><b>Prevented: the Oracle service account never expired.</b><div class="small">The Service Request Agent raised the rotation 9 days early and the Security Lead approved it, so the GoldenGate and Informatica failures of scenario S03 did not happen.</div></div></div>` : ""}
    ${open.map(issue).join("")}
    ${r.other_asks.length ? `<h2>Other decisions waiting</h2>${r.other_asks.map((n) => askCard(n, { asSeat: true })).join("")}` : ""}
    ${closed.length ? `<h2>Resolved tonight (${closed.length})</h2>${closed.map(issue).join("")}` : ""}`;
  const btn = $("#runNext");
  if (btn) btn.onclick = async () => { btn.disabled = true; btn.textContent = "Moving the clock…"; await guard(() => api("POST", "/sim/next")); PULSE = ""; render(true); };
};

// ------------------------------------------------------------------ boot
function setMe(name) { store.set("owai_me", name); $("#personPick").value = name; PULSE = ""; render(); }
(async function boot() {
  const theme = store.get("owai_theme", "");
  if (theme) document.documentElement.dataset.theme = theme;
  $("#themeToggle").onclick = () => { const t = document.documentElement.dataset.theme === "dark" ? "light" : "dark"; document.documentElement.dataset.theme = t; store.set("owai_theme", t); };
  try { META = await api("GET", "/meta"); } catch (e) { main().innerHTML = `<div class="card"><div class="notice bad">${esc(e.message)}</div></div>`; return; }
  $("#version").textContent = "v" + META.version;
  const pick = $("#personPick");
  const group = (label, list) => `<optgroup label="${label}">${list.map((p) => `<option value="${esc(p.name)}">${esc(p.name)}</option>`).join("")}</optgroup>`;
  pick.innerHTML = group("On-call engineers", META.people.filter((p) => p.tower)) + group("Managed service leads", META.people.filter((p) => p.role && !/client|data_owner/.test(p.role))) + group("Client", META.people.filter((p) => /client|data_owner/.test(p.role || "")));
  if (META.me) { pick.value = META.me; pick.disabled = true; } else {
    if (!META.people.some((p) => p.name === store.get("owai_me", ""))) {
      const o = await api("GET", "/overview");
      store.set("owai_me", META.people.some((p) => p.name === "Oracle DBA") ? "Oracle DBA" : o.waiting[0]?.[0] || META.people[0].name);
    }
    pick.value = store.get("owai_me");
    pick.onchange = () => setMe(pick.value);
  }
  render();
})();
