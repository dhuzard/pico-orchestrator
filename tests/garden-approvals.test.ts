import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

type Listener = () => void;
class Element {
  tagName: string;
  className = "";
  children: Element[] = [];
  listeners: Record<string, Listener[]> = {};
  text = ""; type = ""; open = false;
  constructor(tag: string) { this.tagName = tag.toUpperCase(); }
  set textContent(value: unknown) { this.text = String(value); this.children = []; }
  get textContent(): string { return this.text + this.children.map(item => item.textContent).join(""); }
  set innerHTML(_value: unknown) { throw new Error("Queue issue text must never be parsed as HTML"); }
  append(...children: Element[]) { this.children.push(...children); }
  replaceChildren(...children: Element[]) { this.children = children; }
  addEventListener(name: string, listener: Listener) { (this.listeners[name] ||= []).push(listener); }
  fire(name: string) { for (const listener of this.listeners[name] || []) listener(); }
  all(): Element[] { return [this, ...this.children.flatMap(item => item.all())]; }
}
type Render = (element: Element, data: unknown, options?: unknown) => void;
function renderer(): Render {
  const context: Record<string, unknown> = { document: { createElement: (tag: string) => new Element(tag) } };
  context.globalThis = context;
  vm.runInNewContext(readFileSync(new URL("../viewer/approvals.js", import.meta.url), "utf8"), context);
  return (context.PicoApprovals as { render: Render }).render;
}
const item = {
  number: 7, title: "<img src=x onerror=alert(1)>", target: "acme/tool", objective: "Add tests.\nKeep scope.",
  criteria: "Tests pass.", digest_short: "0123456789ab", error: null, state: "not_approved", approved_mode: null,
  approved_current: false, approvable: true,
};
const buttons = (root: Element) => root.all().filter(node => node.tagName === "BUTTON");

test("an approvable issue offers both explicit modes and passes the chosen one", () => {
  const render = renderer(); const container = new Element("div"); const chosen: string[] = [];
  render(container, { ok: true, queue: "acme/queue", actor: "dhuzard@github", issues: [item] },
    { onApprove: (_: unknown, mode: string) => chosen.push(mode) });
  const [readOnly, mutation] = buttons(container);
  assert.equal(readOnly.textContent, "Approve read-only");
  assert.equal(mutation.textContent, "Approve with code changes");
  readOnly.fire("click"); mutation.fire("click");
  assert.deepEqual(chosen, ["read-only", "local-mutation"]);
  assert.ok(container.textContent.includes("<img src=x onerror=alert(1)>"), "titles are plain text");
  assert.ok(container.textContent.includes("Digest: 0123456789ab"));
  assert.ok(container.all().some(node => node.tagName === "DETAILS" && node.textContent.includes("Keep scope.")));
});

test("dispatched, invalid or read-only listings offer no approval", () => {
  const render = renderer(); const container = new Element("div");
  const onApprove = () => assert.fail("not approvable");
  render(container, { ok: true, issues: [{ ...item, approvable: false, state: "running" },
    { ...item, number: 8, approvable: false, error: "target repository is not on the operator allowlist" }] }, { onApprove });
  assert.equal(buttons(container).length, 0);
  assert.ok(container.textContent.includes("Running"));
  assert.ok(container.textContent.includes("Not approvable: target repository is not on the operator allowlist"));
  render(container, { ok: true, issues: [item] });
  assert.equal(buttons(container).length, 0, "no handler, no buttons");
});

test("missing token, rejected identity and an unreadable queue are explained", () => {
  const render = renderer(); const container = new Element("div");
  render(container, { ok: false, tokenMissing: true });
  assert.ok(container.textContent.includes("#token="));
  render(container, { ok: false, identityRejected: true, error: "approvals need a Tailscale login" });
  assert.ok(container.textContent.includes("Tailscale login"));
  render(container, { ok: false, error: "the queue cannot be read" });
  assert.equal(buttons(container).length, 0);
});

test("an approval whose issue changed afterwards is flagged", () => {
  const render = renderer(); const container = new Element("div");
  render(container, { ok: true, issues: [{ ...item, state: "approved", approved_mode: "read-only", approved_current: false }] }, { onApprove: () => undefined });
  assert.ok(container.textContent.includes("changed after it was approved"));
});
