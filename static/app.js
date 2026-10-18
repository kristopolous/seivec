"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  config: {}, calls: new Map(), selected: null, follow: true, stats: null,
  rules: { columns: [], rows: [] }, learning: new Map(), traces: [], audit: [],
  browserCall: null, lastCount: null,
};

// ---------- helpers ----------
const fmtPhone = (n) => {
  if (!n) return "";
  const d = n.replace(/\D/g, "").replace(/^1/, "");
  return d.length === 10 ? `(${d.slice(0, 3)}) ${d.slice(3, 6)}-${d.slice(6)}` : n;
};
const fmtTime = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" });
const el = (tag, cls, text) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
};
const current = () => state.calls.get(state.selected);
const isLive = (c) => ["listening", "routing", "transferring"].includes(c?.status);
const STATUS_WORDS = {
  listening: "On the line", routing: "Deciding", transferring: "Forwarding", transferred: "Connected",
  handoff: "Handed off", emergency: "Sent to 911", "no-match": "Outside service area", ended: "Ended",
};
const reduced = () => matchMedia("(prefers-reduced-motion: reduce)").matches;

// ---------- socket ----------
function connect() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.onmessage = (m) => handle(JSON.parse(m.data));
  ws.onclose = () => setTimeout(connect, 1200);
}

function handle(msg) {
  switch (msg.type) {
    case "hello":
      state.config = msg.config || {};
      state.calls = new Map(msg.calls.map((c) => [c.id, c]));
      state.traces = msg.traces || [];
      state.rules = msg.rules;
      state.stats = msg.stats;
      if (!state.selected && msg.calls[0]) state.selected = msg.calls[0].id;
      renderConfig(); buildWall(); renderAll();
      break;
    case "call":
    case "lanes":
    case "system1": {
      const isNew = !state.calls.has(msg.call.id);
      state.calls.set(msg.call.id, msg.call);
      if (isNew && state.follow) { state.selected = msg.call.id; state.audit = []; renderAudit(); }
      renderCalls();
      if (msg.call.id === state.selected) renderCall();
      break;
    }
    case "transcript": {
      const c = state.calls.get(msg.call_id);
      if (!c) return;
      c.transcript = msg.transcript;
      if (msg.transcript[0]?.source === "guava") c.transcript_source = "guava";
      if (msg.call_id === state.selected) renderTranscript();
      break;
    }
    case "pipeline": {
      const c = state.calls.get(msg.call_id);
      if (!c) return;
      c.pipeline = msg.pipeline;
      if (msg.call_id === state.selected) { renderPipeline(); narrow(); }
      break;
    }
    case "rules":
      state.rules = msg.rules;
      buildWall();
      break;
    case "learn":
      onLearn(msg);
      break;
    case "trace":
      state.traces.push(msg.trace);
      if (state.traces.length > 300) state.traces.shift();
      addTrace(msg.trace);
      break;
    case "audit":
      if (msg.record.call_id === state.selected) { state.audit.push(msg.record); addAudit(msg.record); }
      break;
    case "stats":
      state.stats = msg.stats;
      renderStats();
      break;
  }
}

// ---------- header ----------
function renderConfig() {
  const c = state.config;
  const info = $("line-info");
  info.replaceChildren();
  if (c.phone) info.append(chip(`Call <b>${fmtPhone(c.phone)}</b>`));
  if (c.sim_only) info.append(chip("Sample calls only", true));
  const mode = {
    handoff: "Ends with the handoff briefing",
    demo: c.demo_number ? `Transfers to the demo phone ${fmtPhone(c.demo_number)}` : "Demo transfers (no demo phone set)",
    live: "Live transfers to real departments",
  }[c.transfer_mode];
  if (mode) info.append(chip(mode, c.transfer_mode === "live"));
  $("browser-call").hidden = !c.webrtc_code;
}
function chip(html, warn) {
  const s = el("span", "chip" + (warn ? " warn" : ""));
  s.innerHTML = html;
  return s;
}

// ---------- calls rail ----------
function renderCalls() {
  const list = $("call-list");
  const calls = [...state.calls.values()].sort((a, b) => b.started_at - a.started_at);
  $("calls-empty").hidden = calls.length > 0;
  list.replaceChildren(...calls.map((c) => {
    const li = el("li", "call-item");
    li.tabIndex = 0;
    li.setAttribute("aria-current", String(c.id === state.selected));
    const dot = el("span", "dot"); dot.dataset.s = c.status;
    const who = el("span", "who", c.caller ? fmtPhone(c.caller) : c.channel === "browser" ? "Browser caller" : "Caller");
    const when = el("span", "when", fmtTime(c.started_at));
    const cat = c.decision?.category?.label;
    const what = el("span", "what", [cat, STATUS_WORDS[c.status]].filter(Boolean).join(", ") + (c.simulated ? " (sample)" : ""));
    li.append(dot, who, when, what);
    const pick = () => { state.selected = c.id; state.follow = isLive(c); loadAudit(); renderAll(); backfillTranscript(c); };
    li.onclick = pick;
    li.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(); } };
    return li;
  }));
}

// ---------- call view ----------
function renderAll() {
  renderCalls(); renderCall(); renderTranscript(); renderPipeline(); renderStats(); renderTraces();
}

function renderCall() {
  const c = current();
  renderSign(c);
  renderLanes(c);
  renderS1(c);
  renderHandoff(c);
  renderReview(c);
  narrow();
  const alert = $("alert");
  alert.hidden = !c?.alert;
  alert.textContent = c?.alert || "";
  $("talk-title").textContent = c ? `${c.caller ? fmtPhone(c.caller) : "Browser caller"}${c.simulated ? ", sample call" : ""}` : "Transcript";
}

function renderSign(c) {
  const sign = $("sign");
  const s = c?.status || "idle";
  const cell = c?.decision?.cell;
  sign.dataset.state = s;
  const set = (k, d, sv, exit) => {
    const changed = $("sign-dept").textContent !== d;
    $("sign-kicker").textContent = k; $("sign-dept").textContent = d; $("sign-service").textContent = sv;
    $("sign-exit").hidden = !exit; $("sign-exit").textContent = exit || "";
    if (changed && !reduced()) { sign.classList.remove("flip"); void sign.offsetWidth; sign.classList.add("flip"); }
  };
  if (!c) return set("Waiting for a call", "Sievec", "Routes you right when time is tight", null);
  if (s === "emergency") return set("Not a transfer", "Hang up and dial 911", "Someone may be in danger", "911");
  if (cell) {
    const k = { transferring: "Forwarding to", transferred: "Connected to", handoff: "Handed off to", ended: "Routed to" }[s] || "Routing to";
    return set(k, cell.department, `${cell.service}, ${c.decision.place?.name || ""}`.replace(/, $/, ""), cell.phone_display);
  }
  if (s === "no-match") return set("Outside this line", c.decision?.place?.name || "Not in LA County", "Caller was told who to call", null);
  if (s === "routing") return set("Deciding", "Checking the table", c.decision?.category?.label || "Matching the request", null);
  const top = c.lanes?.[0];
  return set("Listening", top ? top.label : "What does the caller need?", top ? "Best guess so far" : "Options narrow as the caller talks", null);
}

function renderLanes(c) {
  const lanes = c?.lanes || [];
  $("lanes-empty").hidden = lanes.length > 0;
  const list = $("lanes");
  const existing = new Map([...list.children].map((li) => [li.dataset.id, li]));
  list.replaceChildren(...lanes.map((l) => {
    let li = existing.get(l.id);
    if (!li) {
      li = el("li", "lane");
      li.dataset.id = l.id;
      const bar = el("div", "lane-bar");
      bar.append(el("div", "lane-fill"), el("div", "lane-name", l.label));
      li.append(bar, el("span", "lane-pct"));
    }
    li.classList.toggle("emergency", !!l.emergency);
    li.querySelector(".lane-fill").style.width = `${Math.round(l.score * 100)}%`;
    li.querySelector(".lane-pct").textContent = `${Math.round(l.score * 100)}%`;
    return li;
  }));
}

function renderS1(c) {
  const s = $("s1");
  if (!c?.system1) { s.hidden = true; return; }
  s.hidden = false;
  s.replaceChildren(`System 1 (${state.config.llm?.system1_model || "fast model"}, ${c.system1.latency_ms} ms) thinks: `, el("b", null, c.system1.label));
}

// Past real calls: if our live capture is empty, ask the server to pull
// Guava's own transcript for it.
const backfilled = new Set();
async function backfillTranscript(c) {
  if (!c || c.simulated || isLive(c) || backfilled.has(c.id)) return;
  backfilled.add(c.id);
  await fetch(`/api/calls/${c.id}/transcript/refresh`, { method: "POST" });
}

function renderPlayback(c) {
  const box = $("playback");
  const show = c && !c.simulated && !isLive(c);
  box.hidden = !show;
  if (!show) return;
  const audio = $("recording");
  const src = `/api/calls/${c.id}/recording`;
  if (audio.getAttribute("src") !== src) audio.setAttribute("src", src);
  const dur = c.ended_at ? Math.round(c.ended_at - c.started_at) : null;
  $("playback-note").textContent = [
    "Recording from Guava",
    dur != null ? `${dur} s call` : null,
    c.transcript_source === "guava" ? "transcript from Guava" : null,
  ].filter(Boolean).join(", ");
}

function renderTranscript() {
  const c = current();
  renderPlayback(c);
  const list = $("transcript");
  const atBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 60;
  const lines = c?.transcript || [];
  if (c && !lines.length && !isLive(c)) {
    list.replaceChildren(el("li", "none", "Nothing was said on this call. The caller hung up before speaking."));
    return;
  }
  list.replaceChildren(...lines.map((l) => {
    const li = el("li", `line ${l.role}${l.partial ? " partial" : ""}`);
    li.append(el("span", "who", l.role === "caller" ? "Caller" : "Rosa"), el("span", "text", l.text));
    return li;
  }));
  if (atBottom || lines.length < 4) list.scrollTop = list.scrollHeight;
}

function renderPipeline() {
  const c = current();
  const steps = c?.pipeline || ["Area code", "Location", "Service", "Rule", "Connect"].map((label) => ({ label, state: "pending" }));
  $("pipeline").replaceChildren(...steps.map((st) => {
    const li = el("li", "step");
    li.dataset.state = st.state;
    li.append(el("span", "step-label", st.label));
    li.append(el("span", "step-value", st.value ?? (st.state === "pending" ? "–" : "")));
    if (st.detail) li.append(el("span", "step-detail", st.detail));
    return li;
  }));
}

// ---------- the wall ----------
const groupClass = (key) => key === "la_city" ? "g-city" : key === "la_county" ? "g-county" : "g-learned";

function tileNode(row, col, cell) {
  const t = el("div", "tile");
  t.dataset.row = row.id;
  t.dataset.col = col.key;
  t.append(el("span", "svc", row.label), el("span", "dep", cell.department || ""), el("span", "ph", cell.phone_display || ""));
  t.title = `${cell.department}: ${row.label}, ${cell.phone_display || ""}`;
  return t;
}

function ghostNode(colKey, ev) {
  const t = el("div", "tile ghost");
  t.dataset.row = ev.category;
  t.dataset.col = colKey;
  t.dataset.ghost = "1";
  const words = { queued: "Queued", searching: "Searching the web", reading: "Reading city pages" }[ev.state] || "";
  t.append(el("span", "svc", ev.label), el("span", "dep", words), el("span", "ph", ev.city));
  return t;
}

function buildWall() {
  const { columns, rows } = state.rules;
  const wall = $("wall");
  wall.replaceChildren(...columns.map((col) => {
    const g = el("section", `group ${groupClass(col.key)}`);
    g.dataset.col = col.key;
    const head = el("div", "group-head");
    head.append(el("span", null, col.name), el("span", "n"));
    const tiles = el("div", "tiles");
    rows.filter((row) => !row.emergency).forEach((row) => {
      const cell = row.cells[col.key];
      if (cell) tiles.append(tileNode(row, col, cell));
      else {
        const ev = state.learning.get(`${col.key}|${row.id}`);
        if (ev && !["done", "failed"].includes(ev.state)) tiles.append(ghostNode(col.key, ev));
      }
    });
    g.append(head, tiles);
    return g;
  }));
  state.lastCount = null;
  narrow(true);
}

function filtersFor(c) {
  if (!c) return { cols: null, rows: null, chips: [] };
  const pipe = Object.fromEntries((c.pipeline || []).map((s) => [s.key, s]));
  const chips = [];
  let cols = null, rows = null, soft = false;
  if (pipe.place?.state === "done" && pipe.place.data?.key) {
    cols = new Set([pipe.place.data.key]);
    chips.push(["Location", pipe.place.value]);
  } else if (c.guess?.candidates?.length) {
    cols = new Set(c.guess.candidates);
    soft = true;
    chips.push([`Area code ${c.guess.area_code}`, `${c.guess.candidates.length} jurisdictions`]);
  }
  if (pipe.service?.state === "done" && pipe.service.data?.id) {
    rows = new Set([pipe.service.data.id]);
    chips.push(["Service", pipe.service.value]);
  } else if (c.lanes?.length) {
    rows = new Set(c.lanes.filter((l) => l.score >= 0.08).map((l) => l.id));
    chips.push(["Sounds like", `${rows.size} services`]);
  }
  return { cols, rows, soft, chips, pipe };
}

function narrow(instant) {
  const c = current();
  const { cols, rows, chips, pipe } = filtersFor(c);
  const lead = c && isLive(c) ? c.lanes?.[0]?.id : null;
  const hitRow = pipe?.rule?.state === "done" ? pipe.rule.data?.category : null;
  const hitCol = pipe?.rule?.state === "done" ? pipe.rule.data?.column : null;
  let count = 0;
  document.querySelectorAll("#wall .group").forEach((g) => {
    const colOk = !cols || cols.has(g.dataset.col);
    let n = 0;
    g.querySelectorAll(".tile").forEach((t) => {
      const keep = colOk && (!rows || rows.has(t.dataset.row));
      if (keep && !t.dataset.ghost) n++;
      setOut(t, !keep, instant);
      t.classList.toggle("lead", keep && t.dataset.row === lead && !hitRow);
      const hit = t.dataset.row === hitRow && t.dataset.col === hitCol;
      t.classList.toggle("hit", hit);
      if (hit && !instant) t.scrollIntoView({ block: "nearest", behavior: reduced() ? "auto" : "smooth" });
    });
    count += n;
    g.hidden = !colOk || (n === 0 && !g.querySelector(".tile.ghost:not(.gone)"));
    g.querySelector(".n").textContent = n ? `${n}` : "";
  });
  const counter = $("count");
  if (state.lastCount !== count) {
    counter.textContent = count;
    if (state.lastCount != null && !reduced()) { counter.classList.remove("bump"); void counter.offsetWidth; counter.classList.add("bump"); }
    state.lastCount = count;
  }
  $("count-l").textContent = count === 1 ? "match" : c ? "options left" : "departments and numbers on file";
  $("filters").replaceChildren(...chips.map(([k, v]) => {
    const li = el("li");
    li.append(`${k}: `, el("b", null, v));
    return li;
  }));
}

function setOut(t, out, instant) {
  if (out) {
    if (t.classList.contains("gone")) return;
    t.classList.add("out");
    if (instant || reduced()) t.classList.add("gone");
    else setTimeout(() => t.classList.contains("out") && t.classList.add("gone"), 360);
  } else if (t.classList.contains("out")) {
    t.classList.remove("gone");
    requestAnimationFrame(() => t.classList.remove("out"));
  }
}

function onLearn(ev) {
  const k = `${ev.key}|${ev.category}`;
  state.learning.set(k, ev);
  let group = document.querySelector(`#wall .group[data-col="${CSS.escape(ev.key)}"]`);
  if (!group) return; // the rules event that creates the column will rebuild
  const tiles = group.querySelector(".tiles");
  const existing = tiles.querySelector(`.tile[data-row="${ev.category}"]`);
  if (ev.state === "done") {
    const row = state.rules.rows.find((r) => r.id === ev.category);
    const t = tileNode(row, { key: ev.key }, ev.cell);
    t.classList.add("fresh");
    existing ? existing.replaceWith(t) : tiles.append(t);
  } else if (ev.state === "failed") {
    if (existing?.dataset.ghost) existing.remove();
  } else {
    const g = ghostNode(ev.key, ev);
    existing ? existing.replaceWith(g) : tiles.append(g);
  }
  narrow(true);
}

// ---------- handoff ----------
function renderHandoff(c) {
  const h = c?.decision?.handoff;
  const box = $("handoff");
  box.hidden = !h || !["transferring", "handoff", "transferred", "ended"].includes(c.status);
  if (box.hidden) return;
  $("handoff-to").textContent = `Calling ${h.to.department}, ${h.to.phone_display}`;
  $("handoff-script").textContent = h.script;
  const f = h.facts;
  $("handoff-facts").replaceChildren(...[
    ["Service", f.service], ["Where", `${f.location} (${f.jurisdiction})`], ["Rule", f.rule],
    ["Callback", f.callback ? fmtPhone(f.callback) : "not shared"], ["Reference", f.reference],
    ["Number source", h.to.source_url || "decision table"],
  ].flatMap(([k, v]) => [el("dt", null, k), el("dd", null, v)]));
}
$("play-brief").onclick = () => {
  const h = current()?.decision?.handoff;
  if (!h || !("speechSynthesis" in window)) return;
  speechSynthesis.cancel();
  speechSynthesis.speak(new SpeechSynthesisUtterance(h.script));
};
$("copy-brief").onclick = async () => {
  const h = current()?.decision?.handoff;
  if (h) { await navigator.clipboard.writeText(h.script); $("copy-brief").textContent = "Copied"; setTimeout(() => ($("copy-brief").textContent = "Copy"), 1500); }
};

// ---------- review (feedback loop) ----------
function renderReview(c) {
  const form = $("review");
  const done = c && c.decision?.category && !isLive(c) && c.status !== "emergency";
  form.hidden = !done;
  if (!done) return;
  const pick = $("review-pick");
  if (!pick.options.length) state.rules.rows.forEach((r) => pick.append(new Option(r.label, r.id)));
  pick.value = c.decision.category.id;
  const fb = c.feedback;
  const msg = $("review-done");
  msg.hidden = !fb;
  if (fb) {
    msg.textContent = fb.matched ? "Marked correct."
      : fb.learned ? `Corrected to ${labelOf(fb.correct)}. Sievec learned the phrase "${fb.learned.phrase}", so the next caller who says it goes straight there.`
      : `Corrected to ${labelOf(fb.correct)}.`;
  }
}
const labelOf = (id) => state.rules.rows.find((r) => r.id === id)?.label || id;
async function sendFeedback(correct) {
  const c = current();
  if (!c) return;
  await fetch(`/api/calls/${c.id}/feedback`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ correct }) });
}
$("review-yes").onclick = () => sendFeedback(current()?.decision?.category?.id);
$("review").onsubmit = (e) => { e.preventDefault(); sendFeedback($("review-pick").value); };

// ---------- stats ----------
function renderStats() {
  const s = state.stats;
  if (!s) return;
  $("s-total").textContent = s.total;
  $("s-transferred").textContent = s.transferred;
  $("s-emerg").textContent = s.emergencies;
  $("s-cities").textContent = s.cities_learned;
  const fb = s.feedback;
  $("s-acc").textContent = fb.accuracy == null ? "–" : `${Math.round(fb.accuracy * 100)}%`;
  $("s-acc-l").textContent = fb.reviewed
    ? `accurate across ${fb.reviewed} reviewed${fb.learned_phrases ? `, ${fb.learned_phrases} phrases learned` : ""}`
    : "accuracy, nothing reviewed yet";
  $("s-services").replaceChildren(...s.by_service.map((d) => {
    const li = el("li");
    li.append(el("b", null, String(d.count)), ` ${d.label}`);
    return li;
  }));
}

// ---------- under the hood ----------
function renderConfigPanel(st) {
  const rows = [
    ["Voice platform", "Guava: speech, turn-taking, multiple-choice fields, transfer"],
    ["Phone line", st.phone ? fmtPhone(st.phone) : "not attached"],
    ["Browser line", st.webrtc_code || "not attached"],
    ["System 1 model", `${st.llm?.system1_model}: service type, every utterance`],
    ["System 2 model", `${st.llm?.system2_model}: reads city web pages`],
    ["Model endpoint", st.llm?.endpoint],
    ["Web search", st.web_search ? "Parallel search + extract" : "off (no PARALLEL_API_KEY)"],
    ["Decision table", `${st.rules_file}: ${st.categories} services by ${st.columns} jurisdictions`],
    ["Call ending", st.transfer_mode + (st.demo_number ? ` to ${fmtPhone(st.demo_number)}` : "")],
  ];
  $("hood-config").replaceChildren(...rows.flatMap(([k, v]) => [el("dt", null, k), el("dd", null, v ?? "")]));
}

const traceVisible = (t) => !$("trace-follow").checked || !state.selected || t.call_id === state.selected || !t.call_id;
function traceNode(t, isNew) {
  const li = el("li", "trace" + (isNew ? " new" : ""));
  const d = el("details");
  const s = el("summary");
  const tier = t.error ? "err" : t.kind === "web" ? "web" : t.system === 2 ? "s2" : "s1";
  const tierText = t.error ? "Error" : t.kind === "web" ? "Web" : t.system === 2 ? "System 2" : "System 1";
  s.append(el("span", `tier ${tier}`, tierText), el("span", null, `${t.purpose} (${t.model || t.kind})`), el("span", "ms", `${t.latency_ms} ms`));
  d.append(s, el("div", "pre-label", "Request"), el("pre", null, JSON.stringify(t.request, null, 2)),
    el("div", "pre-label", "Response"), el("pre", null, JSON.stringify(t.response, null, 2)));
  li.append(d);
  return li;
}
function renderTraces() {
  const items = state.traces.filter(traceVisible).slice(-80).reverse();
  $("traces").replaceChildren(...items.map((t) => traceNode(t, false)));
  $("traces-empty").hidden = items.length > 0;
}
function addTrace(t) {
  if ($("hood").hidden || !traceVisible(t)) return;
  $("traces").prepend(traceNode(t, true));
  $("traces-empty").hidden = true;
}

function auditNode(r, isNew) {
  const li = el("li", isNew ? "new" : "");
  li.append(el("span", "ev", r.event), " ",
    el("span", "meta", `#${r.seq} ${fmtTime(r.ts)} ${r.hash.slice(0, 12)}`),
    el("pre", null, JSON.stringify(r.payload, null, 1)));
  return li;
}
function renderAudit() {
  $("audit").replaceChildren(...state.audit.map((r) => auditNode(r, false)));
}
function addAudit(r) {
  if ($("hood").hidden || $("pane-audit").hidden) return;
  $("audit").append(auditNode(r, true));
}
async function loadAudit() {
  const c = current();
  if (!c) return;
  const data = await (await fetch(`/api/calls/${c.id}/audit`)).json();
  state.audit = data.records;
  renderAudit();
  const chain = $("chain");
  chain.className = "chain " + (data.chain.ok ? "ok" : "bad");
  chain.textContent = data.chain.ok
    ? `Hash chain intact: ${data.chain.records} records, head ${data.chain.head.slice(0, 16)}…`
    : `Hash chain broken at record #${data.chain.broken_at}`;
  $("audit-download").href = `/api/calls/${c.id}/audit`;
  $("audit-download").download = `sievec-audit-${c.id}.json`;
}

function selectTab(which) {
  const audit = which === "audit";
  $("tab-audit").setAttribute("aria-selected", String(audit));
  $("tab-payloads").setAttribute("aria-selected", String(!audit));
  $("pane-audit").hidden = !audit;
  $("pane-payloads").hidden = audit;
  if (audit) loadAudit();
}
$("tab-payloads").onclick = () => selectTab("payloads");
$("tab-audit").onclick = () => selectTab("audit");

async function openHood() {
  $("hood").hidden = false;
  $("open-hood").setAttribute("aria-expanded", "true");
  renderConfigPanel(await (await fetch("/api/settings")).json());
  renderTraces();
  if (!$("pane-audit").hidden) loadAudit();
}
const closeHood = () => { $("hood").hidden = true; $("open-hood").setAttribute("aria-expanded", "false"); };
$("open-hood").onclick = () => ($("hood").hidden ? openHood() : closeHood());
$("close-hood").onclick = closeHood;
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("hood").hidden) closeHood(); });
$("trace-follow").onchange = renderTraces;
$("reset-learned").onclick = async () => { await fetch("/api/learned/reset", { method: "POST" }); state.learning.clear(); };

// ---------- sample calls ----------
async function loadScenarios() {
  const list = await (await fetch("/api/scenarios")).json();
  $("sim-select").replaceChildren(...list.map((s) => new Option(s.title, s.id)));
}
$("sim-run").onclick = async () => {
  state.follow = true;
  await fetch(`/api/simulate/${$("sim-select").value}`, { method: "POST" });
};

// ---------- browser calling (Guava WebRTC) ----------
$("browser-call").onclick = async () => {
  const btn = $("browser-call");
  if (state.browserCall) { await state.browserCall.hangup(); return; }
  if (!window.Guava) { btn.textContent = "Calling library didn't load"; return; }
  state.follow = true;
  const call = new Guava.Call({ webrtcCode: state.config.webrtc_code, customPayload: { source: "sievec-dashboard" } });
  state.browserCall = call;
  call.onStateChange((s) => {
    const words = { starting: "Starting", "waiting-for-permission": "Allow the microphone", connecting: "Connecting", connected: "Hang up", ended: "Call from browser" };
    btn.textContent = words[s] || btn.textContent;
    btn.classList.toggle("live", s === "connected");
    if (s === "ended") state.browserCall = null;
  });
  await call.start();
};

loadScenarios();
connect();
