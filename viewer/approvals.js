// Dispatcher queue approvals (`pico watch --approvals`). Issue text is untrusted: it is shown
// as text only, never parsed as HTML. Every approval is previewed by the server and confirmed
// in the dialog; this file only lists the queue and starts that flow.
(function (root) {
  "use strict";
  const node = (tag, className, text) => {
    const result = document.createElement(tag);
    result.className = className;
    if (text != null) result.textContent = String(text);
    return result;
  };
  const STATES = {
    not_approved: "Not approved", approved: "Approved · waiting for dispatch", launching: "Launching",
    running: "Running", launch_uncertain: "Launch uncertain", completed: "Completed", partial: "Partial",
    blocked: "Blocked", interrupted: "Interrupted", cancelled: "Cancelled", stale: "Stale · issue changed after approval",
  };
  const MODES = { "read-only": "read-only", "local-mutation": "with code changes" };
  const stateLabel = value => STATES[value] || String(value || "unknown").replaceAll("_", " ");
  const expanded = new Set(); // issue numbers whose details are open; survives re-render
  function button(className, text, onClick) {
    const result = node("button", className, text);
    result.type = "button";
    result.addEventListener("click", onClick);
    return result;
  }
  function section(title, text) {
    const block = node("div", "approval-section");
    block.append(node("strong", "", title), node("p", "approval-text", text || "—"));
    return block;
  }
  function card(item, options) {
    const article = node("article", "approval-card");
    const heading = node("div", "approval-heading");
    heading.append(node("strong", "approval-title", `#${item.number} · ${item.title || "Untitled"}`),
      node("span", `soft-count approval-state ${item.state || ""}`, stateLabel(item.state)));
    article.append(heading);
    const facts = node("div", "approval-facts");
    facts.append(node("span", "", `Target: ${item.target || "—"}`), node("span", "", `Digest: ${item.digest_short || "—"}`));
    if (item.approved_mode) facts.append(node("span", "", `Recorded mode: ${MODES[item.approved_mode] || item.approved_mode}`));
    article.append(facts);
    if (item.error) article.append(node("p", "delivery-warning", `Not approvable: ${item.error}`));
    if (item.state === "approved" && !item.approved_current) {
      article.append(node("p", "delivery-warning", "The issue changed after it was approved: the dispatcher will mark it stale unless you approve the new version."));
    }
    if (item.objective || item.criteria) {
      const details = node("details", "approval-details");
      details.open = expanded.has(item.number);
      details.addEventListener("toggle", () => { if (details.open) expanded.add(item.number); else expanded.delete(item.number); });
      details.append(node("summary", "", "Objective and acceptance criteria"),
        section("Objective", item.objective), section("Acceptance criteria", item.criteria));
      article.append(details);
    }
    if (item.approvable && options.onApprove) {
      const actions = node("div", "approval-actions");
      actions.append(
        button("primary-button approval-button", "Approve read-only", () => options.onApprove(item, "read-only")),
        button("quiet-button approval-button mutation", "Approve with code changes", () => options.onApprove(item, "local-mutation")));
      article.append(actions);
    }
    return article;
  }
  function render(container, data, options = {}) {
    container.replaceChildren();
    if (data?.tokenMissing) {
      container.append(node("p", "delivery-warning", "Open the Garden from its private link (it ends in #token=…) to see and approve queue issues."));
      return;
    }
    if (data?.identityRejected) {
      container.append(node("p", "delivery-warning", data.error || "This Garden only accepts approvals from an allowed Tailscale login."));
      return;
    }
    if (!data?.ok) {
      container.append(node("p", "delivery-warning", data?.error || "The queue cannot be read right now; nothing is approved from a stale list."));
      return;
    }
    const issues = Array.isArray(data.issues) ? data.issues : [];
    if (!issues.length) { container.append(node("p", "empty", "No open issue carries pico:queued.")); return; }
    for (const item of issues) container.append(card(item, options));
  }
  root.PicoApprovals = { render, stateLabel, MODES };
})(globalThis);
