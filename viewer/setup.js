"use strict";
const $ = (id) => document.getElementById(id);
const token = /(?:^#|&)token=([A-Za-z0-9_-]{20,})/.exec(location.hash)?.[1] || null;
if (token) history.replaceState(null, "", location.pathname);
let root = null, preview = null;
const rows = [];
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
    if (data.ok) { $("message").textContent = `Saved ${data.path}. Open it with pico watch --workspace <manifest> --controls.`; preview = null; $("save").hidden = true; }
    else show(data);
  } catch { show({ ok: false, error_code: "SETUP_UNAVAILABLE", error: "The setup server did not answer." }); }
  finally { $("save").disabled = false; }
});
(async () => {
  try { const data = await call();
    if (!data.ok) throw Error(data.error_code);
    root = data.root; $("root").textContent = `Discovery root: ${root}`;
    for (const item of data.candidates) addRow(item);
    $("discovery-note").textContent = `${data.candidates.length} checkouts · ${data.skipped.length} skipped · ${data.unreadable.length} unreadable` +
      (data.truncated.length ? ` · search bounds reached: ${data.truncated.join(", ")}` : "");
  } catch { $("root").textContent = "Cannot read setup. Open the private URL printed by pico watch --setup-root."; }
})();
