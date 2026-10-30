const $ = (id) => document.getElementById(id);
const state = { runs: [], selected: null, manuallySelected: false, paused: false, healthy: false, taskOpen: new Map(), agent: "lead", view: "garden" };
const number = (value) => typeof value === "number" && Number.isFinite(value) ? new Intl.NumberFormat().format(value) : "—";
const short = (value) => typeof value === "number" ? new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 }).format(value) : "—";
const age = (ms) => { const s = Math.max(0, Math.floor(ms / 1000)); return s >= 3600 ? `${Math.floor(s / 3600)}h ${String(Math.floor(s % 3600 / 60)).padStart(2, "0")}m` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`; };
const cap = (value) => String(value || "waiting").replaceAll("_", " ");
const el = (tag, className, value) => { const node = document.createElement(tag); if (className) node.className = className; if (value != null) node.textContent = value; return node; };

function selectedRun() { return state.runs.find((run) => run.id === state.selected) || null; }
function setStatus() {
  const node = $("connection");
  node.classList.toggle("offline", !state.healthy);
  node.lastChild.textContent = state.paused ? " Display updates paused" : state.healthy ? " Live updates" : state.tokenRequired ? " Open the URL printed at launch" : " Connection lost";
}
function renderPicker(repository) {
  const picker = $("run-select");
  const old = [...picker.options].map((option) => option.value).join("|");
  const next = state.runs.map((run) => run.id).join("|");
  if (old !== next) {
    picker.replaceChildren();
    if (!state.runs.length) picker.add(new Option("No runs yet", ""));
    for (const run of state.runs) {
      const stamp = run.started_at ? new Date(run.started_at).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "Unknown time";
      picker.add(new Option(`${stamp} · ${run.status === "IN_PROGRESS" ? "Live" : cap(run.status)}`, run.id));
    }
  }
  if (!state.manuallySelected || !state.runs.some((run) => run.id === state.selected)) state.selected = state.runs[0]?.id || null;
  picker.value = state.selected || "";
  $("repo-name").textContent = repository || "All repositories";
}
function renderOverview(run) {
  $("run-title").textContent = run ? run.id : "Waiting for a run";
  const badge = $("run-state"); badge.textContent = cap(run?.status); badge.className = `state-pill ${run?.status?.toLowerCase() || ""}`;
  const elapsed = run?.started_at ? new Date(run.ended_at || Date.now()) - new Date(run.started_at) : null;
  $("elapsed").textContent = elapsed == null ? "—" : age(elapsed);
  $("spawns").textContent = number(run?.context?.spawned_agents);
  $("concurrency").textContent = number(run?.context?.peak_concurrent_workers);
  $("tokens").textContent = short(run?.metrics?.total_tokens);
  $("tokens-caption").textContent = run?.metrics?.total_tokens == null ? "total unavailable" : "observed total";
  const metrics = run?.metrics || {};
  const observed = (a, b) => a == null && b == null ? null : (a || 0) + (b || 0);
  $("input-tokens").textContent = short(observed(metrics.lead_input_tokens, metrics.worker_input_tokens));
  $("output-tokens").textContent = short(observed(metrics.lead_output_tokens, metrics.worker_output_tokens));
  $("speedup").textContent = typeof metrics.parallel_speedup === "number" ? `${metrics.parallel_speedup.toFixed(2)}×` : "—";
  $("cost").textContent = costText(metrics.estimated_cost_usd, metrics.cost_complete, metrics.unpriced);
  $("lead-model").textContent = run?.metrics?.lead_model || run?.context?.lead_usage?.model || "Waiting for model";
  const workerCalls = Object.values(run?.context?.worker_usage || {}).reduce((total, entry) => total + (entry?.invocations || 0), 0);
  $("lead-detail").textContent = run ? `${run.phases.length} tasks · ${workerCalls} worker calls observed` : "Ready to coordinate the next run.";
  $("context").textContent = run?.context?.peak_context_tokens == null ? "—" : `${short(run.context.peak_context_tokens)} tokens`;
  $("compactions").textContent = number(run?.context?.compactions);
}
function renderCompletion(run) {
  const status = run?.status || "WAITING";
  const message = $("run-message");
  message.className = `run-message ${status.toLowerCase()}`;
  const completed = (run?.phases || []).filter((phase) => phase.status === "completed").length;
  const total = run?.phases?.length || 0;
  const ended = run?.ended_at ? new Date(run.ended_at).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : null;
  const copy = {
    WAITING: ["✦", "Waiting for a run", "Start an orchestration run to see its progress here."],
    IN_PROGRESS: ["✦", "Work in motion", `${completed} of ${total} tasks completed so far.`],
    COMPLETED: ["✿", "This run is complete", `${completed} of ${total} tasks completed${ended ? ` · Finished ${ended}` : ""}.`],
    PARTIAL: ["◒", "This run has ended with follow-ups", `${completed} of ${total} tasks completed${ended ? ` · Finished ${ended}` : ""}.`],
    BLOCKED: ["!", "This run stopped at a blocker", `${run?.blocker_code || "A blocker was recorded"}${ended ? ` · Finished ${ended}` : ""}`],
    FAILED: ["!", "This run failed", `${run?.blocker_code || "A failure was recorded"}${ended ? ` · Finished ${ended}` : ""}`],
  }[status] || ["◒", "Run ended", ended ? `Finished ${ended}` : cap(status)];
  $("run-message-icon").textContent = copy[0];
  $("run-message-title").textContent = copy[1];
  // Single-owner run lease: who may continue this run, and whether that owner still answers.
  const lease = run?.lease;
  const holder = lease ? ` · ${lease.state === "stale" ? "Stale lease" : "Held"} by ${lease.owner_id} (pid ${lease.pid} on ${lease.host})${lease.state === "stale" ? `: ${lease.stale_reason}` : ""}` : "";
  $("run-message-detail").textContent = copy[2] + holder;
  const links = $("run-links"); links.replaceChildren();
  for (const link of run?.links || []) {
    if (link.kind !== "pr") continue;
    const anchor = el("a", "pr-link", `View PR #${link.number} ↗`);
    anchor.href = link.url; anchor.target = "_blank"; anchor.rel = "noopener noreferrer";
    links.append(anchor);
  }
}
function renderReport(run) {
  const phases = run?.phases || [];
  const completed = phases.filter((phase) => phase.status === "completed").length;
  const percent = phases.length ? Math.round(100 * completed / phases.length) : 0;
  $("task-ring").style.setProperty("--progress", `${percent}%`);
  $("progress-percent").textContent = `${percent}%`;
  $("progress-copy").textContent = phases.length ? `${completed} of ${phases.length} tasks completed` : "No tasks recorded yet.";
  $("report-status").textContent = phases.length ? `${completed} / ${phases.length} complete` : "Awaiting tasks";
  const metrics = run?.metrics || {};
  const observed = (input, output) => input == null && output == null ? null : (input || 0) + (output || 0);
  const lead = observed(metrics.lead_input_tokens, metrics.lead_output_tokens);
  const worker = observed(metrics.worker_input_tokens, metrics.worker_output_tokens);
  const maximum = Math.max(lead || 0, worker || 0, 1);
  $("lead-token-count").textContent = short(lead);
  $("worker-token-count").textContent = short(worker);
  $("lead-token-bar").style.width = `${100 * (lead || 0) / maximum}%`;
  $("worker-token-bar").style.width = `${100 * (worker || 0) / maximum}%`;
}
function renderTasks(run) {
  const list = $("task-list");
  list.replaceChildren();
  const phases = run?.phases || [];
  $("task-count").textContent = `${phases.length} ${phases.length === 1 ? "task" : "tasks"}`;
  if (!phases.length) { list.append(el("p", "empty", "Tasks will bloom here once routing begins.")); return; }
  for (const phase of phases) {
    const card = el("details", "task-card"); card.dataset.id = phase.id;
    const key = `${run.id}/${phase.id}`;
    card.open = state.taskOpen.has(key) ? state.taskOpen.get(key) : phase.status === "in_progress";
    card.addEventListener("toggle", () => state.taskOpen.set(key, card.open));
    const head = el("summary", "task-head");
    head.append(el("span", `task-glyph ${phase.status}`, phase.status === "completed" ? "✿" : "✦"));
    const title = el("span", "task-name"); title.append(el("strong", "", phase.id), el("small", "", `Route ${phase.route || "pending"}${phase.wave_id ? ` · ${phase.wave_id}` : ""}`)); head.append(title);
    head.append(el("span", `task-status ${phase.status}`, statusLabel(run, phase)), el("span", "chevron", "›")); card.append(head);
    const details = el("div", "task-detail");
    if (phase.wave_id) details.append(el("p", "wave", `◇ In ${phase.wave_id}`));
    if (!phase.workers?.length) details.append(el("p", "no-workers", "No helpers have joined this task yet."));
    for (const worker of phase.workers || []) {
      const row = el("div", "worker-row"); row.append(el("span", `worker-dot ${worker.status}`));
      const copy = el("div", "worker-copy"); copy.append(el("strong", "", worker.step_id), el("small", "", [worker.backend, worker.model].filter(Boolean).join(" · ") || "Model pending"));
      row.append(copy, el("span", "worker-state", statusLabel(run, phase, worker))); details.append(row);
    }
    card.append(details); list.append(card);
  }
}
function renderActivity(run) {
  const list = $("activity-list"); list.replaceChildren();
  const items = (run?.activity || []).slice(-14).reverse();
  if (!items.length) { list.append(el("p", "empty", "A quiet moment. Worker activity will appear here.")); return; }
  for (const item of items) {
    const row = el("div", `activity-item ${item.status}`), icon = el("span", "activity-icon", item.status === "spawned" ? "✿" : item.status === "failed" ? "!" : item.status === "started" ? "↗" : "✓");
    const copy = el("div", "");
    const who = item.tool === "spawn_subagent" ? "Subagent" : item.tool === "message_subagent" ? "Subagent follow-up" : item.tool === "ClaudeWorker" ? "Claude worker" : "Claude request";
    const action = item.status === "spawned" ? "joined" : item.status === "started" ? "started" : item.status === "failed" ? "ran into trouble" : "finished";
    copy.append(el("p", "", `${who} ${action}${item.task_id ? ` · ${item.task_id}` : ""}${item.step_id ? ` / ${item.step_id}` : ""}`));
    const at = item.at ? new Date(item.at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "";
    copy.append(el("small", "", `${at}${item.subagent_id ? ` · ${item.subagent_id}` : ""}`)); row.append(icon, copy); list.append(row);
  }
}
// "Abandoned (run ended PARTIAL)": reconciled work the run ended without finishing, never shown as running.
const abandonedLabel = (run, phaseId, stepId, epoch) => {
  const entry = (run?.reconciled || []).find((item) => item.phase_id === phaseId && (stepId ? item.kind === "worker" && item.step_id === stepId && (item.route_epoch ?? 0) === (epoch ?? 0) : item.kind === "phase"));
  const ended = String(entry?.reason || "").replace(/^run finished /, "");
  return ended ? `Abandoned (run ended ${ended})` : "Abandoned";
};
const statusLabel = (run, phase, worker) => (worker || phase).status === "abandoned" ? abandonedLabel(run, phase.id, worker?.step_id, worker?.route_epoch) : cap((worker || phase).status);
const normalizedStatus = (value) => ({ in_progress: "active", completed: "completed", partial: "blocked", abandoned: "abandoned", blocked: "blocked", failed: "failed", not_started: "waiting" })[String(value || "").toLowerCase()] || "waiting";
const timeLabel = (value) => value ? new Date(value).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "Time unknown";

function agentNodes(run) {
  if (!run) return [];
  const phases = run.phases || [];
  const activities = run.activity || [];
  const nodes = [];
  const seen = new Set();
  for (const event of activities) {
    if (event.tool !== "spawn_subagent" || !["started", "spawned"].includes(event.status)) continue;
    const task = event.task_id || "unassigned";
    const step = event.step_id || "subagent";
    const match = `${task}/${step}`;
    if (event.status === "started" && activities.some((item) => item.tool === "spawn_subagent" && item.status === "spawned" && `${item.task_id || "unassigned"}/${item.step_id || "subagent"}` === match)) continue;
    const key = `subagent/${event.subagent_id || match}`;
    if (seen.has(key)) continue;
    seen.add(key);
    const phase = phases.find((item) => item.id === task);
    const worker = phase?.workers?.find((item) => item.step_id === step);
    nodes.push({ key, kind: "subagent", label: step.replaceAll("_", " "), task, stepId: step, status: normalizedStatus(worker?.status || phase?.status || "in_progress"), model: worker?.model || "Model not recorded", backend: worker?.backend || "pi-subagent", identity: event.subagent_id || "Spawn starting", provisional: event.status === "started" });
  }
  for (const phase of phases) {
    for (const worker of phase.workers || []) {
      if (nodes.some((node) => node.kind === "subagent" && node.task === phase.id && node.label === worker.step_id.replaceAll("_", " "))) continue;
      nodes.push({ key: `worker/${phase.id}/${worker.step_id}`, kind: "worker", label: worker.step_id.replaceAll("_", " "), task: phase.id, stepId: worker.step_id, status: normalizedStatus(worker.status), label_text: worker.status === "abandoned" ? abandonedLabel(run, phase.id, worker.step_id, worker.route_epoch) : undefined, model: worker.model || "Model not recorded", backend: worker.backend || "pi-subagent", identity: worker.step_id });
    }
  }
  return nodes;
}

function nodeButton(node, x, y) {
  const button = el("button", `agent-node ${node.kind} ${node.status}${state.agent === node.key ? " selected" : ""}`);
  button.type = "button";
  button.style.left = `${x / 7}%`;
  button.style.top = `${y / 4.7}%`;
  button.dataset.agent = node.key;
  button.title = `${node.label} · ${node.task || "run"}`;
  button.setAttribute("aria-label", `${node.kind === "lead" ? "Lead agent" : node.kind}, ${node.label}, ${node.status}, task ${node.task || "run"}`);
  button.append(el("span", "agent-face", node.kind === "lead" ? "✿" : node.kind === "subagent" ? "✦" : "●"), el("strong", "", node.label), el("small", "", node.kind === "lead" ? "lead" : `${node.kind} · ${node.task}`), el("span", "node-state", node.status === "completed" ? "✓ Completed" : node.status === "active" ? "● Working" : node.status === "blocked" ? "! Blocked" : node.status === "failed" ? "× Failed" : node.status === "abandoned" ? "– Abandoned" : "○ Waiting"));
  button.addEventListener("click", () => { state.agent = node.key; document.querySelectorAll(".agent-node").forEach((item) => item.classList.toggle("selected", item.dataset.agent === node.key)); renderAgentDetail(selectedRun(), node); });
  return button;
}

function gardenLine(svg, from, to, dotted = false) {
  const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
  for (const [key, value] of Object.entries({ x1: from.x, y1: from.y, x2: to.x, y2: to.y })) line.setAttribute(key, value);
  line.setAttribute("class", dotted ? "garden-line related" : "garden-line");
  svg.append(line);
}

function renderAgentDetail(run, selected) {
  const box = $("agent-detail"); box.replaceChildren();
  if (!run) { box.append(el("p", "empty", "Agents will appear when a run starts.")); return; }
  const node = selected || { key: "lead", kind: "lead", label: "Lead agent", status: normalizedStatus(run.status), model: run.metrics?.lead_model || run.context?.lead_usage?.model || "Model not recorded", task: "The whole run" };
  box.append(el("span", `detail-symbol ${node.status}`, node.kind === "lead" ? "✿" : node.kind === "subagent" ? "✦" : "●"));
  box.append(el("p", "eyebrow", node.kind === "lead" ? "AT THE CENTER" : node.kind === "subagent" ? "SUBAGENT" : "WORKER"));
  box.append(el("h3", "", node.label));
  box.append(el("p", `detail-status ${node.status}`, node.status === "active" ? "Working" : node.label_text || cap(node.status)));
  const facts = el("dl", "agent-facts");
  const addFact = (term, value) => { facts.append(el("dt", "", term), el("dd", "", value)); };
  addFact("Task", node.task || "The whole run");
  addFact("Model", node.model || "Not recorded");
  if (node.kind === "subagent") addFact("Subagent", node.identity || "Starting");
  if (node.kind !== "lead") addFact("Backend", node.backend || "Not recorded");
  const phase = node.kind === "lead" ? null : run.phases?.find((item) => item.id === node.task);
  const worker = phase?.workers?.find((item) => item.step_id === node.stepId);
  if (worker?.blocker_code) addFact("Blocker", worker.blocker_code);
  const usage = node.kind === "lead" ? run.context?.lead_usage : run.context?.worker_usage?.[node.backend];
  const notes = [];
  if (usage && Object.values(usage).some((value) => typeof value === "number")) {
    addFact(node.kind === "lead" ? "Observed tokens" : "Backend totals", [usage.input_tokens, usage.output_tokens].every((value) => value == null) ? "Unknown" : `${short(usage.input_tokens)} in · ${short(usage.output_tokens)} out`);
    if (node.kind !== "lead") notes.push("Worker usage is aggregated by backend, not assigned to this individual node.");
  }
  if (node.kind === "subagent" && !run.phases?.find((phase) => phase.id === node.task)?.workers?.some((worker) => worker.step_id.replaceAll("_", " ") === node.label)) notes.push("This subagent's task status is shown; its individual completion time is not recorded.");
  box.append(facts);
  if (node.kind !== "lead") renderRoutingDetail(box, phase, node.stepId);
  for (const note of notes) box.append(el("p", "detail-note", note));
}

function renderRoutingDetail(box, phase, stepId) {
  const routing = phase?.routing;
  const section = el("section", "routing-detail");
  section.append(el("h4", "", "Why this agent"));
  if (!routing) {
    section.append(el("p", "detail-note", "Routing scores were not recorded for this run."));
    box.append(section);
    return;
  }
  const selected = routing.assignments?.find((item) => item.step_id === stepId);
  const purpose = {
    CODEX: "Codex handles the diagnosis, correctness, or review task.",
    CLAUDE: "Claude handles the bounded implementation or operational task.",
    CLAUDE_CODEX: "Claude implements; a fresh Codex context reviews independently.",
    CODEX_CLAUDE_CODEX: "Codex specifies the contract, Claude implements, and fresh Codex reviews.",
  }[phase.route] || "The routing policy selected this execution plan.";
  section.append(el("p", "route-explanation", purpose));
  const facts = el("dl", "agent-facts routing-facts");
  const add = (term, value) => facts.append(el("dt", "", term), el("dd", "", value));
  add("Route", phase.route || "Unknown");
  add("Task type", routing.task_type?.replaceAll("_", " ") || "Unknown");
  add("Ambiguity", `${routing.ambiguity}/10`);
  if (selected) {
    add("Role", selected.role);
    add("Capacity", `${selected.capacity_tier} · ${selected.ambiguity_capacity}/10 ambiguity`);
    add("Tier rule", selected.rule || "Authored plan tier retained");
  }
  const signals = [];
  if (routing.diagnosis_required) signals.push("diagnosis required");
  if (routing.failure_cost !== "low") signals.push(`${routing.failure_cost} failure cost`);
  if (routing.semantic_correctness === "critical") signals.push("critical correctness");
  if (routing.implementation_scope === "broad") signals.push("broad scope");
  if (routing.ambiguity >= 7) signals.push("unresolved consequential choices");
  for (const signal of routing.signals || []) signals.push(signal.replaceAll("_", " "));
  if (routing.fallback_applied) signals.push("fallback route");
  add("Signals", signals.join(" · ") || "Bounded task under the selected policy route");
  add("Matches", routing.matches?.join(" · ") || "Policy fallback");
  if (routing.ambiguity_dimensions) add("Uncertainty", Object.entries(routing.ambiguity_dimensions).map(([key, value]) => `${key} ${value}`).join(" · "));
  if (routing.work_profile) add("Work demand", Object.entries(routing.work_profile).map(([key, value]) => `${key} ${value}`).join(" · "));
  section.append(facts);
  if (selected && routing.ambiguity > selected.ambiguity_capacity) section.append(el("p", "detail-note", "The recorded task score exceeds this worker's declared capacity; review this run's routing data."));
  box.append(section);
}

function renderGarden(run) {
  const stage = $("garden-stage");
  const focused = document.activeElement?.dataset?.agent;
  stage.replaceChildren();
  if (!run) { stage.append(el("p", "garden-empty", "A quiet garden. Start a run to watch the team grow.")); renderAgentDetail(null); return; }
  const agents = agentNodes(run);
  if (state.agent !== "lead" && !agents.some((node) => node.key === state.agent)) state.agent = "lead";
  const lead = { key: "lead", kind: "lead", label: "Lead agent", status: normalizedStatus(run.status), model: run.metrics?.lead_model || run.context?.lead_usage?.model, task: "The whole run" };
  const positions = new Map();
  const subagents = agents.filter((node) => node.kind === "subagent");
  const workers = agents.filter((node) => node.kind === "worker");
  const arrange = (group, rx, ry, start) => group.forEach((node, index) => { const angle = start + 2 * Math.PI * index / Math.max(group.length, 1); positions.set(node.key, { x: 350 + rx * Math.cos(angle), y: 235 + ry * Math.sin(angle) }); });
  arrange(subagents, 165, 135, -Math.PI / 2);
  arrange(workers, 280, 185, -Math.PI / 2 + 1);
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 700 470"); svg.setAttribute("class", "garden-edges"); svg.setAttribute("aria-hidden", "true");
  for (const node of agents) {
    const related = node.kind === "worker" ? subagents.find((item) => item.task === node.task) : null;
    gardenLine(svg, { x: 350, y: 235 }, positions.get(node.key));
    if (related) gardenLine(svg, positions.get(related.key), positions.get(node.key), true);
  }
  stage.append(svg, nodeButton(lead, 350, 235));
  for (const node of agents) { const at = positions.get(node.key); stage.append(nodeButton(node, at.x, at.y)); }
  if (state.gardenCenteredFor !== run.id) {
    stage.parentElement.scrollLeft = Math.max(0, (stage.scrollWidth - stage.parentElement.clientWidth) / 2);
    state.gardenCenteredFor = run.id;
  }
  renderAgentDetail(run, agents.find((node) => node.key === state.agent) || lead);
  if (focused) stage.querySelectorAll(".agent-node").forEach((item) => { if (item.dataset.agent === focused) item.focus({ preventScroll: true }); });
}

function timelineMoments(run) {
  if (!run) return [];
  if (run.activity?.length) return run.activity.map((event) => {
    const actor = event.tool === "spawn_subagent" ? "Subagent" : event.tool === "ClaudeWorker" ? "Claude worker" : "Worker";
    const action = event.status === "started" ? "started" : event.status === "spawned" ? "joined" : event.tool === "spawn_subagent" && event.status === "finished" ? "spawn request finished" : event.status === "finished" ? "finished" : event.status === "failed" ? "failed" : cap(event.status);
    return { at: event.at, status: event.status === "spawned" || event.status === "started" ? "active" : event.status === "failed" ? "failed" : "completed", title: `${actor} ${action}`, detail: [event.task_id, event.step_id].filter(Boolean).join(" · ") };
  });
  return (run.phases || []).map((phase) => ({ at: null, status: normalizedStatus(phase.status), title: phase.id, detail: `Phase ${statusLabel(run, phase)}` }));
}

function renderTimeline(run) {
  const all = timelineMoments(run);
  const full = state.view === "timeline";
  const shown = full ? all : all.slice(-5);
  const list = $("run-timeline");
  const previousScroll = list.scrollLeft;
  const wasAtEnd = list.scrollWidth - list.clientWidth - previousScroll < 24;
  list.replaceChildren();
  $("timeline-count").textContent = `${all.length} ${all.length === 1 ? "moment" : "moments"}`;
  $("timeline-caption").textContent = run?.activity?.length ? "Recorded events, oldest to newest" : "Phase order; individual event times unavailable";
  if (!shown.length) { list.append(el("p", "empty", "No task events recorded yet.")); return; }
  for (const item of shown) {
    const card = el("article", `timeline-moment ${item.status}`);
    card.append(el("span", "moment-dot", item.status === "completed" ? "✓" : item.status === "failed" ? "×" : item.status === "blocked" ? "!" : "●"), el("time", "", timeLabel(item.at)), el("strong", "", item.title), el("small", "", item.detail));
    list.append(card);
  }
  if (full) list.scrollLeft = state.followTimeline || wasAtEnd ? list.scrollWidth : previousScroll;
}

function renderConversation(run) {
  const list = $("conversation-list"); list.replaceChildren();
  // The parent's recorded interactions, plus what the extension recorded for each worker call: the start of
  // its contract (directive) and the start of its result (feedback). Text only; the full text is in the
  // Pi session transcript.
  const recorded = (run?.interactions || []).map((item) => ({ ...item, label: String(item.sequence), id: `conversation-step-${item.sequence}` }));
  const observed = (run?.activity || []).flatMap((entry, index) => {
    const text = entry.status === "started" ? entry.prompt_excerpt || entry.goal : entry.result_summary;
    if (!text || !entry.task_id || !entry.step_id) return [];
    return [{ type: entry.status === "started" ? "directive" : "feedback",
      summary: entry.status === "started" ? [entry.goal, entry.prompt_excerpt].filter(Boolean).join(" — ") : text, task_id: entry.task_id, step_id: entry.step_id,
      subagent_id: entry.subagent_id, at: entry.at, label: "•", id: `conversation-activity-${index}`, commit: entry.commit_sha, failed: entry.status === "failed" }];
  });
  const items = [...recorded, ...observed].sort((a, b) => String(a.at || "").localeCompare(String(b.at || "")));
  $("conversation-count").textContent = `${items.length} ${items.length === 1 ? "step" : "steps"}`;
  if (!items.length) {
    list.append(el("p", "empty", "No worker contracts, results or decisions were recorded for this run."));
    return;
  }
  for (const item of items) {
    const card = el("article", `conversation-card ${item.type}`);
    card.id = item.id;
    const head = el("div", "conversation-card-head");
    head.append(el("span", "conversation-number", item.label),
      el("strong", "", item.type === "directive" ? "Parent → worker" : item.type === "feedback" ? "Worker → parent" : "Parent decides"),
      el("time", "", timeLabel(item.at)));
    card.append(head, el("p", "conversation-summary", item.summary));
    if (item.commit) card.append(el("small", "", `commit ${item.commit}`));
    const footer = el("div", "conversation-card-footer");
    footer.append(el("span", "", `${item.task_id} · ${item.step_id}${item.subagent_id ? ` · ${item.subagent_id}` : ""}`));
    if (item.reply_to != null) {
      const link = el("a", "conversation-reply", `↳ follows #${item.reply_to}`);
      link.href = `#conversation-step-${item.reply_to}`;
      link.addEventListener("click", (event) => {
        event.preventDefault();
        const target = $(`conversation-step-${item.reply_to}`);
        target?.scrollIntoView({ behavior: "smooth", block: "center" });
        target?.classList.add("conversation-highlight");
        setTimeout(() => target?.classList.remove("conversation-highlight"), 1800);
      });
      footer.append(link);
    }
    const showAgent = el("button", "conversation-agent-link", "Show agent ↗");
    showAgent.type = "button";
    showAgent.addEventListener("click", () => {
      const node = item.type === "feedback"
        ? agentNodes(run).find((agent) => agent.stepId === item.step_id && agent.task === item.task_id)
        : null;
      state.agent = node?.key || "lead";
      state.view = "garden";
      renderObservatory(run);
      $("garden-stage").scrollIntoView({ behavior: "smooth", block: "center" });
    });
    footer.append(showAgent); card.append(footer); list.append(card);
  }
}

function renderObservatory(run) {
  const garden = state.view === "garden", conversation = state.view === "conversation", timeline = state.view === "timeline";
  $("observatory-eyebrow").textContent = garden ? "THE GARDEN" : conversation ? "THE CONVERSATION" : "THE TIMELINE";
  $("observatory-title").textContent = garden ? "Who's here" : conversation ? "How the work unfolded" : "When it happened";
  $("garden-layout").hidden = !garden;
  $("conversation-layout").hidden = !conversation;
  $("timeline-layout").hidden = conversation;
  $("timeline-layout").classList.toggle("expanded", timeline);
  for (const [name, selected] of [["garden", garden], ["conversation", conversation], ["timeline", timeline]]) {
    $(`${name}-view`).classList.toggle("selected", selected);
    $(`${name}-view`).setAttribute("aria-pressed", String(selected));
  }
  if (garden) renderGarden(run);
  if (conversation) renderConversation(run);
  if (!conversation) renderTimeline(run);
}
function render(repository) { renderPicker(repository); const run = selectedRun(); renderOverview(run); renderCompletion(run); renderReport(run); renderObservatory(run); renderTasks(run); renderActivity(run); setStatus(); }
// Workspace mode: one mission across repositories. States are the scheduler's own.
const wsTone = (value) => {
  const s = String(value || "");
  if (s === "running") return "active";
  if (["completed", "validated", "ready_for_downstream", "merged", "declared_merged", "declared_ready_for_downstream"].includes(s)) return "completed";
  if (["failed", "cancelled", "refused"].includes(s)) return "failed";
  if (["partial", "needs_reconciliation", "blocked_by_failure", "not_dispatchable", "held_by_budget", "awaiting_authorization", "waiting_for_authorization"].includes(s)) return "blocked";
  return "waiting";
};
const wsLabel = (value) => ({ running: "Running", completed: "Completed", partial: "Partial", failed: "Failed", cancelled: "Cancelled", refused: "Refused at dispatch", needs_reconciliation: "Needs reconciliation", blocked_by_failure: "Blocked by failure", waiting_for_readiness: "Waiting for readiness", not_dispatchable: "Not dispatchable", not_launched: "Not launched", validated: "Validated", ready_for_downstream: "Ready for downstream", merged: "Merged", held_by_budget: "Held by budget", awaiting_authorization: "Awaiting authorization", waiting_for_authorization: "Waiting for authorization" })[value] || (String(value || "").startsWith("declared_") ? `Declared ${cap(value.slice(9))}` : cap(value));
const wsGlyph = (tone) => tone === "completed" ? "✿" : tone === "failed" ? "×" : tone === "blocked" ? "!" : tone === "active" ? "●" : "○";
const tokenSum = (tokens) => tokens?.input_tokens == null && tokens?.output_tokens == null ? null : (tokens.input_tokens || 0) + (tokens.output_tokens || 0) + (tokens.cache_write_tokens || 0);
// Cost is a lower bound when a participant reported tokens but no cost. Plain text only.
const costText = (cost, complete, unpriced) => typeof cost !== "number" ? (complete === false ? "unknown" : "—")
  : `${complete === false ? "≥ " : ""}$${cost.toFixed(3)}${complete === false ? ` (lower bound${unpriced?.length ? `; excludes ${unpriced.join(", ")}` : ""})` : ""}`;
const stamp = (value) => value ? new Date(value).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—";
function runAnchor(runId, text) {
  const anchor = el("a", "ws-run-link", text);
  anchor.href = `#run=${encodeURIComponent(runId)}`;
  return anchor;
}
function prAnchor(link) {
  const anchor = el("a", "pr-link", `PR #${link.number} ↗`);
  anchor.href = link.url; anchor.target = "_blank"; anchor.rel = "noopener noreferrer";
  return anchor;
}
async function refreshWorkspace() {
  const response = await fetch("/api/workspace", { cache: "no-store", headers: tokenHeaders() });
  const data = await response.json().catch(() => ({ ok: false, error_code: `HTTP_${response.status}`, error: "The workspace endpoint did not answer." }));
  state.workspace = data;
}
function applyMode() {
  const workspace = Boolean(state.workspaceMode);
  const match = /^#run=(.+)$/.exec(location.hash);
  if (match) {
    const id = decodeURIComponent(match[1]);
    if (state.runs.some((run) => run.id === id) && state.selected !== id) { state.selected = id; state.manuallySelected = true; state.agent = "lead"; state.gardenCenteredFor = null; }
  }
  state.mode = workspace && !match && location.hash !== "#runs" ? "workspace" : "runs";
  $("mode-switch").hidden = !workspace;
  $("workspace-view").hidden = state.mode !== "workspace";
  $("run-view").hidden = state.mode === "workspace";
  for (const [id, selected] of [["workspace-mode", state.mode === "workspace"], ["runs-mode", state.mode === "runs"]]) {
    $(id).classList.toggle("selected", selected); $(id).setAttribute("aria-pressed", String(selected));
  }
  if (state.mode === "workspace") renderWorkspace(state.workspace);
}
function renderWorkspaceOverview(data) {
  const ok = data?.ok;
  const tasks = ok ? data.repositories.flatMap((repo) => repo.tasks) : [];
  const tones = tasks.map((task) => wsTone(task.state));
  const done = tasks.filter((task) => ["completed", "partial", "validated", "ready_for_downstream", "merged"].includes(task.state)).length;
  $("ws-name").textContent = data?.workspace_id || state.workspaceId || "Unknown workspace";
  $("ws-title").textContent = data?.workspace_id || state.workspaceId || "Workspace";
  $("ws-updated").textContent = ok ? (data.record_found ? `Record updated ${stamp(data.updated_at)} · from the ${data.source}` : "Not scheduled yet · showing the manifest plan") : "Unavailable";
  const pill = $("ws-state");
  const running = tones.includes("active");
  const awaiting = ok && data.status === "AWAITING_AUTHORIZATION";
  pill.textContent = !ok ? "Unavailable" : data.status === "ATTENTION" ? "Needs attention" : awaiting ? "Awaiting your approval" : running ? "In progress" : "OK";
  pill.className = `state-pill ${!ok || data.status === "ATTENTION" || awaiting ? "blocked" : running ? "in_progress" : "completed"}`;
  const message = $("ws-message");
  let copy;
  if (!ok) copy = ["failed", "!", "This workspace cannot be shown", `${data?.error_code || "WORKSPACE_UNAVAILABLE"} · ${data?.error || "No detail available."}`];
  else if (data.status === "ATTENTION") {
    const bad = tasks.filter((task) => ["failed", "cancelled", "refused", "needs_reconciliation", "blocked_by_failure"].includes(task.state));
    copy = ["blocked", "!", "Some tasks need attention", bad.map((task) => `${task.id}: ${wsLabel(task.state).toLowerCase()}`).join(" · ")];
  } else if (awaiting) {
    // Not a failure: these tasks launched nothing, or their run stopped before changing anything.
    copy = ["blocked", "⚿", "Your authorization is needed", (data.approval_requests || []).map((request) => `${request.github || request.repository} needs ${request.code === "MUTATION_NOT_AUTHORIZED" ? "allow mutation when scheduling" : request.missing.join(" and ") || request.code} for ${request.tasks.join(", ")}`).join(" · ") || "A task's run asked for approval again; see its card."];
  } else if (running) copy = ["in_progress", "✦", "Work in motion", `${tones.filter((tone) => tone === "active").length} child runs active · ${done} of ${tasks.length} tasks finished.`];
  else if (tasks.length && tasks.every((task) => ["completed", "partial", "validated", "ready_for_downstream", "merged", "declared_merged", "declared_ready_for_downstream"].includes(task.state))) copy = ["completed", "✿", "Every task has finished", `${done} of ${tasks.length} tasks ran to completion${done < tasks.length ? "; the others were declared done" : ""}.`];
  else copy = ["waiting", "◒", "Waiting for the next frontier", `${done} of ${tasks.length} tasks finished. Waiting tasks dispatch once their upstream tasks are declared ready.`];
  message.className = `run-message ${copy[0]}`;
  $("ws-message-icon").textContent = copy[1]; $("ws-message-title").textContent = copy[2]; $("ws-message-detail").textContent = copy[3];
  $("ws-tasks").textContent = ok ? number(tasks.length) : "—";
  $("ws-tasks-caption").textContent = ok ? Object.entries(data.counts || {}).map(([key, value]) => `${value} ${wsLabel(key).toLowerCase()}`).join(" · ") || "across repositories" : "across repositories";
  $("ws-repo-count").textContent = ok ? number(data.repositories.length) : "—";
  $("ws-completed").textContent = ok ? number(done) : "—";
  const tokens = ok ? data.tokens : null;
  $("ws-total-tokens").textContent = short(tokenSum(tokens));
  $("ws-tokens-caption").textContent = !tokens?.runs ? "no child run tokens yet" : `input + output · ${tokens.runs} ${tokens.runs === 1 ? "run" : "runs"}`;
  $("ws-reported").textContent = tokens?.total_tokens == null ? "—" : `${short(tokens.total_tokens)}${tokens.total_complete ? "" : " (partial)"}`;
  $("ws-input").textContent = short(tokens?.input_tokens);
  $("ws-output").textContent = short(tokens?.output_tokens);
  $("ws-cost").textContent = costText(tokens?.estimated_cost_usd, tokens?.cost_complete, tokens?.unpriced);
  $("ws-parallel").textContent = ok && data.max_parallel != null ? number(data.max_parallel) : "—";
  $("ws-plan").textContent = ok ? (data.plan_status ? cap(data.plan_status.toLowerCase()) : "record only") : "—";
  $("ws-wave-count").textContent = ok && data.waves.length ? `${data.waves.length} ${data.waves.length === 1 ? "wave" : "waves"}` : "waves unknown";
  const budget = ok ? data.budget : null;
  $("ws-budget-wrap").hidden = !budget; $("ws-budget-meter").hidden = !budget?.max_tokens;
  if (budget) {
    const parts = [];
    if (budget.max_tokens) parts.push(`${short(budget.spent_tokens)} / ${short(budget.max_tokens)} tokens`);
    if (budget.max_cost_usd) parts.push(`${budget.cost_complete === false ? "≥ " : ""}$${budget.spent_cost_usd.toFixed(2)} / $${budget.max_cost_usd.toFixed(2)} reported`);
    if (budget.cost_complete === false && budget.unpriced?.length) parts.push(`cost is a lower bound, excludes ${budget.unpriced.join(", ")}`);
    if (budget.incomplete) parts.push("in-flight spend unknown: nothing new will launch");
    $("ws-budget").textContent = parts.join(" · ") + (budget.exhausted ? " · exhausted" : "") + " · launch gate: running children can overshoot";
    if (budget.max_tokens) {
      $("ws-budget-bar").style.width = `${Math.min(100, 100 * budget.spent_tokens / budget.max_tokens)}%`;
      $("ws-budget-meter").classList.toggle("over", budget.exhausted);
    }
  }
}
function duration(seconds) {
  const s = Math.max(0, Math.round(seconds)), h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h ? `${h}h ${m}m` : m ? `${m}m ${s % 60}s` : `${s}s`;
}
function workspaceTaskCard(task) {
  const tone = wsTone(task.state);
  const card = el("article", `ws-task ${tone}`);
  card.id = workspaceTaskDomId(task.repository_id, task.id);
  card.tabIndex = -1;
  const head = el("div", "ws-task-head");
  head.append(el("span", `task-glyph ws-glyph ${tone}`, wsGlyph(tone)));
  const name = el("span", "task-name");
  const meta = [task.wave == null ? "No wave" : `Wave ${task.wave + 1}`];
  if (task.readiness) meta.push(cap(task.readiness));
  if (task.launches) meta.push(`${task.launches} ${task.launches === 1 ? "launch" : "launches"}`);
  name.append(el("strong", "", task.id), el("small", "", meta.join(" · ")));
  const display = controlState.panels?.action_view?.states?.find((row) => row.id === task.id)?.display;
  head.append(name, el("span", `ws-state ${tone}`, display?.native === task.state ? display.label : wsLabel(task.state)));
  card.append(head);
  const facts = el("div", "ws-task-facts");
  if (task.blocked_by?.length) facts.append(el("p", "ws-chip blocked", `Blocked by ${task.blocked_by.join(", ")}`));
  else if (task.depends_on?.length) facts.append(el("p", "ws-chip", `After ${task.depends_on.join(", ")}`));
  if (task.blocks?.length) facts.append(el("p", "ws-chip quiet", `Unblocks ${task.blocks.join(", ")}`));
  const blocker = task.refusal_code || task.run_blocker_code || task.run?.blocker_code;
  const awaiting = task.state === "awaiting_authorization";
  if (blocker) facts.append(el("p", `ws-chip ${awaiting ? "blocked" : "failed"}`, blocker));
  if (awaiting && task.authorization?.status === "awaiting") facts.append(el("p", "ws-chip blocked", task.authorization.missing.length ? `Needs approval: ${task.authorization.missing.join(", ").replaceAll("_", " ")}` : "Needs allow mutation when scheduling"));
  else if (awaiting) facts.append(el("p", "ws-chip blocked", "Asked for approval again under the same authorization: inspect its run; not retried"));
  else if (task.authorization?.status === "awaiting" && !["completed", "partial", "running", "validated", "ready_for_downstream", "merged"].includes(task.state)) facts.append(el("p", "ws-chip quiet", `Will need approval: ${task.authorization.missing.join(", ").replaceAll("_", " ") || task.authorization.code}`));
  if (task.previous_attempts?.length) facts.append(el("p", "ws-chip quiet", `Attempt ${task.attempt || task.previous_attempts.length + 1} · after ${task.previous_attempts.map((a) => `${a.run_id || "no run"} (${[a.run_status, a.run_blocker_code].filter(Boolean).join(" ") || "unknown"})`).join(", ")}`));
  const limit = task.time_limit;
  if (limit?.max_duration_seconds) {
    const text = limit.stop_reason ? `Stopped by the ${duration(limit.max_duration_seconds)} time limit${limit.elapsed_seconds != null ? ` after ${duration(limit.elapsed_seconds)}` : ""}`
      : limit.remaining_seconds != null ? `Time limit ${duration(limit.max_duration_seconds)} · ${duration(limit.remaining_seconds)} left`
      : `Time limit ${duration(limit.max_duration_seconds)}`;
    facts.append(el("p", limit.stop_reason ? "ws-chip failed" : "ws-chip", text));
  }
  const calls = task.lead_call_limit;
  if (calls?.max_lead_calls) {
    facts.append(el("p", calls.stopped ? "ws-chip failed" : "ws-chip", `Lead calls ${calls.calls ?? "?"} of ${calls.max_lead_calls}${calls.stopped ? " · stopped by LEAD_CALL_LIMIT" : ""}`));
  }
  if (task.run_id) {
    const line = el("p", "ws-run");
    const status = task.run?.status ? cap(task.run.status.toLowerCase()) : "record unavailable";
    line.append(runAnchor(task.run_id, `${task.run_id} ↗`), el("span", "", ` ${status}${task.run?.phases_total ? ` · ${task.run.phases_completed}/${task.run.phases_total} phases` : ""}`));
    facts.append(line);
  }
  const total = tokenSum(task.tokens);
  if (task.tokens && total != null) facts.append(el("p", "ws-tokens", `${short(task.tokens.input_tokens)} in · ${short(task.tokens.output_tokens)} out${task.tokens.total_tokens == null ? " · no reported total" : ` · ${short(task.tokens.total_tokens)} reported total`}`));
  if (task.links?.length) { const links = el("div", "run-links"); for (const link of task.links) if (link.kind === "pr") links.append(prAnchor(link)); facts.append(links); }
  if (facts.childNodes.length) card.append(facts);
  const actions = taskActions(task);
  if (actions) card.append(actions);
  return card;
}
function renderWorkspaceBoard(data) {
  const board = $("ws-repos"); board.replaceChildren();
  if (!data?.ok) { board.append(el("p", "empty", "The workspace cannot be read. Check the id or manifest passed to --workspace.")); return; }
  if (!data.repositories.length) { board.append(el("p", "empty", "This workspace has no recorded tasks yet.")); return; }
  for (const repo of data.repositories) {
    const column = el("section", "ws-column");
    const head = el("div", "ws-column-head");
    const title = el("div", "");
    title.append(el("strong", "", repo.repository || repo.id), el("small", "", [repo.id, repo.pinned_commit ? `@ ${repo.pinned_commit.slice(0, 12)}` : null].filter(Boolean).join(" ")));
    const total = tokenSum(repo.tokens);
    head.append(title, el("span", "soft-count", `${repo.tasks.length} ${repo.tasks.length === 1 ? "task" : "tasks"}${total != null ? ` · ${short(total)} tok` : ""}`));
    column.append(head);
    if (repo.blocker_code) column.append(el("p", "ws-chip blocked", `Repository: ${repo.blocker_code}`));
    for (const request of (data.approval_requests || []).filter((item) => item.repository === repo.id)) column.append(approvalBox(request));
    if (!repo.tasks.length) column.append(el("p", "empty", "No tasks in this repository."));
    for (const task of repo.tasks) column.append(workspaceTaskCard({ ...task, repository_id: repo.id }));
    board.append(column);
  }
}
function workspaceTaskDomId(repository, task) {
  return `ws-task-${encodeURIComponent(repository || "unknown")}-${encodeURIComponent(task || "unknown")}`;
}
const attentionReasons = {
  awaiting_authorization: "Local mutation needs authorization before this task can launch.",
  waiting_for_authorization: "An upstream task needs authorization before this one can launch.",
  needs_reconciliation: "This interrupted attempt needs reconciliation before work continues.",
  blocked_by_failure: "An upstream task failed or was cancelled.",
  held_by_budget: "The workspace budget is holding new launches.",
  not_dispatchable: "The scheduler cannot dispatch this task in its current state.",
  failed: "The child run failed. Inspect its run before deciding how to recover.",
  refused: "The task was refused at dispatch. Inspect its blocker before trying again.",
  cancelled: "The child run was cancelled. Inspect its run before creating new work.",
  partial: "The child run ended with unfinished work. Inspect the recorded outcome."
};
function attentionRows(data) {
  if (!data?.ok) return [];
  if (Array.isArray(data.attention)) return data.attention;
  // Older Garden servers omit attention. Keep this read-only and derive only from recorded states.
  return data.repositories.flatMap((repo) => repo.tasks.filter((task) => attentionReasons[task.state]).map((task) => ({
    task_id: task.id, repository: repo.id, github: repo.repository, state: task.state,
    reason_code: task.refusal_code || task.run_blocker_code || null,
    reason: attentionReasons[task.state], blocked_by: task.blocked_by || [], run_id: task.run_id || null
  })));
}
function renderWorkspaceAttention(data) {
  const list = $("ws-attention-list"); list.replaceChildren();
  const rows = attentionRows(data);
  $("ws-attention-count").textContent = !data?.ok ? "Unavailable" : `${rows.length} ${rows.length === 1 ? "task" : "tasks"}`;
  if (!data?.ok) { list.append(el("p", "empty", "Attention cannot be checked while the workspace is unavailable.")); return; }
  if (!rows.length) { list.append(el("p", "empty", "No tasks need a decision right now. Review the repository board for running and waiting work.")); return; }
  for (const row of rows) {
    const tone = wsTone(row.state);
    const item = el("article", `ws-attention-item ${tone}`);
    const heading = el("div", "ws-attention-heading");
    const title = el("div");
    title.append(el("strong", "", row.task_id || "Task"), el("small", "", row.github || row.repository || "Repository unavailable"));
    heading.append(title, el("span", `ws-state ${tone}`, wsLabel(row.state)));
    item.append(heading);
    item.append(el("p", "ws-attention-reason", row.reason || attentionReasons[row.state] || "Inspect this task's recorded state."));
    if (row.reason_code) item.append(el("small", "ws-attention-code", row.reason_code));
    if (row.blocked_by?.length) item.append(el("small", "ws-attention-code", `Blocked by ${row.blocked_by.join(", ")}`));
    const links = el("div", "ws-attention-links");
    const target = workspaceTaskDomId(row.repository, row.task_id);
    const locate = el("button", "ws-attention-link", "View task"); locate.type = "button";
    locate.addEventListener("click", () => {
      const card = document.getElementById(target);
      card?.scrollIntoView({ behavior: "smooth", block: "center" }); card?.focus({ preventScroll: true });
    });
    links.append(locate);
    if (row.run_id) links.append(runAnchor(row.run_id, "View child run ↗"));
    item.append(links); list.append(item);
  }
}
function approvalBox(request) {
  // The repository-specific approval request; the action itself is previewed and confirmed like any other.
  const box = el("div", "ws-approval");
  const flag = request.code === "MUTATION_NOT_AUTHORIZED";
  box.append(el("strong", "", flag ? "Allow mutation to launch" : `Approve local mutation · ${request.missing.join(", ").replaceAll("_", " ")}`));
  box.append(el("small", "", `${request.code} · ${request.tasks.join(", ")}`));
  const steps = flag ? ["The committed policy already grants worktrees and commits.", "Schedule again with allow mutation checked."]
    : [request.policy_source === "default" ? "No .pi/autonomy.yaml is committed at the pinned commit: the default policy grants no mutation." : "The committed .pi/autonomy.yaml does not grant these.",
      ...(request.uncommitted_policy ? ["The checkout's own .pi/autonomy.yaml is uncommitted: it does not count yet."] : []),
      "1. Approve: grants only these permissions in .pi/autonomy.yaml (you review the diff).",
      `2. Commit the policy on ${request.base_branch || "the base branch"}.`, "3. Set this repository's pinned_commit in the manifest to that commit.",
      "4. Schedule with allow mutation: each blocked task starts a new attempt linked to the one that stopped."];
  const list = el("ol", "ws-approval-steps"); for (const step of steps) list.append(el("li", "", step)); box.append(list);
  box.append(el("small", "", `Never granted by this: ${request.never_granted.join(", ").replaceAll("_", " ")}.`));
  if (controlState.enabled && !flag && request.missing.length) {
    const button = el("button", "ws-action", "Approve local mutation…"); button.type = "button";
    button.addEventListener("click", () => workspaceAction("approve_mutation", { repository: request.repository }, `${request.repository} · local mutation`));
    box.append(button);
  }
  return box;
}
function renderWorkspaceTokens(data) {
  const box = $("ws-token-rows"); box.replaceChildren();
  const repos = data?.ok ? data.repositories.filter((repo) => repo.tokens?.runs) : [];
  if (!repos.length) { box.append(el("p", "empty", "No child run has reported tokens yet.")); return; }
  const values = repos.map((repo) => tokenSum(repo.tokens) || 0);
  const maximum = Math.max(...values, 1);
  repos.forEach((repo, index) => {
    const row = el("div", "token-row ws-token-row");
    const track = el("div", "token-track"); const bar = el("i", "lead-token-bar"); bar.style.width = `${100 * values[index] / maximum}%`; track.append(bar);
    row.append(el("span", "", repo.id), track, el("b", "", short(values[index])));
    box.append(row);
  });
  const totals = el("div", "token-report-heading");
  totals.append(el("strong", "", `Workspace · ${short(tokenSum(data.tokens))}`), el("small", "", data.tokens.total_complete ? `${short(data.tokens.total_tokens)} reported total` : "Some runs did not report a total"));
  box.append(totals);
}
function renderWorkspaceSessions(data) {
  const list = $("ws-sessions"); list.replaceChildren();
  const sessions = data?.ok ? data.sessions : [];
  $("ws-session-count").textContent = `${sessions.length} ${sessions.length === 1 ? "session" : "sessions"}`;
  if (!sessions.length) { list.append(el("p", "empty", "No scheduler session has run yet.")); return; }
  for (const session of sessions) {
    const tone = !session.finished_at ? "started" : session.status === "ATTENTION" ? "failed" : session.status === "AWAITING_AUTHORIZATION" ? "blocked" : "finished";
    const row = el("div", `activity-item ${tone}`);
    const copy = el("div", "");
    const parts = [`${session.dispatched.length} dispatched`];
    if (session.resumed.length) parts.push(`${session.resumed.length} resumed`);
    if (session.observed_running.length) parts.push(`${session.observed_running.length} observed running`);
    const refused = Object.entries(session.refused || {});
    if (refused.length) parts.push(`${refused.length} refused`);
    copy.append(el("p", "", `${!session.finished_at ? "Scheduler running" : session.status === "ATTENTION" ? "Scheduler needs attention" : session.status === "AWAITING_AUTHORIZATION" ? "Scheduler awaits your authorization" : "Scheduler finished"} · cap ${session.cap ?? "—"} · ${parts.join(" · ")}`));
    const detail = [...session.dispatched.map((id) => `→ ${id}`), ...session.resumed.map((id) => `↻ ${id}`), ...refused.map(([id, code]) => `× ${id} ${code}`),
      ...(session.held_by_budget || []).map((id) => `⏸ ${id} held by budget`),
      ...(session.awaiting_authorization || []).map((id) => `⚿ ${id} awaiting authorization`), ...(session.new_attempts || []).map((id) => `↻ ${id} new attempt`)];
    if (detail.length) copy.append(el("small", "ws-session-tasks", detail.join("  ")));
    copy.append(el("small", "", `${stamp(session.started_at)}${session.finished_at ? ` → ${stamp(session.finished_at)}` : " · in progress"}`));
    row.append(el("span", "activity-icon", tone === "started" ? "↗" : tone === "failed" ? "!" : tone === "blocked" ? "⚿" : "✓"), copy);
    list.append(row);
  }
}
function renderWorkspace(data) { renderWorkspaceOverview(data); renderWorkspaceBoard(data); renderWorkspaceAttention(data); renderWorkspaceTokens(data); renderWorkspaceSessions(data); renderControls(); setStatus(); }
// Controls (pico watch --controls): every action previews first and runs only after confirmation.
// The session token arrives in the launch URL's fragment and is kept in memory only.
const controlState = { token: null, enabled: false, panels: null, fetchedAt: 0 };
// With controls on, the server also requires the token to read panels and branches.
const tokenHeaders = () => controlState.token ? { "X-Pico-Token": controlState.token } : {};
(() => {
  const match = /(?:^#|&)token=([A-Za-z0-9_-]{20,})/.exec(location.hash);
  if (match) { controlState.token = match[1]; history.replaceState(null, "", location.pathname); }
})();
async function refreshControls(force = false) {
  if (!force && Date.now() - controlState.fetchedAt < 10000) return;
  controlState.fetchedAt = Date.now();
  try {
    const response = await fetch("/api/controls", { cache: "no-store", headers: tokenHeaders() });
    const data = await response.json();
    controlState.panels = data.ok ? data : null;
    controlState.enabled = Boolean(data.ok && data.enabled && controlState.token);
  } catch { controlState.panels = null; controlState.enabled = false; }
}
function renderControls() {
  const panels = controlState.panels;
  $("ws-controls").hidden = !panels;
  const mode = $("footer-mode");
  if (mode) mode.textContent = controlState.enabled ? "Local · actions on, each one confirmed · refreshes every 2 seconds" : "Local, read-only · refreshes every 2 seconds";
  if (!panels) return;
  $("ws-controls-state").textContent = controlState.enabled ? "actions on" : panels.enabled ? "actions need the launch URL" : "read-only";
  $("ws-perm-note").textContent = controlState.enabled
    ? "Select a permission to change it. You review the exact diff of .pi/autonomy.yaml first; merge, deploy and mark-ready stay file-only. Publishing (push branch, create PR) reads the policy committed on the default branch on GitHub, so a change here has no effect on it until it is committed and pushed to the default branch."
    : "Read from each checkout's autonomy policy. Change them in the file. Publishing reads the policy on the default branch on GitHub, not this checkout.";
  $("ws-model-note").textContent = controlState.enabled
    ? "Select a model to change it for that tier. The next run uses the new value."
    : "Change them with pico models set. The next run uses the new value.";
  const list = $("ws-permissions"); list.replaceChildren();
  for (const repo of panels.repositories) {
    const box = el("div", "perm-repo");
    const title = el("strong", "", repo.repository || repo.id);
    if (repo.policy_uncommitted) {
      title.append(el("span", "perm-badge", "uncommitted"));
      if (controlState.enabled) {
        const commit = el("button", "ws-action perm-commit", "Commit policy"); commit.type = "button";
        commit.addEventListener("click", () => workspaceAction("commit_policy", { repository: repo.id }, repo.id));
        title.append(commit);
      }
    }
    box.append(title);
    if (repo.error_code) { box.append(el("small", "", repo.error_code)); list.append(box); continue; }
    box.append(el("small", "", `${repo.source} · profile ${repo.profile || "—"} · PRs per ${repo.pr_granularity || "—"} · issues ${repo.issue_reference === "closes" ? "closed on merge" : "referenced (Refs)"} · base ${repo.base_branch}`));
    const flags = el("div", "perm-flags");
    const editable = new Set(controlState.enabled ? panels.editable_permissions || [] : []);
    for (const [name, on] of Object.entries(repo.permissions)) {
      const locked = !editable.has(name);
      const flag = el(locked ? "span" : "button", `perm-flag ${["deploy", "mark_pr_ready"].includes(name) ? "locked" : on ? "on" : ""}`, `${on ? "✓" : "×"} ${name.replaceAll("_", " ")}`);
      if (!locked) {
        flag.type = "button"; flag.title = `${on ? "Deny" : "Allow"} ${name.replaceAll("_", " ")} for ${repo.repository}`;
        flag.addEventListener("click", () => workspaceAction("set_permission", { repository: repo.id, permission: name, value: !on }, `${repo.id} · ${name}`));
      }
      flags.append(flag);
    }
    flags.append(el("span", "perm-flag locked", `${repo.merge_enabled ? "✓" : "×"} merge (never from the Garden)`));
    box.append(flags);
    const limits = Object.entries(repo.limits || {}).map(([key, value]) => `${key.replaceAll("_", " ")} ${value}`).join(" · ");
    if (limits) box.append(el("small", "", limits));
    list.append(box);
  }
  const models = panels.models; const box = $("ws-models"); box.replaceChildren();
  $("ws-model-source").textContent = models.error_code ? "unavailable" : models.configured ? "user config" : "defaults";
  if (models.error_code) { box.append(el("p", "empty", models.error_code)); return; }
  const families = Object.keys(models.models); const tiers = families.length ? Object.keys(models.models[families[0]]) : [];
  const table = el("table"); const head = el("tr"); head.append(el("th", "", "Tier"), ...families.map((family) => el("th", "", family)));
  table.append(head);
  for (const tier of tiers) {
    const row = el("tr"); row.append(el("td", "", tier));
    for (const family of families) {
      const cell = el("td");
      if (controlState.enabled) {
        const button = el("button", "model-cell", models.models[family][tier]); button.type = "button";
        button.addEventListener("click", () => modelDialog(family, tier, models)); cell.append(button);
      } else cell.textContent = models.models[family][tier];
      row.append(cell);
    }
    table.append(row);
  }
  box.append(table);
  renderActionsBar();
}
function renderActionsBar() {
  const bar = $("ws-actions-bar"); bar.replaceChildren();
  bar.hidden = !controlState.enabled;
  if (!controlState.enabled) return;
  const scheduler = controlState.panels?.scheduler || {};
  const button = el("button", "ws-action", scheduler.running ? "Scheduler running…" : "Schedule now…"); button.type = "button"; button.disabled = Boolean(scheduler.running);
  button.addEventListener("click", () => scheduleDialog(false));
  bar.append(button);
  const schedule = controlState.panels?.action_view?.actions?.find((item) => item.action === "schedule");
  if (schedule && !schedule.available) bar.append(el("small", "", `${schedule.refusal_code}: ${schedule.explanation}`));
  const tasks = state.workspace?.ok ? state.workspace.repositories.flatMap((repo) => repo.tasks) : [];
  if (tasks.some((task) => task.state === "needs_reconciliation")) {
    const resume = el("button", "ws-action", "Resume interrupted…"); resume.type = "button";
    resume.addEventListener("click", () => scheduleDialog(true)); bar.append(resume);
  }
  const last = scheduler.last;
  if (last) bar.append(el("small", "", `Last Garden schedule: ${last.status}${last.result_status ? ` · ${last.result_status}` : ""}${last.error_code ? ` · ${last.error_code}` : ""} · ${stamp(last.started_at)}`));
}
function scheduleDialog(resume) {
  openDialog("schedule", { id: "workspace" });
  const content = $("action-content"); content.replaceChildren();
  const form = el("div", "action-form");
  const mutation = el("input"); mutation.type = "checkbox"; mutation.id = "opt-mutation";
  const resumeBox = el("input"); resumeBox.type = "checkbox"; resumeBox.id = "opt-resume"; resumeBox.checked = resume;
  const l1 = el("label"); l1.append(mutation, el("span", "", "Allow mutation: children may create worktrees and commit, as each repository's policy permits"));
  const l2 = el("label"); l2.append(resumeBox, el("span", "", "Resume interrupted children into their existing runs"));
  form.append(l1, l2); content.append(el("p", "action-note", "Choose the options, then preview what will launch."), form);
  const confirm = $("action-confirm"); confirm.textContent = "Preview"; confirm.hidden = false;
  confirm.onclick = async () => previewThenRun("schedule", { action: "schedule", allow_mutation: mutation.checked, resume_interrupted: resumeBox.checked });
}
function modelDialog(family, tier, models) {
  openDialog("set_model", { id: `${family} · ${tier}` });
  const content = $("action-content"); content.replaceChildren();
  const floor = models.floors?.[tier] ?? 0;
  const select = el("select"); select.setAttribute("aria-label", "Model");
  for (const choice of models.choices?.[family] || []) {
    const option = el("option", "", `${choice.model} · capacity ${choice.capacity}`); option.value = choice.model; option.dataset.capacity = choice.capacity;
    if (choice.model === models.models[family][tier]) option.selected = true; select.append(option);
  }
  const capacity = el("input"); capacity.type = "number"; capacity.min = String(floor); capacity.max = "10";
  capacity.value = String(models.ambiguity_capacities?.[family]?.[tier] ?? floor);
  select.addEventListener("change", () => { capacity.value = String(Math.max(floor, Number(select.selectedOptions[0].dataset.capacity))); });
  const form = el("div", "action-form"); const label = el("label"); label.append(el("span", "", `Ambiguity capacity (at least ${floor} for ${tier})`), capacity);
  form.append(select, label);
  const reset = el("button", "quiet-button", "Reset this tier to its default"); reset.type = "button";
  reset.addEventListener("click", () => previewThenRun("reset_model", { action: "reset_model", family, tier }));
  content.append(el("p", "action-note", "Only models already used in this family are offered. Others need pico models set."), form, reset);
  const confirm = $("action-confirm"); confirm.textContent = "Preview"; confirm.hidden = false;
  confirm.onclick = async () => previewThenRun("set_model", { action: "set_model", family, tier, model: select.value, capacity: Number(capacity.value) });
}
async function workspaceAction(action, extra, label) {
  openDialog(action, { id: label });
  await previewThenRun(action, { action, ...extra });
}
function taskActions(task) {
  if (!controlState.enabled) return null;
  const hasPr = (task.links || []).some((link) => link.kind === "pr");
  const buttons = [];
  if (task.kind === "qualification") {
    if (!["validated", "running"].includes(task.state) && !(task.blocked_by || []).length) buttons.push(["validate", "Run checks"]);
  } else {
    if (["completed", "partial", "validated"].includes(task.state) && !hasPr) buttons.push(["validate", task.state === "validated" ? "Revalidate" : "Validate"]);
    if (task.state === "validated" && !hasPr) buttons.push(["publish", "Request PR"]);
    if (hasPr) buttons.push(["ci", "Check CI"]);
    if (hasPr && !["ready_for_downstream", "merged"].includes(task.state)) buttons.push(["promote", "Promote"]);
  }
  if (task.state === "running" && ["launched", "launching"].includes(task.execution_status)) buttons.push(["cancel", "Cancel run"]);
  if (!buttons.length) return null;
  const row = el("div", "ws-actions");
  for (const [action, label] of buttons) {
    const button = el("button", `ws-action${action === "cancel" ? " danger" : ""}`, label); button.type = "button";
    button.addEventListener("click", () => startAction(action, task));
    row.append(button);
  }
  return row;
}
async function postAction(body) {
  const response = await fetch("/api/action", { method: "POST", cache: "no-store",
    headers: { "Content-Type": "application/json", "X-Pico-Token": controlState.token }, body: JSON.stringify(body) });
  return response.json().catch(() => ({ ok: false, error_code: `HTTP_${response.status}`, error: "No answer from the Garden." }));
}
const ACTION_TITLES = { validate: "Validate the task", publish: "Request a draft PR", ci: "Check CI", promote: "Promote for downstream use",
  cancel: "Cancel the child run", schedule: "Schedule the workspace", set_permission: "Change a permission", approve_mutation: "Approve local mutation", commit_policy: "Commit the policy", set_model: "Change a model", reset_model: "Reset a model" };
function factList(pairs) {
  const list = el("dl", "action-facts");
  for (const [key, value] of pairs) if (value != null && value !== "") list.append(el("dt", "", key), el("dd", "", String(value)));
  return list;
}
function openDialog(action, task) {
  state.paused = true;
  $("action-eyebrow").textContent = `${action.toUpperCase()} · ${task.id}`;
  $("action-title").textContent = ACTION_TITLES[action];
  $("action-content").replaceChildren(el("p", "action-note", "Preparing a preview…"));
  $("action-confirm").hidden = true; $("action-confirm").disabled = false;
  $("action-dialog").showModal();
}
function showResult(result) {
  const content = $("action-content"); content.replaceChildren();
  content.append(el("p", `action-result ${result.ok ? "ok" : "fail"}`, result.ok ? (result.stage === "started" ? "Started. Child runs appear on the board as they launch." : "Done.") : `Not done · ${result.error_code || "UNKNOWN"}`));
  if (result.error) content.append(el("p", "action-note", result.error));
  content.append(factList([["Status", result.status], ["Readiness", result.readiness], ["CI", result.ci?.status], ["Mutations applied", result.mutations_applied]]));
  if (result.url) {
    const link = el("a", "pr-link", `Open PR #${result.pr} ↗`); link.href = result.url; link.target = "_blank"; link.rel = "noopener noreferrer";
    const line = el("p", "action-note"); line.append(link); content.append(line);
  }
  if (result.checks?.length) content.append(el("pre", "action-pre", result.checks.map((c) => `${c.status.padEnd(9)} ${c.id} [${c.repository}] ${c.duration_ms ?? "—"} ms`).join("\n")));
  $("action-confirm").hidden = true;
  controlState.fetchedAt = 0;
}
function showPreview(action, result, onConfirm) {
  const content = $("action-content"); content.replaceChildren();
  const p = result.preview || {};
  if (action === "validate") {
    content.append(factList([["Repository", p.repository], ["Branch", p.head], ["Commit", p.commit]]));
    content.append(el("pre", "action-pre", (p.checks || []).map((c) => `${c.id} [${c.repository}]\n  ${c.argv.join(" ")}`).join("\n")));
  } else if (action === "publish") {
    content.append(factList([["Repository", p.repository], ["Base", p.base], ["Head", p.head], ["Commit", p.commit], ["Title", p.title],
      ["Draft", p.draft ? "yes" : "no"], ["Existing PR", p.existing_pr ? `#${p.existing_pr}` : "none"], ["GitHub mutations", p.mutations_planned]]));
    content.append(el("pre", "action-pre", p.body || ""));
  } else if (action === "promote") {
    content.append(factList([["Repository", p.repository], ["CI", p.ci?.status], ["Checks", p.ci?.checks], ["PR state", p.ci?.pr_state], ["Head", p.ci?.head]]));
  } else if (action === "cancel") {
    content.append(factList([["Repository", p.repository], ["Execution", p.execution_status], ["Run", p.run_id]]));
  } else if (action === "schedule") {
    content.append(factList([["Will launch", p.dispatch?.join(", ") || "nothing"], ["Will resume", p.resume?.join(", ") || "nothing"],
      ["Awaiting authorization (not launched)", p.awaiting_authorization?.join(", ") || "none"],
      ["Interrupted", p.interrupted?.join(", ") || "none"], ["Parallel cap", p.cap], ["Allow mutation", p.allow_mutation ? "yes" : "no"],
      ["Budget", p.budget ? `${short(p.budget.spent_tokens)} of ${p.budget.max_tokens ? short(p.budget.max_tokens) : "—"} tokens${p.budget.cost_complete === false ? ` · cost is a lower bound, excludes ${(p.budget.unpriced || []).join(", ")}` : ""}${p.budget.incomplete ? " · in-flight spend unknown: nothing new will launch" : ""}${p.budget.exhausted ? " · exhausted: nothing new will launch" : ""}` : "none set"]]));
    content.append(el("pre", "action-pre", Object.entries(p.mutating_permissions || {}).map(([repo, perms]) => `${repo}: ${perms.length ? perms.join(", ") : "no mutating permissions"}`).join("\n")));
  } else if (action === "set_permission") {
    content.append(factList([["Repository", p.repository], ["Permission", p.permission?.replaceAll("_", " ")], ["New value", p.value ? "allowed" : "denied"], ["File", p.creates_file ? "created from the template" : "edited in place"]]));
    content.append(el("pre", "action-pre", p.diff || ""));
  } else if (action === "approve_mutation") {
    content.append(factList([["Repository", p.repository], ["Grants", p.grants?.join(", ").replaceAll("_", " ")], ["For tasks", p.tasks?.join(", ")],
      ["File", p.creates_file ? "created, every other permission denied" : "edited in place"], ["Never granted", p.never_granted?.join(", ").replaceAll("_", " ")],
      ["Already granted by the file", p.already_granted_beyond_local?.length ? p.already_granted_beyond_local.join(", ").replaceAll("_", " ") : "nothing beyond local"]]));
    content.append(el("pre", "action-pre", p.diff || ""));
    if (p.next_steps?.length) { const list = el("ol", "ws-approval-steps"); for (const step of p.next_steps) list.append(el("li", "", step)); content.append(list); }
  } else if (action === "commit_policy") {
    content.append(factList([["Repository", p.repository], ["Branch", p.branch], ["Head", p.head], ["Message", p.message]]));
    content.append(el("pre", "action-pre", p.diff || ""));
  } else if (action === "set_model" || action === "reset_model") {
    content.append(factList([["Family", p.family], ["Tier", p.tier], ["Current", `${p.current?.model} · capacity ${p.current?.capacity}`], ["New", `${p.new?.model} · capacity ${p.new?.capacity}`]]));
  }
  if (p.note) content.append(el("p", "action-note", p.note));
  const confirm = $("action-confirm");
  const labels = { publish: "Push and open draft PR", promote: "Promote",
    cancel: "Cancel the run", schedule: "Launch", set_permission: "Write the policy", approve_mutation: "Approve and write the policy", commit_policy: "Commit", set_model: "Save the model", reset_model: "Reset the model" };
  confirm.textContent = labels[action] || "Run checks";
  confirm.hidden = !result.ok; confirm.disabled = false;
  const blocked = { promote: "CI is not green on the validated commit, so promotion is not possible yet.", schedule: "Nothing can launch or resume: no task is ready, or the budget is exhausted.",
    set_permission: "This would not change the policy.", set_model: "This is already the current model.", reset_model: "This tier already uses its default." };
  if (!result.ok) content.append(el("p", "action-result fail", blocked[action] || "This action cannot run now."));
  confirm.onclick = async () => { confirm.disabled = true; content.append(el("p", "action-note", "Working…")); await onConfirm(); };
}
async function startAction(action, task) {
  openDialog(action, task);
  const body = { action, task: task.id };
  if (action === "validate" && task.kind !== "qualification") {
    const response = await fetch(`/api/branches?task=${encodeURIComponent(task.id)}`, { cache: "no-store", headers: tokenHeaders() });
    const data = await response.json().catch(() => ({ ok: false }));
    const branches = (data.branches || []).filter((branch) => branch.descends_from_pin);
    const content = $("action-content"); content.replaceChildren();
    if (!branches.length) { content.append(el("p", "action-result fail", "No local pico/ branch descends from the pinned commit.")); return; }
    const select = el("select"); select.setAttribute("aria-label", "Branch to validate");
    for (const branch of branches) { const option = el("option", "", `${branch.name} · ${branch.commit.slice(0, 7)}`); option.value = branch.name; select.append(option); }
    content.append(el("p", "action-note", "Choose the branch holding the task's work."), select);
    const confirm = $("action-confirm"); confirm.textContent = "Preview checks"; confirm.hidden = false;
    confirm.onclick = async () => { body.head = select.value; await previewThenRun(action, body); };
    return;
  }
  await previewThenRun(action, body);
}
async function previewThenRun(action, body) {
  const first = await postAction(body);
  if (first.stage !== "preview") { showResult(first); return; }
  showPreview(action, first, async () => showResult(await postAction({ ...body, confirm: first.confirm })));
}
$("action-cancel").addEventListener("click", () => $("action-dialog").close());
$("action-dialog").addEventListener("close", () => { state.paused = false; refresh(); });
$("workspace-mode").addEventListener("click", () => { history.replaceState(null, "", location.pathname); applyMode(); });
$("runs-mode").addEventListener("click", () => { location.hash = "runs"; });
window.addEventListener("hashchange", () => { applyMode(); render(state.label); window.scrollTo({ top: 0 }); });

// Issues change slowly and the server caches gh for a minute, so they are polled every 30 seconds.
const issueState = { fetchedAt: 0 };
async function refreshIssues(force = false) {
  if (!force && Date.now() - issueState.fetchedAt < 30000) return;
  issueState.fetchedAt = Date.now();
  let data;
  try {
    const response = await fetch("/api/issues", { cache: "no-store", headers: tokenHeaders() });
    data = await response.json().catch(() => ({ ok: false, error_code: `HTTP_${response.status}`, error: "The issues endpoint did not answer." }));
  } catch { data = { ok: false, error_code: "CONNECTION_LOST", error: "The Garden server did not answer." }; }
  // Without a target repository (pico watch --all, or a workspace id without a manifest) the panel stays hidden.
  $("issues-panel").hidden = data.error_code === "ISSUES_TARGET_NONE";
  $("issues-state").textContent = data.ok ? `${data.mode === "workspace" ? "per repository" : "this repository"} · read-only` : "unavailable";
  globalThis.PicoIssues.render($("issues-list"), data);
}

async function refresh() {
  if (state.paused) return;
  try {
    const response = await fetch("/api/runs", { cache: "no-store", headers: tokenHeaders() });
    // With controls on, every data endpoint needs the session token from the launch URL.
    state.tokenRequired = response.status === 403;
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    state.runs = data.runs || []; state.healthy = true;
    state.workspaceMode = Boolean(data.workspace_mode); state.workspaceId = data.workspace || null;
    if (state.workspaceMode) { await refreshWorkspace(); await refreshControls(); }
    refreshIssues();  // not awaited: a slow gh call must not hold up the live run view
    applyMode();
    state.label = state.workspaceMode ? `Child runs of workspace ${state.workspaceId || ""}` : data.repository;
    render(state.label);
  } catch { state.healthy = false; setStatus(); }
}
$("run-select").addEventListener("change", (event) => { state.selected = event.target.value; state.manuallySelected = true; state.agent = "lead"; state.gardenCenteredFor = null; if (location.hash.startsWith("#run=")) history.replaceState(null, "", "#runs"); render(selectedRun()?.repository); });
$("garden-view").addEventListener("click", () => { state.view = "garden"; renderObservatory(selectedRun()); });
$("conversation-view").addEventListener("click", () => { state.view = "conversation"; renderObservatory(selectedRun()); });
$("timeline-view").addEventListener("click", () => { state.view = "timeline"; state.followTimeline = true; renderObservatory(selectedRun()); });
$("run-timeline").addEventListener("wheel", () => { state.followTimeline = false; }, { passive: true });
$("run-timeline").addEventListener("pointerdown", () => { state.followTimeline = false; });
$("pause").addEventListener("click", () => { state.paused = !state.paused; $("pause").textContent = state.paused ? "Resume display updates" : "Pause display updates"; setStatus(); if (!state.paused) refresh(); });
refresh(); setInterval(refresh, 2000);
setInterval(() => { const run = selectedRun(); if (run?.status === "IN_PROGRESS" && !state.paused) renderOverview(run); }, 1000);
