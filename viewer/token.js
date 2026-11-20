// The Garden's session token. It arrives once in the launch URL's fragment (`#token=…`), is
// removed from the address bar and remembered in this browser (localStorage, one key per
// origin), so a later tokenless link (a notice's "Garden: <url>") opens the Garden again.
// A token the server rejects is forgotten: the page then asks for the URL printed at launch.
// Storage can be unavailable (private mode, blocked site data): the token then lives in memory only.
(function (root) {
  "use strict";
  const FRAGMENT = /(?:^#|&)token=([A-Za-z0-9_-]{20,})/;
  const VALID = /^[A-Za-z0-9_-]{20,512}$/;
  const keyFor = location => `pico-garden-token:${location.origin}`;
  function read(storage, key) {
    try { const value = storage?.getItem(key); return typeof value === "string" && VALID.test(value) ? value : null; } catch { return null; }
  }
  function write(storage, key, value) {
    try { if (value) storage?.setItem(key, value); else storage?.removeItem(key); } catch { /* memory only */ }
  }
  function storageOf(scope) {
    try { return scope.localStorage; } catch { return null; }
  }
  // The token for this page: from the fragment (stored, then stripped from the URL), else the stored one.
  function load(scope = root) {
    const { location, history } = scope;
    const storage = storageOf(scope);
    const match = FRAGMENT.exec(location.hash || "");
    if (match) {
      write(storage, keyFor(location), match[1]);
      history.replaceState(null, "", location.pathname);
      return match[1];
    }
    return read(storage, keyFor(location));
  }
  function forget(scope = root) {
    write(storageOf(scope), keyFor(scope.location), null);
  }
  // A reply that says the session token was refused (not a Tailscale identity or origin refusal).
  const rejected = data => Boolean(data && typeof data === "object" && data.error_code === "CONTROL_TOKEN_INVALID");
  root.PicoToken = { load, forget, rejected };
})(globalThis);
