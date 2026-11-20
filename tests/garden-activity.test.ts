import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

class Element {
  tagName: string;
  className = "";
  children: Element[] = [];
  attributes: Record<string, string> = {};
  href = ""; target = ""; rel = ""; text = "";
  constructor(tag: string) { this.tagName = tag.toUpperCase(); }
  set textContent(value: unknown) { this.text = String(value); this.children = []; }
  get textContent(): string { return this.text + this.children.map(item => item.textContent).join(""); }
  set innerHTML(_value: unknown) { throw new Error("Activity text must never be parsed as HTML"); }
  setAttribute(name: string, value: string) { this.attributes[name] = value; }
  append(...children: Element[]) { this.children.push(...children); }
  replaceChildren(...children: Element[]) { this.children = children; }
  all(): Element[] { return [this, ...this.children.flatMap(item => item.all())]; }
}
type Render = (element: Element, data: unknown) => void;
function renderer(): Render {
  const context: Record<string, unknown> = { document: { createElement: (tag: string) => new Element(tag) }, URL };
  context.globalThis = context;
  vm.runInNewContext(readFileSync(new URL("../viewer/activity.js", import.meta.url), "utf8"), context);
  return (context.PicoActivity as { render: Render }).render;
}
const events = [
  { at: "2026-11-20T12:00:00Z", kind: "merged", title: "Merged acme/tool#42", summary: "Merged automatically after a passing audit.", url: "https://github.com/acme/tool/pull/42" },
  { at: "2026-11-20T11:59:00Z", kind: "audit_failed", title: "<img src=x onerror=alert(1)>", summary: "<b>not bold</b>", url: "javascript:alert(1)" },
  { at: "not a date", kind: "deploy", title: "Deployed abc1234", summary: "", url: null },
];

test("events render newest first as text, with a kind, a time and only https links", () => {
  const render = renderer(); const container = new Element("div");
  render(container, { ok: true, events });
  const items = container.all().filter(node => node.tagName === "ARTICLE");
  assert.equal(items.length, 3);
  assert.match(items[0].textContent, /Merged · 2026-11-20 12:00 UTC/);
  assert.match(items[0].textContent, /Merged acme\/tool#42/);
  assert.ok(items[1].textContent.includes("<img src=x onerror=alert(1)>"), "titles are plain text");
  assert.ok(items[1].textContent.includes("<b>not bold</b>"));
  assert.match(items[1].className, /audit_failed/);
  assert.match(items[2].textContent, /time unknown/);
  const links = container.all().filter(node => node.tagName === "A");
  assert.deepEqual(links.map(node => node.href), ["https://github.com/acme/tool/pull/42"]);
  assert.equal(links[0].rel, "noopener noreferrer");
  assert.equal(container.all().some(node => node.tagName === "IMG" || node.tagName === "B"), false);
});

test("empty, missing token and unreadable states are explained", () => {
  const render = renderer(); const container = new Element("div");
  render(container, { ok: true, events: [] });
  assert.match(container.textContent, /Nothing has happened yet/);
  render(container, { ok: false, tokenMissing: true });
  assert.match(container.textContent, /#token=/);
  render(container, { ok: false });
  assert.match(container.textContent, /cannot be read/);
});
