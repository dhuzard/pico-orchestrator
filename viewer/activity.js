// Recent dispatcher activity (`GET /api/activity`, with --approvals): approvals, finished runs,
// delivery PRs, audit results, merges and deploys, newest first. Text only: titles and summaries
// can carry model or issue text, so they are never parsed as HTML.
(function (root) {
  "use strict";
  const node = (tag, className, text) => {
    const result = document.createElement(tag);
    result.className = className;
    if (text != null) result.textContent = String(text);
    return result;
  };
  const KINDS = {
    approval: ["✅", "Approved"], run_finished: ["🏁", "Run finished"], delivery_opened: ["📬", "Delivery opened"],
    audit_passed: ["🔎", "Audit passed"], audit_failed: ["⛔", "Audit failed"], merged: ["🌱", "Merged"],
    deploy: ["🚀", "Deployed"], error: ["⚠️", "Error"],
  };
  function safeLink(url) {
    if (typeof url !== "string" || url.length > 500 || !url.startsWith("https://")) return null;
    try { if (new URL(url).protocol !== "https:") return null; } catch { return null; }
    const anchor = node("a", "pr-link", "Open ↗");
    anchor.href = url; anchor.target = "_blank"; anchor.rel = "noopener noreferrer";
    return anchor;
  }
  function when(stamp) {
    const date = typeof stamp === "string" ? new Date(stamp) : null;
    if (!date || !Number.isFinite(date.getTime())) return "time unknown";
    return date.toISOString().slice(0, 16).replace("T", " ") + " UTC";
  }
  function render(container, data) {
    container.replaceChildren();
    if (data?.tokenMissing) {
      container.append(node("p", "delivery-warning", "Open the Garden from its private link (it ends in #token=…) to see recent activity."));
      return;
    }
    if (!data?.ok) {
      container.append(node("p", "delivery-warning", data?.error || "Recent activity cannot be read right now."));
      return;
    }
    const events = Array.isArray(data.events) ? data.events : [];
    if (!events.length) { container.append(node("p", "empty", "Nothing has happened yet.")); return; }
    for (const item of events) {
      const [icon, caption] = KINDS[item.kind] || ["•", String(item.kind || "event")];
      const article = node("article", `feed-item ${KINDS[item.kind] ? item.kind : ""}`);
      const mark = node("span", "feed-icon", icon);
      mark.setAttribute("aria-hidden", "true");
      const body = node("div", "feed-body");
      body.append(node("small", "feed-meta", `${caption} · ${when(item.at)}`), node("strong", "feed-title", item.title || caption));
      if (item.summary) body.append(node("p", "feed-summary", item.summary));
      const link = safeLink(item.url);
      if (link) body.append(link);
      article.append(mark, body);
      container.append(article);
    }
  }
  root.PicoActivity = { render, KINDS };
})(globalThis);
