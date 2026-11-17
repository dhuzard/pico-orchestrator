import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

type Listener = () => void;
class Element {
  tagName: string;
  className = "";
  children: Element[] = [];
  attributes: Record<string, string> = {};
  listeners: Record<string, Listener[]> = {};
  href = ""; target = ""; rel = ""; text = ""; value = ""; placeholder = "";
  type = ""; disabled = false; open = false; maxLength = 0; rows = 0;
  constructor(tag: string) { this.tagName = tag.toUpperCase(); }
  set textContent(value: unknown) { this.text = String(value); this.children = []; }
  get textContent(): string { return this.text + this.children.map(item => item.textContent).join(""); }
  set innerHTML(_value: unknown) { throw new Error("Feedback text must never be parsed as HTML"); }
  append(...children: Element[]) { this.children.push(...children); }
  replaceChildren(...children: Element[]) { this.children = children; }
  setAttribute(name: string, value: string) { this.attributes[name] = value; }
  addEventListener(name: string, listener: Listener) { (this.listeners[name] ||= []).push(listener); }
  fire(name: string) { for (const listener of this.listeners[name] || []) listener(); }
  all(): Element[] { return [this, ...this.children.flatMap(item => item.all())]; }
}
type Render = (element: Element, data: unknown, options?: unknown) => void;
function renderer(): Render {
  const context: Record<string, unknown> = { document: { createElement: (tag: string) => new Element(tag) } };
  context.globalThis = context;
  vm.runInNewContext(readFileSync(new URL("../viewer/deliveries.js", import.meta.url), "utf8"), context);
  return (context.PicoDeliveries as { render: Render }).render;
}
const row = {
  repository: "acme/app", queue_issue: 10, execution_state: "completed", delivery_state: "needs_human_review",
  checks_state: "passed", review_state: "not_requested", head_sha: "a".repeat(40), run_id: "run-abc", publication: "observed",
  pr_number: 12, pr_url: "https://github.com/acme/app/pull/12", observed_at: new Date().toISOString(), title: "Review me",
};
const find = (root: Element, tag: string, text?: string) =>
  root.all().find(item => item.tagName === tag && (text == null || item.textContent.includes(text)));
const plain = (value: unknown) => JSON.parse(JSON.stringify(value));
const settle = () => new Promise(resolve => setImmediate(resolve));

function harness(answers: Array<Record<string, unknown>>) {
  const render = renderer(); const container = new Element("div"); const sent: Array<Record<string, unknown>> = [];
  const data = { ok: true, feedback: true, deliveries: [row] };
  const feedback = {
    send: async (payload: Record<string, unknown>) => { sent.push(payload); return answers.shift(); },
    rerender: () => render(container, data, { feedback }),
  };
  feedback.rerender();
  return { container, sent, feedback, render, data };
}

test("no composer without --feedback, for finished deliveries, or without a verified PR", () => {
  const render = renderer(); const container = new Element("div");
  render(container, { ok: true, deliveries: [row] });
  assert.equal(find(container, "TEXTAREA"), undefined);
  const feedback = { send: async () => ({}), rerender: () => undefined };
  for (const other of [{ ...row, delivery_state: "merged" }, { ...row, publication: "none" }, { ...row, pr_url: "https://github.com/other/app/pull/12" }]) {
    render(container, { ok: true, deliveries: [other] }, { feedback, filter: "history" });
    render(container, { ok: true, deliveries: [other] }, { feedback });
    assert.equal(find(container, "TEXTAREA"), undefined, JSON.stringify(other));
  }
});

test("preview then confirm sends the exact text and kind, with the server's confirmation", async () => {
  const preview = { author: "reviewer", repository: "acme/app", pr_number: 12, head_sha: "a".repeat(40), kind: "request_changes",
                    body: '<img src=x onerror="alert(1)">', note: "Posts to GitHub as you." };
  const { container, sent } = harness([
    { ok: true, stage: "preview", confirm: "c0ffee", preview },
    { ok: true, stage: "done", status: "CHANGES_REQUESTED", url: "https://github.com/acme/app/pull/12#pullrequestreview-2" },
  ]);
  const text = find(container, "TEXTAREA")!;
  text.value = preview.body; text.fire("input");
  const kind = find(container, "SELECT")!;
  kind.value = "request_changes"; kind.fire("change");
  find(container, "BUTTON", "Preview")!.fire("click");
  await settle();
  assert.deepEqual(plain(sent[0]), { issue: 10, kind: "request_changes", body: preview.body });
  assert.match(container.textContent, /Posts as @reviewer on acme\/app #12 at commit aaaaaaaaaaaa/);
  assert.ok(container.textContent.includes(preview.body), "untrusted text is shown as text");
  assert.equal(container.all().some(item => item.tagName === "IMG"), false);
  assert.equal(find(container, "TEXTAREA")!.disabled, true, "the previewed text is frozen until Edit");
  find(container, "BUTTON", "Send to GitHub")!.fire("click");
  await settle();
  assert.deepEqual(plain(sent[1]), { issue: 10, kind: "request_changes", body: preview.body, confirm: "c0ffee" });
  assert.match(container.textContent, /Change request sent/);
  const links = container.all().filter(item => item.tagName === "A").map(item => item.href);
  assert.ok(links.includes("https://github.com/acme/app/pull/12#pullrequestreview-2"));
  assert.equal(find(container, "TEXTAREA")!.value, "", "the draft is cleared after sending");
});

test("drafts survive re-renders and refusals are shown without sending", async () => {
  const { container, sent, feedback } = harness([{ ok: false, stage: "refused", error_code: "DELIVERY_HEAD_CHANGED", error: "the pull request moved past the observed commit" }]);
  const text = find(container, "TEXTAREA")!;
  text.value = "Keep this"; text.fire("input");
  feedback.rerender();
  assert.equal(find(container, "TEXTAREA")!.value, "Keep this");
  find(container, "BUTTON", "Preview")!.fire("click");
  await settle();
  assert.match(container.textContent, /moved past the observed commit/);
  assert.equal(find(container, "BUTTON", "Send to GitHub"), undefined);
  assert.equal(sent.length, 1);
  assert.equal(find(container, "TEXTAREA")!.value, "Keep this");
});

test("an empty draft is not previewed and an unverified posted URL is not linked", async () => {
  const { container, sent } = harness([
    { ok: true, stage: "preview", confirm: "c", preview: { author: "r", repository: "acme/app", pr_number: 12, head_sha: "a".repeat(40), kind: "comment", body: "ok" } },
    { ok: true, stage: "done", status: "COMMENT_POSTED", url: "https://evil.example/acme/app/pull/12" },
  ]);
  find(container, "BUTTON", "Preview")!.fire("click");
  await settle();
  assert.equal(sent.length, 0);
  assert.match(container.textContent, /Write some feedback first/);
  const text = find(container, "TEXTAREA")!; text.value = "ok"; text.fire("input");
  find(container, "BUTTON", "Preview")!.fire("click"); await settle();
  find(container, "BUTTON", "Send to GitHub")!.fire("click"); await settle();
  assert.match(container.textContent, /Comment sent/);
  assert.equal(container.all().some(item => item.tagName === "A" && item.href.startsWith("https://evil.example")), false);
});

test("a page opened without the session token says so instead of hiding the feature", () => {
  const render = renderer(); const container = new Element("div");
  render(container, { ok: false, tokenMissing: true, deliveries: [] }, { feedback: null });
  assert.match(container.textContent, /opened without it/);
  assert.match(container.textContent, /#token=/);
  assert.doesNotMatch(container.textContent, /Delivery observations unavailable/);
  assert.equal(find(container, "TEXTAREA"), undefined);
});
