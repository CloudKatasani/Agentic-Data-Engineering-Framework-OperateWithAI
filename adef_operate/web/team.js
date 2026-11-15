/* Human-in-the-loop views: My inbox, Agent team and roles, Activity, and the notification
   card used in the inbox and the case room. Loaded after app.js and reuses its helpers. */
"use strict";

const KIND_LABEL = { acknowledge: "Acknowledge", review: "Review", decide: "Decide", run: "Run it", confirm: "Confirm", fyi: "For your information", escalation: "Escalation" };
const KIND_BADGE = { acknowledge: "b-info", review: "b-violet", decide: "b-warn", run: "b-warn", confirm: "b-info", fyi: "", escalation: "b-bad" };
const PRIO_BADGE = { P1: "b-bad", P2: "b-warn", P3: "b-info", P4: "" };
const AGENT_NAME = { commander: "Commander", onboarding: "Service Onboarding", sentinel: "Signal Sentinel", diagnosis: "Incident Diagnosis",
  remediation: "Remediation", finops: "FinOps", governance: "Governance", knowledge: "Problem and Knowledge", notifier: "Commander" };
const CAUSE_LIST = ["source data", "upstream outage", "code defect", "configuration", "capacity", "platform", "access", "unknown"];
let PEOPLE = [];

const roleLabel = (r) => String(r || "").replace(/_/g, " ");
function dueLabel(n) {
  if (!n.due_at || !["open", "acknowledged"].includes(n.status)) return "";
  const mins = Math.round((new Date(n.due_at) - Date.now()) / 60000);
  return mins < 0 ? `<span class="badge b-bad">overdue ${-mins} min</span>` : `<span class="badge">respond within ${mins < 120 ? `${mins} min` : `${Math.round(mins / 60)} h`}</span>`;
}

async function loadPeople() {
  try { PEOPLE = await api("GET", "/people"); } catch { PEOPLE = []; }
  const sel = $("#personPick");
  if (!sel) return;
  if (WHO?.person) { sel.innerHTML = `<option>${esc(WHO.person)}</option>`; sel.disabled = true; return; }
  const cur = store.get("op_person", "");
  sel.innerHTML = `<option value="">Everyone (supervisor view)</option>` + PEOPLE.map((p) =>
    `<option value="${esc(p.name)}" ${p.name === cur ? "selected" : ""}>${esc(p.name)}${p.open ? ` (${p.open})` : ""}</option>`).join("");
}

// One notification, with the actions it asks for. `ctx.options` lazily filled for choose-option.
function notificationCard(n, compact = false) {
  const open = ["open", "acknowledged"].includes(n.status);
  const acts = !open ? "" : n.actions.map((a) => {
    if (a === "acknowledge") return `<button class="btn small primary" data-nact="acknowledge">Acknowledge</button>`;
    if (a === "council-agree") return `<button class="btn small ok" data-nact="council-agree">Agree with the diagnosis</button>`;
    if (a === "council-disagree") return `<button class="btn small bad" data-nact="council-disagree">Disagree</button>`;
    if (a === "approve") return `<button class="btn small ok" data-nact="approve">Approve</button>`;
    if (a === "reject") return `<button class="btn small bad" data-nact="reject">Reject</button>`;
    if (a === "run-done") return `<button class="btn small primary" data-nact="run-done">I ran it - verify now</button>`;
    if (a === "close") return `<button class="btn small primary" data-nact="close">Close the case</button>`;
    if (a === "confirm-cause") return `<select data-cause>${CAUSE_LIST.map((c) => `<option ${c === n.link?.suggested_cause ? "selected" : ""}>${c}</option>`).join("")}</select>
      <button class="btn small primary" data-nact="confirm-cause">Confirm cause</button>`;
    if (a === "choose-option") return `<div class="opts" data-opts="${esc(n.case_id)}"><span class="small muted">Loading options…</span></div>`;
    return "";
  }).join(" ");
  const who = `<span class="who ai">AI</span>${esc(AGENT_NAME[n.from_agent] || n.from_agent)} → <span class="who hu">Human</span><b>${esc(n.to_person)}</b> <span class="small muted">(${esc(roleLabel(n.to_role))})</span>`;
  return `<div class="card ntf ${open ? "" : "done"} k-${esc(n.kind)}" data-ntf="${esc(n.id)}" data-dp="${esc(n.product)}" data-case="${esc(n.case_id || "")}" data-req="${esc(n.request_id || "")}" data-act-id="${esc(n.link?.act || "")}">
    <div class="spread"><div>${badge("", n.priority, PRIO_BADGE[n.priority])} ${badge("", KIND_LABEL[n.kind] || n.kind, KIND_BADGE[n.kind])}
      ${n.shadow ? badge("", "shadow", "mode-shadow") : ""} <b>${esc(n.title)}</b></div>
      <div class="row">${dueLabel(n)}${open ? "" : badge(n.status === "done" ? "approved" : "", n.status)}</div></div>
    ${compact ? "" : `<div class="small" style="margin:4px 0 6px">${who} · <span class="muted">${esc(n.product)}${n.case_id ? ` · <a href="#/case/${esc(n.product)}/${esc(n.case_id)}">${esc(n.case_id)}</a>` : ""} · ${fmtTime(n.created_at)}</span></div>`}
    ${n.body ? `<div class="small muted">${esc(n.body)}</div>` : ""}
    ${open && n.actions.length ? `<div class="row" style="margin-top:10px">${acts}
      <input data-note placeholder="Note (required to reject or dismiss)" style="max-width:280px">
      <a href="#" class="small" data-nact="dismiss">Dismiss…</a></div>` : ""}
    ${!open && n.history.length ? `<div class="small muted" style="margin-top:6px">${n.history.map((h) => `${esc(h.event)} by ${esc(h.by)}${h.note ? `: ${esc(h.note)}` : ""}`).join(" · ")}</div>` : ""}
  </div>`;
}

async function fillOptions(root) {
  for (const el of $$("[data-opts]", root)) {
    const card = el.closest("[data-ntf]");
    try {
      const d = await api("GET", `/cases/${encodeURIComponent(card.dataset.case)}?dp=${encodeURIComponent(card.dataset.dp)}`);
      const opts = d.options.filter((o) => o.kind !== "trade-off");
      el.innerHTML = `<div class="small muted" style="margin-bottom:4px">Choose a fix. Choosing records your approval when you hold the gate's role.</div>` +
        opts.map((o) => `<label class="chk"><input type="radio" name="opt-${esc(card.dataset.ntf)}" value="${o.no}" ${o.recommended ? "checked" : ""}>
          <span><b>#${o.no} ${esc(o.title)}</b> ${o.recommended ? badge("", "recommended", "b-ok") : ""}<br><span class="small muted">${esc(o.effect)} · risk: ${esc(o.risk)} · ${esc(o.reversibility)} · ${esc(o.approval)}</span></span></label>`).join("") +
        `<button class="btn small primary" data-nact="choose-option" style="margin-top:6px">Choose this fix</button>`;
    } catch (e) { el.innerHTML = `<span class="small" style="color:var(--bad)">${esc(e.message)}</span>`; }
  }
}

async function handleNotificationClick(e, after) {
  const b = e.target.closest("[data-nact]");
  if (!b) return false;
  e.preventDefault();
  const card = b.closest("[data-ntf]");
  const action = b.dataset.nact;
  const note = $("[data-note]", card)?.value || "";
  let by = WHO?.person || store.get("op_person", "");
  if (!by) { by = prompt("Who is acting? (your name as recorded for this product)") || ""; if (!by) return true; }
  const body = { action, by, note };
  if (action === "choose-option") body.option = Number($(`input[name="opt-${card.dataset.ntf}"]:checked`, card)?.value);
  if (action === "confirm-cause") body.cause_class = $("[data-cause]", card).value;
  if (action === "run-done") body.act_id = card.dataset.actId;
  if (action === "dismiss" && !note) { toast("Give a reason to dismiss", "err"); return true; }
  if (action === "reject" && !note) { toast("Give a reason to reject (it is returned word for word)", "err"); return true; }
  const r = await guard(() => api("POST", `/notifications/${card.dataset.dp}/${card.dataset.ntf}/actions`, body));
  toast(`${action.replace("-", " ")}: ${r.state || r.status || r.council || "done"}${r.follow_up?.status ? ` · ${r.follow_up.status}` : ""}`, "ok");
  after();
  return true;
}

// ------------------------------------------------------------------ My inbox
route("inbox", async () => {
  await refreshFleet(); await loadPeople();
  const me = WHO?.person || store.get("op_person", "");
  const view = store.get("op_inbox_view", "open");
  const items = await api("GET", `/notifications?status=${view === "all" ? "all" : "open"}${me ? `&person=${encodeURIComponent(me)}` : ""}`);
  const open = items.filter((n) => ["open", "acknowledged"].includes(n.status));
  const overdue = open.filter((n) => n.due_at && new Date(n.due_at) < Date.now()).length;
  const p1 = open.filter((n) => n.priority === "P1").length;
  main().innerHTML = `<div class="spread"><div><h1>${me ? `Inbox · ${esc(me)}` : "Inbox · everyone"}</h1>
      <div class="sub">What the agents need from ${me ? "you" : "people"}: acknowledge, review, choose a fix, approve, run, confirm. Agents do the read-only work; every fix waits for a person.</div></div>
      <div class="row"><div class="seg">${[["open", "Open"], ["all", "All"]].map(([k, l]) => `<button class="btn small ${view === k ? "primary" : ""}" data-view="${k}">${l}</button>`).join("")}</div>
      <button class="btn small" data-run="1" title="Run monitors and agents now (the scheduler does this automatically when started with --autotick)">Run a cycle now</button></div></div>
    <div class="grid g4"><div class="card kpi"><div class="l">Open asks</div><div class="v">${open.length}</div></div>
      <div class="card kpi"><div class="l">Priority 1</div><div class="v" style="color:${p1 ? "var(--bad)" : "inherit"}">${p1}</div></div>
      <div class="card kpi"><div class="l">Overdue</div><div class="v" style="color:${overdue ? "var(--bad)" : "inherit"}">${overdue}</div><div class="small muted">Overdue asks escalate to the next role</div></div>
      <div class="card kpi"><div class="l">Open cases (fleet)</div><div class="v">${FLEET.totals.open_cases}</div></div></div>
    <div style="margin-top:16px">${items.length ? items.map((n) => notificationCard(n)).join("") : `<div class="card empty">${me ? "Nothing needs you right now." : "Nothing is waiting on anyone."} Inject a fault on a demo product's Service page, or run a cycle.</div>`}</div>`;
  fillOptions(main());
  main().onclick = async (e) => {
    const v = e.target.closest("[data-view]")?.dataset.view;
    if (v) { store.set("op_inbox_view", v); return navigate(); }
    if (e.target.closest("[data-run]")) { const r = await guard(() => api("POST", "/run")); toast(`Cycle: ${Object.keys(r).length} product(s) checked`, "ok"); return navigate(); }
    await handleNotificationClick(e, () => navigate());
  };
});

// ------------------------------------------------------------------ Agent team and roles
route("team", async () => {
  const t = await api("GET", "/team");
  main().innerHTML = `<h1>Agent team and roles</h1>
    <div class="sub">Eight AI agents run operations around the clock; people make every decision that changes something. Each agent states what it owns, what it may never do, and exactly when it brings a person in.</div>
    <div class="card"><h2>How an incident moves</h2><div class="lanes-wrap"><div class="flowline">
      ${t.flow.map((f, i) => `<div class="fstep ${f.who}"><div class="small muted">${i + 1}</div><div class="who ${f.who === "human" ? "hu" : "ai"}">${f.who === "human" ? "Human" : f.who === "both" ? "AI or human" : "AI"}</div>
        <div><b>${esc(f.step)}</b></div><div class="small">${esc(f.agent)}</div><div class="small muted">${esc(f.detail)}</div></div>`).join("")}
    </div></div></div>
    <div class="grid g2" style="margin-top:16px">${t.agents.map((a) => `<div class="card agent-card">
      <div class="spread"><div><span class="badge">${esc(a.stage)}</span> <b style="font-size:16px">${esc(a.name)}</b><div class="small muted">${esc(a.title)} · <code>${esc(a.skill)}</code></div></div>
        <div class="small" style="text-align:right">${a.stats.open_items ? badge("pending", `${a.stats.open_items} open item(s)`) : badge("", "idle")}<br><span class="muted">${a.stats.events} action(s) · ${a.stats.notifications} notification(s)</span></div></div>
      <p style="margin:8px 0">${esc(a.mission)}</p>
      <div class="prof-grid"><div><div class="ph">Responsible for</div><ul>${a.responsibilities.map((r) => `<li>${esc(r)}</li>`).join("")}</ul></div>
        <div><div class="ph ok">Brings in a person</div><ul>${a.humans.map((h) => `<li><b>${esc(roleLabel(h.role))}</b>: ${esc(h.asks)} <span class="muted">(when ${esc(h.when.charAt(0).toLowerCase() + h.when.slice(1))})</span></li>`).join("")}</ul>
          <div class="ph bad">Never</div><ul>${a.never.map((r) => `<li>${esc(r)}</li>`).join("")}</ul></div></div>
      <div class="small muted" style="margin-top:6px">Woken by: ${a.triggers.map(esc).join(", ")} · Produces: ${a.produces.map(esc).join(", ")}${a.gates.length ? ` · Gates: ${a.gates.map(esc).join(", ")}` : ""}</div>
      ${a.stats.last ? `<div class="small" style="margin-top:6px">Last: <a href="#/case/${esc(a.stats.last.product)}/${esc(a.stats.last.case)}">${esc(a.stats.last.case)}</a> ${esc(a.stats.last.event)} <span class="muted">${fmtTime(a.stats.last.at)}</span></div>` : ""}
    </div>`).join("")}</div>
    <div class="card" style="margin-top:16px"><h2>Human roles</h2>${table(["Role", "Held by", "Decides", "Open asks"], t.roles.map((r) => `<tr><td><b>${esc(r.title)}</b><div class="small muted">${esc(r.role)}${r.response ? ` · ${esc(r.response)}` : ""}</div></td>
      <td class="small">${r.holders.map(esc).join("<br>") || '<span class="muted">not named</span>'}</td><td class="small">${r.decides.map(esc).join("<br>")}</td><td>${r.open ? badge("pending", r.open) : "-"}</td></tr>`))}
      <div class="small muted" style="margin-top:8px">Council of three for S1 diagnoses: the Tech Lead, Platform Owner and Data Owner of the product (${t.council_open} open review(s)).</div></div>`;
});

// ------------------------------------------------------------------ Activity
route("activity", async () => {
  const items = await api("GET", "/activity");
  main().innerHTML = `<h1>Activity</h1><div class="sub">Everything the agents did and every decision people made, newest first.</div>
    <div class="card">${items.length ? `<div class="feed" style="max-height:none">${items.map((x) => `<div class="e"><span class="ts">${fmtTime(x.at)}</span> <span class="who ${x.kind === "human" ? "hu" : "ai"}">${x.kind === "human" ? "Human" : "AI"}</span><b>${esc(AGENT_NAME[x.actor] || x.actor)}</b>
      <span class="muted small">${esc(x.product)}${x.case ? ` · <a href="#/case/${esc(x.product)}/${esc(x.case)}">${esc(x.case)}</a>` : ""}</span> ${esc(x.text)}</div>`).join("")}</div>` : '<div class="empty">No activity yet.</div>'}</div>`;
});

// ------------------------------------------------------------------ Case room additions
async function caseWaitingBanner(dp, caseId) {
  const items = (await api("GET", `/notifications?dp=${encodeURIComponent(dp)}`)).filter((n) => n.case_id === caseId);
  if (!items.length) return `<div class="now ok"><div><b>Nothing is waiting on a person for this case.</b><div class="small">Agents keep watching; you will be notified at the next decision.</div></div></div>`;
  return `<div class="card" style="border-left:5px solid var(--warn)"><h2>Waiting on people</h2>${items.map((n) => notificationCard(n, false)).join("")}</div>`;
}
