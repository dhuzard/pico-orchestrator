import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

// A minimal DOM: enough for viewer/issues.js, and it refuses innerHTML so any
// HTML parsing path fails the test instead of passing silently.
class FakeElement {
  tagName: string;
  className = "";
  children: FakeElement[] = [];
  dataset: Record<string, string> = {};
  href = "";
  target = "";
  rel = "";
  #text = "";
  constructor(tag: string) { this.tagName = tag.toUpperCase(); }
  get textContent(): string { return this.#text + this.children.map((child) => child.textContent).join(""); }
  set textContent(value: string) { this.#text = String(value); this.children = []; }
  set innerHTML(_value: string) { throw new Error("innerHTML must never be used for issue text"); }
  set outerHTML(_value: string) { throw new Error("outerHTML must never be used for issue text"); }
  insertAdjacentHTML(): never { throw new Error("insertAdjacentHTML must never be used for issue text"); }
  append(...nodes: FakeElement[]): void { this.children.push(...nodes); }
  replaceChildren(...nodes: FakeElement[]): void { this.children = [...nodes]; }
  all(): FakeElement[] { return [this, ...this.children.flatMap((child) => child.all())]; }
}

type Render = (container: FakeElement, data: unknown, options?: unknown) => void;

function loadRender(): Render {
  const created: string[] = [];
  const document = { createElement: (tag: string) => { created.push(tag); return new FakeElement(tag); } };
  const context: Record<string, unknown> = { document };
  context.globalThis = context;
  vm.runInNewContext(readFileSync(new URL("../viewer/issues.js", import.meta.url), "utf8"), context);
  return (context.PicoIssues as { render: Render }).render;
}

const HOSTILE = '<img src=x onerror="alert(1)"><script>alert(2)</script>';

test("issue titles and labels are rendered as text, never as markup", () => {
  const render = loadRender();
  const container = new FakeElement("div");
  render(container, {
    ok: true, mode: "repository", cap: 50, repositories: [{
      id: null, repository: "acme/app", available: true, truncated: false, cached: false, fetched_at: "2026-10-21T14:00:00Z",
      pull_requests: { available: true, scanned: 1, truncated: false }, branches: { available: true, scanned: 1 },
      issues: [{ number: 7, title: HOSTILE, labels: [HOSTILE], assignees: ["octo-cat"], updated_at: "2026-10-21T14:15:11Z",
        url: "javascript:alert(3)", references: { branches: ["pico/issue-7"], pull_requests: [{ number: 40, draft: true, url: "https://github.com/acme/app/pull/40" }] } }],
    }],
  });
  const nodes = container.all();
  assert.equal(nodes.some((node) => ["IMG", "SCRIPT"].includes(node.tagName)), false);
  assert.ok(nodes.some((node) => node.className === "issue-title" && node.textContent === HOSTILE));
  assert.ok(nodes.some((node) => node.className === "issue-label" && node.textContent === HOSTILE));
  // A non-GitHub URL is not turned into a link.
  assert.equal(nodes.some((node) => node.href.startsWith("javascript:")), false);
  const row = nodes.find((node) => node.className === "issue-row");
  assert.deepEqual(row?.dataset, { repository: "acme/app", issue: "7" });
  assert.ok(nodes.some((node) => node.tagName === "A" && node.href === "https://github.com/acme/app/pull/40"));
  assert.match(container.textContent, /branch pico\/issue-7/);
});

test("an unavailable repository is explicit, never an empty list", () => {
  const render = loadRender();
  const container = new FakeElement("div");
  render(container, { ok: true, mode: "workspace", cap: 50, repositories: [
    { id: "api", repository: "acme/api", available: false, error_code: "GH_UNAUTHENTICATED", error: "gh is not authenticated; run gh auth login", issues: null },
    { id: "web", repository: "acme/web", available: true, truncated: false, issues: [], pull_requests: { available: true }, branches: { available: true } },
  ] });
  const text = container.textContent;
  assert.match(text, /Issues unavailable: GH_UNAUTHENTICATED/);
  assert.match(text, /gh is not authenticated/);
  assert.equal(text.match(/No open issues\./g)?.length, 1);  // only the repository that really has none
  render(container, { ok: false, error_code: "CONTROL_TOKEN_INVALID", error: "open the Garden from the URL printed at launch" });
  assert.match(container.textContent, /Issues unavailable: CONTROL_TOKEN_INVALID/);
});

test("a per-row action hook can add a control to each row", () => {
  const render = loadRender();
  const container = new FakeElement("div");
  const seen: string[] = [];
  render(container, { ok: true, cap: 50, repositories: [{ id: "api", repository: "acme/api", available: true, truncated: false,
    issues: [{ number: 3, title: "t", labels: [], assignees: [], references: { branches: null, pull_requests: null } }] }] },
  { rowAction: (entry: { id: string }, issue: { number: number }) => { seen.push(`${entry.id}#${issue.number}`); return new FakeElement("button"); } });
  assert.deepEqual(seen, ["api#3"]);
  assert.ok(container.all().some((node) => node.tagName === "BUTTON"));
  assert.match(container.textContent, /branches unknown/);
});
