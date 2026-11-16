// A read-only view of durable delivery observations. Never interprets issue text as HTML or authority.
(function (root) {
  "use strict";
  const node = (tag, className, text) => {
    const result = document.createElement(tag);
    result.className = className;
    if (text != null) result.textContent = String(text);
    return result;
  };
  const label = value => String(value || "unknown").replaceAll("_", " ");
  const attention = new Set(["needs_human_review", "needs_decision", "changes_requested", "blocked", "unavailable"]);
  const finished = new Set(["merged", "closed", "cancelled"]);
  function verifiedLink(row) {
    if (typeof row.repository !== "string" || !/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(row.repository)) return null;
    const prefix = `https://github.com/${row.repository}/pull/`;
    if (typeof row.pr_url !== "string" || !row.pr_url.startsWith(prefix) || !/^[1-9][0-9]*$/.test(row.pr_url.slice(prefix.length))) return null;
    const anchor = node("a", "pr-link", "Review pull request ↗");
    anchor.href = row.pr_url; anchor.target = "_blank"; anchor.rel = "noopener noreferrer";
    return anchor;
  }
  function render(container, data, options = {}) {
    container.replaceChildren();
    if (!data?.ok) container.append(node("p", "delivery-warning", "Delivery observations unavailable. Last known evidence may be outdated; refresh before deciding."));
    const all = Array.isArray(data?.deliveries) ? data.deliveries : [];
    const rows = all.filter(row => {
      if (options.filter === "history") return finished.has(row.delivery_state);
      if (options.filter === "running") return !attention.has(row.delivery_state) && !finished.has(row.delivery_state);
      return attention.has(row.delivery_state) || Boolean(row.needs_attention);
    });
    if (!rows.length) container.append(node("p", "empty", all.length ? "No deliveries in this view." : "No delivery observations yet."));
    for (const row of rows) {
      const card = node("article", "delivery-card");
      const heading = node("div", "delivery-heading");
      heading.append(node("strong", "", row.title || `${row.repository || "Repository"} · queue #${row.queue_issue || "?"}`), node("span", "soft-count", label(row.delivery_state)));
      card.append(heading, node("p", "delivery-next", row.next_action || "Inspect the latest delivery evidence."));
      if (row.reason) card.append(node("p", "detail-note", row.reason));
      const facts = node("div", "delivery-facts");
      facts.append(node("span", "", `Execution: ${label(row.execution_state)}`), node("span", "", `Checks: ${label(row.checks_state)}`), node("span", "", `Human review: ${label(row.review_state)}`));
      card.append(facts);
      const stamp = row.observed_at || row.updated_at;
      const date = typeof stamp === "string" ? new Date(stamp) : null;
      const validDate = date && Number.isFinite(date.getTime());
      const stale = !validDate || Date.now() - date.getTime() > 180000;
      card.append(node("p", stale ? "delivery-warning" : "detail-note", validDate ? `${stale ? "Observation may be stale · " : "Observed "}${date.toISOString().replace("T", " ")}` : "Observation time unknown"));
      const link = verifiedLink(row);
      if (link) card.append(link);
      const details = node("details", "delivery-evidence");
      details.append(node("summary", "", "Evidence"), node("p", "detail-note", `Commit: ${row.head_sha || "unverified"} · Run: ${row.run_id || "unavailable"}`));
      card.append(details); container.append(card);
    }
  }
  root.PicoDeliveries = { render };
})(globalThis);
