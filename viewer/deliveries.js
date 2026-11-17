// A view of durable delivery observations. Never interprets issue text as HTML or authority.
// With `pico watch --feedback`, a card can also send a PR comment or a change request as the
// person's own gh account: previewed first, confirmed second, and never an approval or merge.
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
  const KINDS = { comment: "Comment on the pull request", request_changes: "Request changes (review)" };
  const drafts = new Map(); // per delivery, survives the periodic re-render
  const draftFor = row => {
    const key = `${row.repository}#${row.queue_issue}`;
    if (!drafts.has(key)) drafts.set(key, { open: false, kind: "comment", text: "", preview: null, confirm: null, busy: false, error: null, sent: null });
    return drafts.get(key);
  };
  function postedLink(row, url) {
    const prefix = `https://github.com/${row.repository}/pull/${row.pr_number}`;
    if (typeof url !== "string" || !(url === prefix || url.startsWith(`${prefix}#`))) return null;
    const anchor = node("a", "pr-link", "Open on GitHub ↗");
    anchor.href = url; anchor.target = "_blank"; anchor.rel = "noopener noreferrer";
    return anchor;
  }
  function button(className, text, onClick, disabled) {
    const result = node("button", className, text);
    result.type = "button"; result.disabled = Boolean(disabled);
    result.addEventListener("click", onClick);
    return result;
  }
  function composer(row, feedback) {
    const draft = draftFor(row);
    const box = node("details", "delivery-feedback");
    box.open = draft.open;
    box.addEventListener("toggle", () => { draft.open = box.open; });
    box.append(node("summary", "", "Send feedback"));
    const call = async (payload, onResult) => {
      draft.busy = true; draft.error = null; feedback.rerender();
      try {
        const result = await feedback.send(payload);
        onResult(result || {});
      } catch {
        draft.error = "The Garden did not answer; nothing was sent.";
      }
      draft.busy = false; feedback.rerender();
    };
    if (draft.sent) {
      const done = node("p", "detail-note", draft.sent.status === "CHANGES_REQUESTED" ? "Change request sent. " : "Comment sent. ");
      const link = postedLink(row, draft.sent.url);
      if (link) done.append(link);
      box.append(done);
    }
    const text = node("textarea", "feedback-text");
    text.value = draft.text; text.maxLength = 4000; text.rows = 4; text.disabled = draft.busy || Boolean(draft.preview);
    text.setAttribute("aria-label", "Your feedback");
    text.placeholder = "What should change, or what looks good?";
    text.addEventListener("input", () => { draft.text = text.value; draft.sent = null; });
    const kind = node("select", "feedback-kind");
    for (const [value, caption] of Object.entries(KINDS)) {
      const option = node("option", "", caption); option.value = value; kind.append(option);
    }
    kind.value = draft.kind; kind.disabled = text.disabled;
    kind.setAttribute("aria-label", "How to send it");
    kind.addEventListener("change", () => { draft.kind = kind.value; });
    box.append(text, kind);
    if (!draft.preview) {
      box.append(button("primary-button", draft.busy ? "Preparing…" : "Preview", () => {
        if (!draft.text.trim()) { draft.error = "Write some feedback first."; feedback.rerender(); return; }
        call({ issue: row.queue_issue, kind: draft.kind, body: draft.text }, result => {
          if (result.ok && result.stage === "preview" && result.preview) { draft.preview = result.preview; draft.confirm = result.confirm; }
          else draft.error = result.error || "The preview was refused.";
        });
      }, draft.busy));
    } else {
      const preview = draft.preview;
      const summary = node("div", "feedback-preview");
      summary.append(
        node("p", "", `Posts as @${preview.author} on ${preview.repository} #${preview.pr_number} at commit ${String(preview.head_sha).slice(0, 12)}`),
        node("p", "", `As: ${KINDS[preview.kind] || preview.kind}`),
        node("blockquote", "feedback-quote", preview.body),
        node("p", "detail-note", preview.note || ""));
      box.append(summary,
        button("primary-button", draft.busy ? "Sending…" : "Send to GitHub", () => {
          call({ issue: row.queue_issue, kind: draft.kind, body: draft.text, confirm: draft.confirm }, result => {
            if (result.ok && result.stage === "done") {
              draft.sent = { status: result.status, url: result.url };
              draft.text = ""; draft.preview = null; draft.confirm = null;
            } else {
              draft.error = result.error || "GitHub did not accept the feedback.";
              draft.preview = null; draft.confirm = null;
            }
          });
        }, draft.busy),
        button("quiet-button", "Edit", () => { draft.preview = null; draft.confirm = null; feedback.rerender(); }, draft.busy));
    }
    if (draft.error) box.append(node("p", "delivery-warning", draft.error));
    return box;
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
      card.append(details);
      if (options.feedback && link && row.publication === "observed" && !finished.has(row.delivery_state)) card.append(composer(row, options.feedback));
      container.append(card);
    }
  }
  root.PicoDeliveries = { render };
})(globalThis);
