import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

class Element {
  tagName: string;
  className = "";
  children: Element[] = [];
  href = "";
  target = "";
  rel = "";
  text = "";
  constructor(tag: string) { this.tagName = tag.toUpperCase(); }
  set textContent(value: unknown) { this.text = String(value); this.children = []; }
  get textContent(): string { return this.text + this.children.map(item => item.textContent).join(""); }
  set innerHTML(_value: unknown) { throw new Error("Untrusted delivery text must never be parsed"); }
  append(...children: Element[]) { this.children.push(...children); }
  replaceChildren(...children: Element[]) { this.children = children; }
  all(): Element[] { return [this, ...this.children.flatMap(item => item.all())]; }
}
function renderer() {
  const context: Record<string, unknown> = { document: { createElement: (tag: string) => new Element(tag) } };
  context.globalThis = context;
  vm.runInNewContext(readFileSync(new URL("../viewer/deliveries.js", import.meta.url), "utf8"), context);
  return (context.PicoDeliveries as { render: (element: Element, data: unknown, options?: unknown) => void }).render;
}
const row = {
  repository: "acme/app", queue_issue: 10, execution_state: "completed", delivery_state: "needs_human_review",
  checks_state: "passed", review_state: "not_requested", head_sha: "a".repeat(40), run_id: "run-abc",
  pr_url: "https://github.com/acme/app/pull/12", observed_at: new Date().toISOString(),
  next_action: "Review the exact change in GitHub.",
};
test("handoff renders untrusted text safely and only links the observed repository", () => {
  const render = renderer(); const container = new Element("div");
  const hostile = '<img src=x onerror="alert(1)">';
  render(container, { ok: true, deliveries: [{ ...row, title: hostile, next_action: hostile }] });
  assert.ok(container.textContent.includes(hostile));
  assert.equal(container.all().some(item => item.tagName === "IMG"), false);
  assert.deepEqual(container.all().filter(item => item.tagName === "A").map(item => item.href), [row.pr_url]);
  for (const url of ["javascript:alert(1)", "https://github.com/other/app/pull/12", row.pr_url + "/files", "https://github.com/acme/app/pull/0"]) {
    render(container, { ok: true, deliveries: [{ ...row, pr_url: url }] });
    assert.equal(container.all().filter(item => item.tagName === "A").length, 0);
  }
});
test("filters distinguish attention from asynchronous checks and completed delivery", () => {
  const render = renderer(); const container = new Element("div");
  const data = { ok: true, deliveries: [{ ...row, title: "Review me" }, { ...row, title: "Still running", delivery_state: "waiting_ci" }, { ...row, title: "Already merged", delivery_state: "merged" }] };
  render(container, data);
  assert.match(container.textContent, /Review me/); assert.doesNotMatch(container.textContent, /Still running|Already merged/);
  render(container, data, { filter: "running" });
  assert.match(container.textContent, /Still running/); assert.doesNotMatch(container.textContent, /Review me|Already merged/);
  render(container, data, { filter: "history" }); assert.match(container.textContent, /Already merged/);
});
test("connection loss and old observations remain visible instead of implying fresh readiness", () => {
  const render = renderer(); const container = new Element("div");
  render(container, { ok: false, deliveries: [{ ...row, observed_at: "2020-01-01T00:00:00Z" }] });
  assert.match(container.textContent, /unavailable/); assert.match(container.textContent, /may be stale/);
  assert.match(container.textContent, /Execution: completed/);
  assert.equal(container.all().some(item => item.tagName === "BUTTON"), false);
});
test("a failed automatic audit needs attention; auditing waits; an automatic merge is history", () => {
  const render = renderer(); const container = new Element("div");
  const data = { ok: true, deliveries: [
    { ...row, title: "Audit refused", delivery_state: "audit_failed", reason: "Automatic audit failed: an acceptance criterion is not met" },
    { ...row, title: "Being audited", delivery_state: "auditing" },
    { ...row, title: "Auto merged", delivery_state: "merged", reason: "Merged automatically after a passing audit" }] };
  render(container, data);
  assert.match(container.textContent, /Audit refused/); assert.match(container.textContent, /audit failed/);
  assert.match(container.textContent, /acceptance criterion is not met/);
  assert.doesNotMatch(container.textContent, /Being audited|Auto merged/);
  render(container, data, { filter: "running" });
  assert.match(container.textContent, /Being audited/); assert.doesNotMatch(container.textContent, /Audit refused|Auto merged/);
  render(container, data, { filter: "history" });
  assert.match(container.textContent, /Merged automatically after a passing audit/);
});
