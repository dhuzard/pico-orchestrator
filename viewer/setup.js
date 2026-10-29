"use strict";
const $ = (id) => document.getElementById(id);
const token = /(?:^#|&)token=([A-Za-z0-9_-]{20,})/.exec(location.hash)?.[1] || null;
if (token) history.replaceState(null, "", location.pathname);
let root = null, preview = null, manifestNames = [];
const rows = [];
function text(tag, value, className = "") {
  const node = document.createElement(tag); node.className = className; node.textContent = String(value); return node;
}
function listManifests(names) {
  manifestNames = [...new Set(names)].sort();
  const list = $("existing-list"); list.replaceChildren();
  if (!manifestNames.length) { list.append(text("p", "No manifest files found in this root yet.")); return; }
  for (const name of manifestNames) {
    const button = text("button", name, "quiet-button"); button.type = "button";
    button.addEventListener("click", () => { $("existing-name").value = name; inspectExisting(); });
    list.append(button);
  }
}
async function inspectExisting() {
  const name = $("existing-name").value.trim();
  const detail = $("existing-detail"); detail.hidden = false;
  $("existing-status").textContent = "Inspecting manifest and current repository state…";
  $("existing-repos").replaceChildren(); $("existing-tasks").replaceChildren(); $("existing-blockers").replaceChildren();
  try {
    const response = await fetch(`/api/setup?manifest=${encodeURIComponent(name)}`,
      { cache: "no-store", headers: { "X-Pico-Token": token || "" } });
    const data = await response.json();
    if (!data.ok) { $("existing-status").textContent = `${data.error_code}: ${data.error}`; return; }
    $("existing-status").textContent = `${data.workspace.workspace_id} · ${data.workspace.status} · read-only import from ${data.path}`;
    for (const repo of data.repositories) {
      const box = document.createElement("div"); box.className = "setup-import-repo";
      box.append(text("strong", `${repo.id} · ${repo.repository}`),
        text("p", `Checkout ${repo.resolved_path || repo.path} · branch ${repo.branch || "unavailable"} · HEAD ${repo.head || "unavailable"}`),
        text("p", `Selected pin ${repo.pinned_commit} · ${repo.dirty === null ? "dirty state unavailable" : repo.dirty ? "dirty" : "clean"} · GitHub access ${repo.access?.status || "unverified"}`));
      if (repo.blocker_code) box.append(text("p", `Blocker: ${repo.blocker_code}`));
      $("existing-repos").append(box);
    }
    for (const repo of data.workspace.repositories) for (const task of repo.tasks)
      $("existing-tasks").append(text("p", `${task.id} · ${repo.id} · ${task.state} · readiness ${task.readiness || "unavailable"}`));
    if (!data.blockers.length) $("existing-blockers").append(text("p", "No plan blockers."));
    for (const blocker of data.blockers)
      $("existing-blockers").append(text("p", `${blocker.scope} ${blocker.id} · ${blocker.code}`));
  } catch { $("existing-status").textContent = "The setup server did not answer."; }
}
function input(label, value = "", max = 200) {
  const wrap = document.createElement("label"), control = document.createElement("input");
  control.value = value; control.maxLength = max; wrap.append(label, control); return { wrap, control };
}
function addRow(candidate = null) {
  const row = document.createElement("fieldset"); row.className = "setup-repo";
  const selected = document.createElement("input"); selected.type = "checkbox";
  const heading = document.createElement("label"); heading.append(selected, document.createTextNode(candidate?.label || "Remote repository"));
  row.append(heading);
  const id = input("Repository ID", candidate?.path.split("/").at(-1).replace(/[^A-Za-z0-9_-]/g, "-") || "", 64);
  const identity = input("GitHub owner/repository", candidate?.identities.length === 1 ? candidate.identities[0] : "", 140);
  if (candidate?.identities.length > 1) {
    const select = document.createElement("select"); select.append(new Option("Choose a GitHub identity", ""));
    for (const name of candidate.identities) select.append(new Option(name, name));
    select.addEventListener("change", () => { identity.control.value = select.value; }); row.append(select);
  }
  const path = input(candidate ? "Checkout path" : "Expected checkout path", candidate?.path || "", 1000);
  if (candidate) path.control.readOnly = true;
  const title = input("Initial task title (optional)", "", 200);
  const objective = input("Task objective (optional)", "", 4000);
  const fields = document.createElement("div"); fields.className = "setup-fields";
  fields.append(id.wrap, identity.wrap, path.wrap, title.wrap, objective.wrap); row.append(fields);
  if (candidate) {
    const evidence = document.createElement("small");
    evidence.textContent = `Branch ${candidate.branch || "detached"} · HEAD ${candidate.head || "unavailable"} · ${candidate.dirty === null ? "dirty state unavailable" : candidate.dirty ? "dirty" : "clean"}`;
    row.append(evidence);
  } else selected.checked = true;
  $("candidates").append(row); rows.push({ candidate, selected, id: id.control, identity: identity.control,
    path: path.control, title: title.control, objective: objective.control });
  for (const control of [selected, id.control, identity.control, path.control, title.control, objective.control])
    control.addEventListener("input", () => { preview = null; $("result").hidden = true; });
}
async function call(action, spec = null, confirm = null) {
  const response = await fetch("/api/setup", { method: action ? "POST" : "GET", cache: "no-store",
    headers: { "X-Pico-Token": token || "", ...(action ? { "Content-Type": "application/json" } : {}) },
    ...(action ? { body: JSON.stringify({ action, spec, confirm }) } : {}) });
  return response.json();
}
function specification() {
  const repositories = rows.filter((row) => row.selected.checked).map((row) => {
    const entry = { id: row.id.value.trim(), repository: row.identity.value.trim(),
      [row.candidate ? "path" : "expected_path"]: row.path.value.trim() };
    if (row.title.value.trim() || row.objective.value.trim()) entry.task = {
      ...(row.title.value.trim() ? { title: row.title.value.trim() } : {}),
      ...(row.objective.value.trim() ? { objective: row.objective.value.trim() } : {}) };
    return entry;
  });
  return { version: 1, root, id: $("workspace-id").value.trim(), goal: $("goal").value.trim(),
    max_parallel: Number($("parallel").value), repositories };
}
function show(data) {
  $("result").hidden = false; $("message").textContent = data.ok ?
    `${data.plan_status} · destination ${data.destination}` : `${data.error_code}: ${data.error}`;
  $("evidence").replaceChildren(); $("manifest").textContent = data.text || "";
  $("save").hidden = !data.ok;
  if (!data.ok) return;
  for (const item of data.repositories) {
    const line = document.createElement("p");
    line.textContent = `${item.id}: ${item.repository} · ${item.local ? "local" : "not cloned"} · pin ${item.pinned_commit} · ${item.base_branch}`;
    $("evidence").append(line);
  }
  for (const item of data.blockers || []) {
    const line = document.createElement("p"); line.textContent = `Blocker: ${item.code || JSON.stringify(item)}`;
    $("evidence").append(line);
  }
  for (const warning of data.warnings || []) { const line = document.createElement("p"); line.textContent = warning; $("evidence").append(line); }
}
$("add-remote").addEventListener("click", () => { addRow(); preview = null; $("result").hidden = true; });
$("open-existing").addEventListener("click", inspectExisting);
for (const id of ["workspace-id", "goal", "parallel"]) $(id).addEventListener("input", () => {
  preview = null; $("result").hidden = true;
});
$("preview").addEventListener("click", async () => {
  preview = null; $("result").hidden = false; $("message").textContent = "Verifying repositories…";
  try { const spec = specification(), data = await call("preview", spec); show(data); if (data.ok) preview = { spec, confirm: data.confirm }; }
  catch { show({ ok: false, error_code: "SETUP_UNAVAILABLE", error: "The setup server did not answer." }); }
});
$("save").addEventListener("click", async () => {
  if (!preview) return;
  $("save").disabled = true;
  try { const data = await call("save", preview.spec, preview.confirm);
    if (data.ok) { $("message").textContent = `Saved ${data.path}. It is available under Open an existing workspace.`;
      preview = null; $("save").hidden = true; listManifests([...manifestNames, data.path.split("/").at(-1)]); }
    else show(data);
  } catch { show({ ok: false, error_code: "SETUP_UNAVAILABLE", error: "The setup server did not answer." }); }
  finally { $("save").disabled = false; }
});
(async () => {
  try { const data = await call();
    if (!data.ok) throw Error(data.error_code);
    root = data.root; $("root").textContent = `Discovery root: ${root}`;
    listManifests(data.manifests || []);
    for (const item of data.candidates) addRow(item);
    $("discovery-note").textContent = `${data.candidates.length} checkouts · ${data.skipped.length} skipped · ${data.unreadable.length} unreadable` +
      (data.truncated.length ? ` · search bounds reached: ${data.truncated.join(", ")}` : "");
  } catch { $("root").textContent = "Cannot read setup. Open the private URL printed by pico watch --setup-root."; }
})();
