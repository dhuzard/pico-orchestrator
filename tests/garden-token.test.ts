import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const TOKEN = "abcdefghijklmnopqrstuvwxyz0123456789_-AB";
const ORIGIN = "https://pico-pilot.tail35e544.ts.net";
const KEY = `pico-garden-token:${ORIGIN}`;

class Storage {
  values = new Map<string, string>();
  getItem(key: string) { return this.values.has(key) ? this.values.get(key)! : null; }
  setItem(key: string, value: string) { this.values.set(key, String(value)); }
  removeItem(key: string) { this.values.delete(key); }
}
const throwing = { getItem() { throw new Error("blocked"); }, setItem() { throw new Error("blocked"); }, removeItem() { throw new Error("blocked"); } };

type Token = { load: () => string | null; forget: () => void; rejected: (data: unknown) => boolean };
function page(hash: string, storage: unknown, options: { storageThrows?: boolean } = {}) {
  const replaced: string[] = [];
  const location = { hash, origin: ORIGIN, pathname: "/" };
  const context: Record<string, unknown> = { location, history: { replaceState: (_s: unknown, _t: string, url: string) => { replaced.push(url); location.hash = ""; } } };
  if (options.storageThrows) Object.defineProperty(context, "localStorage", { get() { throw new Error("SecurityError"); } });
  else context.localStorage = storage;
  context.globalThis = context;
  vm.runInNewContext(readFileSync(new URL("../viewer/token.js", import.meta.url), "utf8"), context);
  return { token: context.PicoToken as Token, replaced };
}

test("a #token= link is stored per origin and stripped from the address bar", () => {
  const storage = new Storage();
  const first = page(`#token=${TOKEN}`, storage);
  assert.equal(first.token.load(), TOKEN);
  assert.deepEqual(first.replaced, ["/"]);
  assert.equal(storage.getItem(KEY), TOKEN);
  const later = page("", storage); // a plain Garden link, as in a notice
  assert.equal(later.token.load(), TOKEN);
  assert.deepEqual(later.replaced, [], "nothing to strip");
});

test("a new #token= link replaces the stored token; other fragments are left alone", () => {
  const storage = new Storage();
  storage.setItem(KEY, "old-token-old-token-old-token");
  const newer = "N".repeat(32);
  assert.equal(page(`#token=${newer}`, storage).token.load(), newer);
  assert.equal(storage.getItem(KEY), newer);
  const runs = page("#runs", storage);
  assert.equal(runs.token.load(), newer);
  assert.deepEqual(runs.replaced, [], "the run-view fragment is not removed");
});

test("a refused token is forgotten; identity or origin refusals are not token errors", () => {
  const storage = new Storage();
  const { token } = page(`#token=${TOKEN}`, storage);
  token.load();
  assert.equal(token.rejected({ ok: false, error_code: "CONTROL_TOKEN_INVALID" }), true);
  assert.equal(token.rejected({ ok: false, error_code: "APPROVAL_IDENTITY_REJECTED" }), false);
  assert.equal(token.rejected({ ok: false, error_code: "CONTROL_ORIGIN_REJECTED" }), false);
  assert.equal(token.rejected(null), false);
  token.forget();
  assert.equal(storage.getItem(KEY), null);
  assert.equal(page("", storage).token.load(), null);
});

test("malformed stored values are ignored", () => {
  const storage = new Storage();
  storage.setItem(KEY, "short");
  assert.equal(page("", storage).token.load(), null);
  storage.setItem(KEY, "x".repeat(30) + "\"><script>");
  assert.equal(page("", storage).token.load(), null);
});

test("unavailable storage keeps the token in memory only and never throws", () => {
  for (const options of [{ storage: throwing }, { storage: undefined, storageThrows: true }]) {
    const fromLink = page(`#token=${TOKEN}`, options.storage, { storageThrows: options.storageThrows });
    assert.equal(fromLink.token.load(), TOKEN);
    assert.deepEqual(fromLink.replaced, ["/"]);
    assert.doesNotThrow(() => fromLink.token.forget());
    assert.equal(page("", options.storage, { storageThrows: options.storageThrows }).token.load(), null);
  }
});

test("the page loads token.js before app.js and the server serves it", () => {
  const html = readFileSync(new URL("../viewer/index.html", import.meta.url), "utf8");
  assert.ok(html.indexOf('src="/token.js"') > 0 && html.indexOf('src="/token.js"') < html.indexOf('src="/app.js"'));
  assert.doesNotMatch(html, /<script(?![^>]*\bsrc=)[^>]*>/, "no inline script (CSP script-src 'self')");
  const watch = readFileSync(new URL("../tools/watch.py", import.meta.url), "utf8");
  assert.match(watch, /"\/token\.js": \("token\.js", "text\/javascript; charset=utf-8"\)/);
  const app = readFileSync(new URL("../viewer/app.js", import.meta.url), "utf8");
  assert.match(app, /PicoToken\.load\(\)/);
  assert.doesNotMatch(app, /localStorage/, "storage is reached only through token.js");
});
