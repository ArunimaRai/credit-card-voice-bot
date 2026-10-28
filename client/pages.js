// Guidelines, Personas and Analytics pages. Each one fetches
// /api/insights/<page> (bot/insights.py), which recomputes from the files on
// disk, when it opens and every REFRESH_MS while it stays open, so the
// numbers are the system's current ones. The call console keeps running
// underneath: switching pages never touches the call.

const REFRESH_MS = 15000;
const VIEWS = ["console", "guidelines", "personas", "analytics"];
const RENDER = { guidelines: renderGuidelines, personas: renderPersonas, analytics: renderAnalytics };

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
// voice-guide.md inline markdown: **bold** and `code` only.
const md = (s) => esc(s).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/`([^`]+)`/g, "<code>$1</code>");
const pct = (r, digits = 0) => (r == null ? null : `${(r * 100).toFixed(digits)}%`);
const title = (s) => String(s || "").replace(/[-_+]/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
const timeOf = (iso) => (iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "—");
const dateOf = (iso) => (iso ? new Date(iso).toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "never");

const OUTCOMES = {
  promise_to_pay: "Promise to pay",
  already_paid: "Already paid",
  callback_request: "Callback",
  dispute_transfer: "Transfer to human",
  no_outcome: "No outcome",
};
const SEVERITY_CHIP = { critical: "bad", major: "warn", minor: "" };

let current = null;
let timer = null;
const cache = {};

function head(kicker, h1, lead, view) {
  return `<header class="doc-head">
    <div><div class="an-k">${kicker}</div><h1>${h1}</h1><p class="doc-lead">${lead}</p></div>
    <div class="doc-sync" id="sync-${view}"></div>
  </header>`;
}
function section(h, sub, body) {
  return `<section class="doc-section"><h2>${h}</h2>${sub ? `<p class="doc-sub">${sub}</p>` : ""}${body}</section>`;
}
function stat(k, v, d) {
  return `<div class="stat"><div class="an-k">${k}</div><div class="stat-v">${v ?? "—"}</div>${d ? `<div class="stat-d">${d}</div>` : ""}</div>`;
}
function bars(rows, { fmt = (v) => v, max, hl } = {}) {
  const top = max ?? Math.max(1, ...rows.map((r) => r.value));
  return `<div class="bars-list">${rows.map((r) => `
    <div class="bar-row${hl && hl(r) ? " hl" : ""}" title="${esc(r.label)}: ${esc(fmt(r.value))}">
      <span class="lbl">${esc(r.label)}</span>
      <span class="bar-track"><span class="bar-fill" style="width:${Math.max(0, (r.value / top) * 100)}%"></span></span>
      <span class="val">${esc(fmt(r.value))}</span>
    </div>`).join("")}</div>`;
}
function metric(name, value, desc) {
  return `<div class="metric"><div class="metric-name">${name}</div>
    <div class="metric-v${value == null ? " na" : ""}">${value ?? "Not measured yet"}</div>
    <div class="metric-d">${desc}</div></div>`;
}

// --- router -------------------------------------------------------------

function show(view) {
  if (!VIEWS.includes(view)) view = "console";
  document.body.dataset.view = view;
  document.querySelectorAll(".page[data-view]").forEach((p) => { p.hidden = p.dataset.view !== view; });
  document.querySelectorAll(".site-nav a").forEach((a) => {
    if (a.dataset.view === view) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  document.title = view === "console" ? "Mumbai Bank · Shreya" : `${title(view)} · Mumbai Bank`;
  clearInterval(timer);
  current = view;
  if (RENDER[view]) {
    window.scrollTo(0, 0);
    load(view);
    timer = setInterval(() => document.visibilityState === "visible" && load(view), REFRESH_MS);
  }
}

async function load(view) {
  const host = document.getElementById(`view-${view}`);
  if (!cache[view]) host.innerHTML = `<div class="doc-empty">Loading…</div>`;
  try {
    const r = await fetch(`/api/insights/${view}`, { cache: "no-store" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();
    if (current !== view) return;
    const fresh = JSON.stringify(data, (k, v) => (k === "generated_at" ? undefined : v));
    if (fresh !== cache[view]?.key) {
      cache[view] = { key: fresh };
      host.innerHTML = RENDER[view](data);
      wire(view, host);
    }
    sync(view, data.generated_at, false);
  } catch (e) {
    if (!cache[view]) host.innerHTML = `<div class="doc-empty">Couldn't load this page (${esc(e.message)}). Is <code>bot.main</code> running?</div>`;
    else sync(view, null, true);
  }
}

function sync(view, at, stale) {
  const box = document.getElementById(`sync-${view}`);
  if (!box) return;
  box.classList.toggle("stale", stale);
  box.innerHTML = `<span class="sdot"></span>${stale ? "Couldn't refresh · showing last data" : `Live · updated ${timeOf(at)}`}<button type="button" data-refresh>Refresh</button>`;
  box.querySelector("[data-refresh]").onclick = () => load(view);
}

function wire(view, host) {
  if (view !== "personas") return;
  host.querySelectorAll("[data-filter]").forEach((b) => {
    b.onclick = () => {
      host.querySelectorAll("[data-filter]").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
      host.querySelectorAll("tr[data-source]").forEach((tr) => {
        const f = b.dataset.filter;
        tr.hidden = f !== "all" && (f === "live" ? tr.dataset.source !== "live" : tr.dataset.source === "live");
      });
    };
  });
}

// --- Guidelines ---------------------------------------------------------

function renderGuidelines(g) {
  if (!g.source) return `<div class="doc-empty">voice-guide.md wasn't found on the server.</div>`;
  const rules = g.non_negotiables.flatMap((x) => x.rules);
  const fired = g.enforcement.reduce((n, e) => n + e.fired_live, 0);
  const checks = new Set(g.enforcement.flatMap((e) => e.checks));

  const groups = g.non_negotiables.map((grp) => `
    <section class="an-card">
      <div class="an-k">${esc(grp.title)}</div>
      <ol class="rules">${grp.rules.map((r) => `<li><span class="rule-n">${r.n}</span><span><b>${esc(r.title)}</b>${/^[,(]/.test(r.text) ? "" : "."} ${md(r.text)}</span></li>`).join("")}</ol>
    </section>`).join("");

  const enforcement = `<section class="an-card"><div class="tbl-wrap"><table class="tbl">
    <thead><tr><th>Rule</th><th>Enforced in code by</th><th class="num">Fired on live calls</th></tr></thead>
    <tbody>${g.enforcement.map((e) => `<tr>
      <td class="strong">${md(e.rule)}</td><td>${md(e.enforced_by)}</td>
      <td class="num">${e.checks.length ? (e.fired_live ? `<span class="chip warn">${e.fired_live}×</span>` : `<span class="chip ok">0</span>`) : `<span class="an-none">n/a</span>`}</td>
    </tr>`).join("")}</tbody></table></div></section>`;

  const flow = `<ol class="flow">${g.stages.map((s, i) => {
    const [k, ...rest] = s.title.split(":");
    return `<li><div class="an-k">${esc(rest.length ? k : `Step ${i + 1}`)}</div><h3>${esc(rest.length ? rest.join(":").trim() : s.title)}</h3>${s.goal ? `<p>${md(s.goal)}</p>` : ""}${s.sample ? `<p class="flow-say">“${esc(s.sample)}”</p>` : ""}</li>`;
  }).join("")}</ol>`;

  const words = `<div class="doc-grid">
    <section class="an-card"><div class="an-k">Say this, not that</div><div class="tbl-wrap"><table class="tbl">
      <thead><tr><th>Say this</th><th>Not this</th></tr></thead>
      <tbody>${g.prefer.map((p) => `<tr><td class="strong">${md(p.say)}</td><td>${md(p.avoid)}</td></tr>`).join("")}</tbody></table></div></section>
    <section class="an-card"><div class="an-k">Banned outright</div>
      <p class="an-rationale">Shreya never says these. The <code>no_banned_phrase</code> guardrail replaces any draft reply that contains one before it reaches text-to-speech.</p>
      <div class="chip-row">${g.banned.map((b) => `<span class="chip bad">${esc(b)}</span>`).join("")}</div>
      <div class="an-k" style="margin-top:22px">Compliance defaults</div>
      <ol class="rules">${g.compliance_defaults.map((d, i) => `<li><span class="rule-n">${i + 1}</span><span>${md(d)}</span></li>`).join("")}</ol>
    </section></div>`;

  return head("Guidelines", "The rules Shreya works within",
    `Read live from <code>${esc(g.source.path)}</code>, last edited ${dateOf(g.source.modified_at)}. The bank's brief and the project's decisions come first; every rule is also enforced in code, so a prompt slip can't reach the customer.`, "guidelines") +
    `<div class="stats">
      ${stat("Non-negotiables", rules.length, "Override everything else in the guide")}
      ${stat("Code checks", checks.size, "Guardrails that run on every reply")}
      ${stat("Banned phrases", g.banned.length, "Blocked before text-to-speech")}
      ${stat("Guardrail fires", fired, `Across ${g.live_calls} analysed live call${g.live_calls === 1 ? "" : "s"}`)}
    </div>` +
    section("Non-negotiables", "From Mumbai Bank's brief and the project's settled decisions. Where a sample line and a rule disagree, the rule wins.", `<div class="doc-grid">${groups}</div>`) +
    section("Compliance and how it's enforced", "A rule enforced only by the prompt is a suggestion, so each one has a code check. The last column counts how often that check had to step in on real calls.", enforcement) +
    section("Call flow", "The stages every call moves through, from the right-party check to a warm close.", flow) +
    section("Words and phrases", "Write for the ear: plain, calm and never threatening.", words);
}

// --- Personas -----------------------------------------------------------

const SOURCE = {
  brief: ["accent", "Bank's brief"],
  inferred: ["", "Inferred"],
  live: ["ok", "Learned live"],
};
const DIFFICULTY = { easy: "ok", medium: "", hard: "warn", trap: "bad" };

function resultCell(p) {
  const r = p.result;
  if (!r) return `<span class="an-none">Not run yet</span>`;
  const issues = r.critical + r.major + r.minor;
  const verdict = r.critical ? `<span class="chip bad">${r.critical} critical</span>`
    : r.major ? `<span class="chip warn">${r.major} major</span>`
      : issues ? `<span class="chip">${issues} minor</span>` : `<span class="chip ok">Passed</span>`;
  return `<div class="chips">${verdict}<span class="chip">${esc(OUTCOMES[r.outcome] ?? r.outcome)}</span></div>
    <span class="id">${r.checks - r.failed}/${r.checks} checks · ${r.version} · ${r.duration_sec}s</span>`;
}

function renderPersonas(d) {
  const L = d.learning;
  const rows = d.personas.map((p) => {
    const [tone, label] = SOURCE[p.source] ?? ["", p.source];
    const expected = p.expected.length ? p.expected.map((o) => OUTCOMES[o] ?? o).join(" or ") : "Any, if handled safely";
    return `<tr data-source="${esc(p.source)}">
      <td class="strong">${esc(title(p.archetype.replace(/^live-/, "")))}<span class="id">${esc(p.id)}${p.from_call ? ` · from call ${esc(p.from_call.slice(0, 8))}` : ""}</span></td>
      <td><div class="chips"><span class="chip ${DIFFICULTY[p.difficulty] ?? ""}">${esc(title(p.difficulty))}</span><span class="chip ${tone}">${label}</span></div></td>
      <td><span class="clamp" title="${esc(p.brief)}">${esc(p.brief)}</span></td>
      <td>${esc(expected)}${p.must_not_say.length ? `<span class="id">Never says: ${esc(p.must_not_say.slice(0, 4).join(", "))}${p.must_not_say.length > 4 ? "…" : ""}</span>` : ""}</td>
      <td>${resultCell(p)}</td>
    </tr>`;
  }).join("");
  const diff = d.counts.by_difficulty;
  const passed = d.personas.filter((p) => p.result && !(p.result.critical + p.result.major + p.result.minor)).length;
  const run = d.personas.filter((p) => p.result).length;

  return head("Personas", "Every kind of customer Shreya has handled",
    "Each persona is a simulated customer with a goal and a temperament, played freely by a model rather than read from a script. The suite runs Shreya against all of them before a prompt version ships.", "personas") +
    `<section class="evolve">
      <div class="evolve-icon"><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M16 10a6 6 0 1 1-1.8-4.3"/><path d="M16.5 3v3.5H13"/></svg></div>
      <div>
        <div class="an-k">Self-evolving</div>
        <h2>This list learns from Shreya's real conversations</h2>
        <p>After every real call, Shreya's side is checked in code. When she makes a critical or major mistake, the call is turned into a new persona that re-creates that customer, and it joins the suite automatically. The next prompt version has to pass it before it ships, so the same mistake can't come back unnoticed.</p>
        <ul class="loop">
          <li><b>${L.live_calls}</b><span>real calls analysed</span></li>
          <li><b>${L.calls_turned_into_cases}</b><span>turned into eval cases</span></li>
          <li><b>${d.counts.learned}</b><span>persona${d.counts.learned === 1 ? "" : "s"} learned from live calls</span></li>
          <li><b>${L.eval_candidates}</b><span>issues queued as eval checks</span></li>
        </ul>
        <span class="src">Last persona learned ${dateOf(L.last_promoted_at)} · last suite run ${dateOf(L.last_suite_run_at)}</span>
      </div>
    </section>` +
    `<div class="stats" style="margin-top:14px">
      ${stat("Personas", d.counts.total, `${d.counts.core} written up front · ${d.counts.learned} learned`)}
      ${stat("Passed cleanly", `${passed}<small>/ ${run}</small>`, "No issues in the latest suite run")}
      ${stat("Easy · medium", `${diff.easy ?? 0} · ${diff.medium ?? 0}`, "Cooperative to mildly evasive")}
      ${stat("Hard · trap", `${diff.hard ?? 0} · ${diff.trap ?? 0}`, "Hardship, abuse, jailbreaks, third parties")}
    </div>` +
    section("All personas", "Source says where a persona came from: the bank's brief, inferred from Indian collections practice, or learned from a live call.",
      `<div class="seg" role="group" aria-label="Filter personas">
        <button type="button" data-filter="all" aria-pressed="true">All ${d.counts.total}</button>
        <button type="button" data-filter="core" aria-pressed="false">Written up front ${d.counts.core}</button>
        <button type="button" data-filter="live" aria-pressed="false">Learned live ${d.counts.learned}</button>
      </div>
      <section class="an-card"><div class="tbl-wrap"><table class="tbl">
        <thead><tr><th>Persona</th><th>Difficulty · source</th><th>What the customer does</th><th>Expected outcome</th><th>Latest result</th></tr></thead>
        <tbody>${rows}</tbody></table></div></section>`);
}

// --- Analytics ----------------------------------------------------------

function renderAnalytics(a) {
  const L = a.live, S = a.suite, P = a.speech;
  const findingsPerCall = L.engaged ? (L.findings.total / L.engaged).toFixed(1) : null;
  const outcomeRows = Object.entries(OUTCOMES).map(([k, label]) => ({ label, value: L.outcomes[k] ?? 0 }));
  const triggerTotal = Object.values(L.guardrail_triggers).reduce((n, v) => n + v, 0);
  const compliance = S.buckets.find((b) => b.bucket === "compliance");
  const passRate = (b) => (b ? `${b.passed}/${b.checks}` : null);
  const sentimentRows = ["positive", "neutral", "frustrated", "unclear"].map((k) => ({ label: title(k), value: L.sentiment_end[k] ?? 0 }));
  const sttRows = [...P.configs].sort((x, y) => x.wer - y.wer).map((c) => ({ label: c.config.replace(/^rt:|^old:/, ""), value: c.wer, live: c.config === P.live_config }));
  const ofEngaged = `of ${L.engaged} call${L.engaged === 1 ? "" : "s"} where the customer spoke`;

  const card = (k, h, body) => `<section class="an-card"><div class="an-k">${k}</div><h3 class="doc-card-title">${h}</h3>${body}</section>`;

  return head("Analytics", "How Shreya is performing",
    `Real calls come from <code>data/calls/</code>; the simulated suite from <code>evals/results/</code>; speech-to-text from <code>evals/live/</code>. ${L.calls} live call${L.calls === 1 ? "" : "s"} so far, the latest ${dateOf(L.last_call_at)}.`, "analytics") +
    `<div class="stats">
      ${stat("Live calls", L.calls, `${L.engaged} where the customer spoke`)}
      ${stat("Resolution rate", pct(L.resolution_rate), "Ended with a clear outcome")}
      ${stat("Right-party contact", pct(L.right_party_rate), "Identity confirmed")}
      ${stat("Avg handle time", L.avg_duration_sec != null ? `${L.avg_duration_sec}<small>s</small>` : null, "Target: 2–3 minutes or less")}
      ${stat("Agent issues / call", findingsPerCall, `${L.findings.critical} critical · ${L.findings.major} major · ${L.findings.minor} minor`)}
    </div>` +
    section("The criteria", "The standard dimensions a voice agent is judged on. Each shows its current value and where it comes from; anything not yet instrumented says so rather than showing a made-up number.",
      `<div class="doc-grid">
        ${card("1 · Task success", "Does the call end with a usable outcome?", `<div class="metrics">
          ${metric("Resolution rate", pct(L.resolution_rate), `Calls that ended with a promise, payment, callback or transfer, ${ofEngaged}.`)}
          ${metric("Promise-to-pay rate", pct(L.promise_to_pay_rate), `Calls that ended with a dated promise of at least the minimum due, ${ofEngaged}.`)}
          ${metric("Outcome validity", pct(S.outcome_valid_rate), `Simulated calls whose filed outcome passed schema validation (<b>${S.runs} runs</b>). An invented commitment fails here.`)}
          </div>${bars(outcomeRows)}<span class="src">Outcome mix across all ${L.calls} live calls</span>`)}
        ${card("2 · Compliance and safety", "Does Shreya stay inside the rules?", `<div class="metrics">
          ${metric("Compliance checks passed", passRate(compliance), "Must-not-say and must-say assertions across the simulated suite (CIBIL, legal threats, waivers, wrong figures).")}
          ${metric("Critical violations (suite)", String(S.critical), `Critical failures in the latest ${S.version ?? ""} suite run.`)}
          ${metric("Guardrail interventions", String(triggerTotal), `Draft replies blocked on live calls${triggerTotal ? `: ${Object.entries(L.guardrail_triggers).map(([k, v]) => `<code>${esc(k)}</code> ×${v}`).join(", ")}` : ""}.`)}
          ${metric("AI disclosure", pct(L.disclosure_rate), "Verified calls where Shreya said she's automated and the call is recorded.")}
          </div>`)}
        ${card("3 · Speech recognition", "Does Shreya hear the customer correctly?", `<div class="metrics">
          ${metric("Word error rate", pct(P.wer, 1), `Live config <code>${esc(P.live_config)}</code> on <b>${P.speech ?? 0} speech clips</b> from real calls, against a three-model vote reference.`)}
          ${metric("Noise let through", P.noise != null ? `${P.junk_passed}/${P.noise}` : null, "Noise or silence clips that still produced a transcript after the junk filter.")}
          ${metric("Speech wrongly dropped", P.speech != null ? `${P.speech_dropped}/${P.speech}` : null, "Real speech the filter held back.")}
          ${metric("Unclear turns", pct(L.unclear_rate), `Customer turns held back as unclear on live calls (${L.customer_turns} turns).`)}
          </div>${bars(sttRows, { fmt: (v) => pct(v, 1), max: 1, hl: (r) => r.live })}<span class="src">Word error rate by STT config, lower is better; teal is the one live</span>`)}
        ${card("4 · Latency and turn-taking", "Does the conversation flow like a phone call?", `<div class="metrics">
          ${metric("Response latency", null, "Time from the customer finishing to Shreya's first audio. Turn timestamps aren't stored with the call record yet.")}
          ${metric("Interruptions handled", null, "Barge-ins where the customer spoke over Shreya and she stopped. Not logged yet.")}
          ${metric("Avg reply length", L.avg_bot_words != null ? `${L.avg_bot_words} words` : null, "Words per Shreya turn. Past about 50 words (20 seconds) callers start to interrupt.")}
          ${metric("Avg turns per call", L.avg_turns != null ? String(L.avg_turns) : null, "Shreya and customer turns combined.")}
          </div>`)}
        ${card("5 · Conversation quality", "How well does Shreya handle the call itself?", `<div class="metrics">
          ${metric("Clean calls", L.engaged ? `${L.findings.clean_calls}/${L.engaged}` : null, "Live calls where the code review of Shreya's side found nothing.")}
          </div>${L.top_findings.length ? bars(L.top_findings.map((f) => ({ label: f.title, value: f.count }))) : `<div class="an-pass">No issues found on live calls</div>`}<span class="src">Issues found on live calls, by type; each one is queued as an eval case</span>`)}
        ${card("6 · Customer experience", "How does the customer feel by the end?", `<div class="metrics">
          ${metric("Ended positive or neutral", L.engaged ? pct(((L.sentiment_end.positive ?? 0) + (L.sentiment_end.neutral ?? 0)) / L.engaged) : null, `Customer sentiment at the end of the call, ${ofEngaged}.`)}
          ${metric("Got worse during the call", L.engaged ? String(Object.entries(L.sentiment_moves).filter(([k]) => /→frustrated$/.test(k) && !/^frustrated/.test(k)).reduce((n, [, v]) => n + v, 0)) : null, "Calls where the customer ended frustrated but didn't start that way.")}
          </div>${bars(sentimentRows)}<span class="src">End-of-call sentiment on live calls</span>`)}
        ${card("7 · Escalation", "Does Shreya hand off when she should, and only then?", `<div class="metrics">
          ${metric("Transfer accuracy", pct(S.transfer_accuracy), `Simulated calls where Shreya transferred exactly when the persona required it (${S.transfers_required} required a human).`)}
          ${metric("Live transfer rate", pct(L.transfer_rate), `Calls handed to a human for dispute, hardship or abuse, ${ofEngaged}.`)}
          </div>`)}
        ${card("8 · Robustness and regression", "Does a new version stay safe on known hard cases?", `<div class="metrics">
          ${metric("Suite runs clean", `${S.clean_runs}/${S.runs}`, `Personas with no issue at all in the latest ${S.version ?? ""} run, last run ${dateOf(S.last_run_at)}.`)}
          ${metric("Avg weighted issue score", S.avg_score != null ? S.avg_score.toFixed(3) : null, "Severity-weighted issues per call; 0 is perfect.")}
          ${metric("Live-call replays passed", P.replay_cases ? `${P.replay_passed}/${P.replay_cases}` : null, "Real customer turns, as STT heard them, replayed into the shipped bot.")}
          </div>${bars(S.buckets.map((b) => ({ label: title(b.bucket), value: b.checks ? b.passed / b.checks : 0 })), { fmt: (v) => pct(v), max: 1 })}<span class="src">Suite pass rate by check bucket</span>`)}
      </div>`);
}

window.addEventListener("hashchange", () => show(location.hash.replace(/^#\/?/, "") || "console"));
show(location.hash.replace(/^#\/?/, "") || "console");
