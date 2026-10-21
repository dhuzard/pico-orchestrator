// Read-only Issues panel for Pico Garden (GET /api/issues).
// Every value from GitHub (titles, labels, logins) is user-controlled, so it is
// written with textContent only; this file never assigns innerHTML.
// A later slice can add a per-row action (for example "add to queue") through
// options.rowAction(repoEntry, issue), which returns a node or null; a row is
// addressed by (repoEntry.id || repoEntry.repository, issue.number).
(function (root) {
  "use strict";
  const node = (tag, className, text) => {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (text != null) item.textContent = String(text);
    return item;
  };
  const safeUrl = (value) => typeof value === "string" && /^https:\/\/github\.com\/[A-Za-z0-9_.\/-]+$/.test(value) ? value : null;
  const link = (text, url, className) => {
    const href = safeUrl(url);
    if (!href) return node("span", className, text);
    const anchor = node("a", className, text);
    anchor.href = href; anchor.target = "_blank"; anchor.rel = "noopener noreferrer";
    return anchor;
  };
  const when = (value) => {
    if (typeof value !== "string") return "update time unknown";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "update time unknown" : `updated ${date.toISOString().slice(0, 16).replace("T", " ")} UTC`;
  };

  function referenceChips(issue) {
    const refs = issue.references || {};
    const box = node("div", "issue-refs");
    if (Array.isArray(refs.branches)) for (const name of refs.branches) box.append(node("span", "ws-chip", `branch ${name}`));
    else box.append(node("span", "ws-chip quiet", "branches unknown"));
    if (Array.isArray(refs.pull_requests)) {
      for (const pr of refs.pull_requests) box.append(link(`PR #${pr.number}${pr.draft ? " (draft)" : ""} ↗`, pr.url, "pr-link"));
    } else box.append(node("span", "ws-chip quiet", "PRs unknown"));
    return box;
  }

  function issueRow(entry, issue, options) {
    const row = node("li", "issue-row");
    row.dataset.repository = entry.id || entry.repository || "";
    row.dataset.issue = String(issue.number);
    const head = node("div", "issue-head");
    head.append(link(`#${issue.number}`, issue.url, "issue-number"), node("strong", "issue-title", issue.title));
    row.append(head);
    const meta = node("div", "issue-meta");
    for (const label of Array.isArray(issue.labels) ? issue.labels : []) meta.append(node("span", "issue-label", label));
    const people = Array.isArray(issue.assignees) && issue.assignees.length ? `assigned to ${issue.assignees.join(", ")}` : "unassigned";
    meta.append(node("small", "", `${people} · ${when(issue.updated_at)}`));
    row.append(meta, referenceChips(issue));
    const action = options && typeof options.rowAction === "function" ? options.rowAction(entry, issue) : null;
    if (action) row.append(action);
    return row;
  }

  function repositoryBlock(entry, data, options) {
    const block = node("section", "issue-repo");
    const head = node("div", "ws-column-head");
    const title = node("div", "");
    title.append(node("strong", "", entry.repository || entry.id || "repository"));
    if (entry.id) title.append(node("small", "", entry.id));
    head.append(title);
    block.append(head);
    if (!entry.available || !Array.isArray(entry.issues)) {
      // Unavailable is never shown as an empty list.
      block.append(node("p", "ws-chip failed issue-unavailable", `Issues unavailable: ${entry.error_code || "UNKNOWN"}`));
      if (entry.error) block.append(node("p", "detail-note", entry.error));
      return block;
    }
    const count = entry.truncated ? `first ${entry.issues.length} open issues (capped at ${data.cap})` : `${entry.issues.length} open ${entry.issues.length === 1 ? "issue" : "issues"}`;
    head.append(node("span", "soft-count", count));
    if (!entry.issues.length) block.append(node("p", "empty", "No open issues."));
    else {
      const list = node("ul", "issue-list");
      for (const issue of entry.issues) list.append(issueRow(entry, issue, options));
      block.append(list);
    }
    const notes = [];
    if (entry.pull_requests && !entry.pull_requests.available) notes.push(`PR references unavailable (${entry.pull_requests.error_code || "unknown"})`);
    else if (entry.pull_requests && entry.pull_requests.truncated) notes.push(`PR references scan only the first ${entry.pull_requests.scanned} open PRs`);
    if (entry.branches && !entry.branches.available) notes.push("local branches could not be read");
    if (entry.fetched_at) notes.push(`fetched ${when(entry.fetched_at).replace("updated ", "")}${entry.cached ? " (cached)" : ""}`);
    if (notes.length) block.append(node("p", "detail-note", notes.join(" · ")));
    return block;
  }

  // Renders /api/issues into container. `data` is the parsed body (or an error body).
  function render(container, data, options) {
    container.replaceChildren();
    if (!data || data.ok !== true || !Array.isArray(data.repositories)) {
      const code = data && data.error_code ? data.error_code : "UNKNOWN";
      container.append(node("p", "ws-chip failed issue-unavailable", `Issues unavailable: ${code}`));
      if (data && data.error) container.append(node("p", "detail-note", data.error));
      return;
    }
    if (!data.repositories.length) { container.append(node("p", "empty", "No repository to read issues from.")); return; }
    for (const entry of data.repositories) container.append(repositoryBlock(entry, data, options));
  }

  root.PicoIssues = { render };
})(globalThis);
