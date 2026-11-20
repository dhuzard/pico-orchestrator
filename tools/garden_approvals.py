"""Approve dispatcher queue issues from the Garden (`pico watch --approvals`).

This is the Garden twin of `issue_dispatcher.py approve --issue N [--allow-mutation]`.
Both call `issue_dispatcher.record_approval`, the single approval core: it re-reads
the issue with the queue credential, parses it against the operator allowlist,
refuses a dispatched task, and writes the same `approvals` row under BEGIN
IMMEDIATE. The Garden never bypasses a check; it only replaces the typed digest
with a two-step preview and confirmation:

1. A POST without `confirm` re-reads the issue and returns a preview (target,
   checkout, objective, acceptance criteria, mode, digest, actor) and a
   confirmation: an HMAC over [action, issue, digest, repo, checkout, mode, actor].
2. A POST with `confirm` and the previewed `digest` runs the core; inside it, on
   the freshly re-read issue, the HMAC is recomputed and compared, so an edited
   issue, a changed allowlist entry, another mode or another person is refused
   (CONTROL_PREVIEW_STALE). The core then checks the confirmed digest itself.

Identity: with `--require-tailscale-login`, every approvals request (reads too)
must carry a `Tailscale-User-Login` header naming an allowed login. Tailscale
Serve sets that header on requests it proxies and overwrites a client-supplied
copy, so the login names the tailnet user. The session token is still required:
it keeps other local users, who could reach the loopback port with a forged
header, out. Without `--require-tailscale-login` (loopback only), the actor is
"local-token". There is no repair (`--repair-of`), reject or dispatch action.
Each confirmed attempt is appended to `<state-dir>/garden-approvals.jsonl`.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import stat
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import garden_controls  # noqa: E402
import issue_dispatcher  # noqa: E402
from garden_controls import ControlError  # noqa: E402

MODES = {"read-only": False, "local-mutation": True}
MODE_LABELS = {"read-only": "READ-ONLY", "local-mutation": "LOCAL CODE MUTATION"}
LOGIN_HEADER = "Tailscale-User-Login"
LOGIN_RE = re.compile(r"^[A-Za-z0-9._@+-]{1,254}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,256}$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
FIELDS = frozenset({"action", "issue", "mode", "confirm", "digest", "repair_of"})
LOCAL_ACTOR = "local-token"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_or_create_token(path: Path) -> str:
    """A persistent session token: a 0600 regular file owned by this user, created when missing."""
    path = Path(path).expanduser()
    if not path.is_absolute():
        raise ValueError("--token-file must be an absolute path")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(secrets.token_urlsafe(32) + "\n")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise ValueError("--token-file cannot be opened (a symlink is refused)") from exc
    with os.fdopen(fd, "r", encoding="utf-8") as handle:
        info = os.fstat(handle.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise ValueError("--token-file must be a regular file owned by this user with mode 0600")
        token = handle.read(1024).strip()
    if not TOKEN_RE.fullmatch(token):
        raise ValueError("--token-file must hold one URL-safe token of at least 32 characters")
    return token


class GardenApprovals:
    """Serves GET and POST /api/approvals. One approval at a time."""

    def __init__(self, port: int, dispatcher_config: Path | dict, *, token: str | None = None,
                 allowed_hosts=(), required_logins=(), audit_path: Path | None = None,
                 dispatcher=issue_dispatcher):
        self.port = port
        self.dispatcher = dispatcher
        self.cfg = (dispatcher_config if isinstance(dispatcher_config, dict)
                    else dispatcher.load_config(Path(dispatcher_config)))
        self.allowed_hosts = frozenset(allowed_hosts)
        self.required_logins = frozenset(required_logins)
        if self.allowed_hosts and not self.required_logins:
            raise ValueError("remote approvals (--allowed-host) need --require-tailscale-login")
        for login in self.required_logins:
            if not LOGIN_RE.fullmatch(login):
                raise ValueError(f"invalid Tailscale login: {login!r}")
        self.audit_path = audit_path
        self.token = token or secrets.token_urlsafe(32)
        self._secret = secrets.token_bytes(32)
        self._lock = threading.Lock()
        self._done: set[str] = set()

    # -- request checks ------------------------------------------------------
    def check_token(self, headers) -> None:
        garden_controls.check_session_token(headers, self.token)

    def actor(self, headers) -> str:
        """The Tailscale login when one is required, else the local token holder."""
        if not self.required_logins:
            return LOCAL_ACTOR
        values = headers.get_all(LOGIN_HEADER) if hasattr(headers, "get_all") else [headers.get(LOGIN_HEADER)]
        values = [value for value in values or [] if value is not None]
        if len(values) != 1 or values[0] not in self.required_logins:
            raise ControlError(403, "APPROVAL_IDENTITY_REJECTED",
                               "approvals need a Tailscale login this Garden allows; open it through Tailscale Serve")
        return values[0]

    def authorize_read(self, headers) -> str:
        self.check_token(headers)
        return self.actor(headers)

    def authorize(self, headers, body: bytes) -> tuple[dict, str]:
        data = garden_controls.authorize_json(headers, body, port=self.port, token=self.token,
                                              allowed_hosts=self.allowed_hosts)
        return data, self.actor(headers)

    # -- read ------------------------------------------------------------------
    def listing(self, actor: str) -> dict:
        try:
            issues = self.dispatcher.queued_issues(self.cfg)
        except self.dispatcher.DispatcherError as exc:
            raise ControlError(502, "APPROVAL_QUEUE_UNAVAILABLE", f"the queue cannot be read: {exc}") from exc
        try:
            states = self.dispatcher.approval_states(self.cfg)
        except (OSError, ValueError, sqlite3.Error) as exc:
            raise ControlError(503, "APPROVAL_STATE_UNAVAILABLE", "the dispatcher state cannot be read") from exc
        items = [self._item(issue, states) for issue in issues if isinstance(issue, dict)]
        items.sort(key=lambda item: item["number"] or 0)
        return {"ok": True, "queue": self.cfg["queue"], "actor": actor, "modes": list(MODES), "issues": items}

    def _item(self, issue: dict, states: dict) -> dict:
        number = issue.get("number") if type(issue.get("number")) is int else None
        title = issue.get("title") if isinstance(issue.get("title"), str) else ""
        item = {"number": number, "title": title[:300], "target": None, "objective": None, "criteria": None,
                "digest_short": None, "error": None, "state": "not_approved", "approved_mode": None,
                "approved_current": False, "approvable": False}
        try:
            sections = self.dispatcher.task_sections(issue.get("body"))
            item.update(target=sections["Target repository"][:200], objective=sections["Objective"][:3200],
                        criteria=sections["Acceptance criteria"][:700])
        except self.dispatcher.DispatcherError:
            pass
        try:
            task = self.dispatcher.parse_task(issue, self.cfg)
        except self.dispatcher.DispatcherError as exc:
            item["error"] = str(exc)
            task = None
        row = states.get(number)
        if row is not None:
            item.update(state=row["state"], approved_mode=row["mode"],
                        approved_current=task is not None and row["digest"] == task.digest)
        if task is not None:
            item["digest_short"] = task.digest[:12]
            item["approvable"] = row is None or row["state"] in self.dispatcher.APPROVABLE
        return item

    # -- the action --------------------------------------------------------------
    def act(self, data: dict, actor: str) -> dict:
        if not self._lock.acquire(blocking=False):
            raise ControlError(409, "CONTROL_BUSY", "another approval is running; try again when it finishes")
        try:
            number, mode, confirm, digest = self._parse(data)
            entry = {"issue": number, "mode": mode, "actor": actor, "digest": digest}
            try:
                result = self._handle(number, mode, confirm, digest, actor, entry)
            except ControlError as exc:
                result = {"ok": False, "stage": "refused", "error_code": exc.code, "error": exc.detail}
            if confirm is not None:
                self._audit({**entry, "stage": result.get("stage"), "ok": bool(result.get("ok")),
                             "error_code": result.get("error_code")})
            return {"action": "approve", **result}
        finally:
            self._lock.release()

    @staticmethod
    def _parse(data: dict) -> tuple[int, str, str | None, str | None]:
        if set(data) - FIELDS:
            raise ControlError(400, "APPROVAL_INVALID", "unexpected field in the approval request")
        if data.get("repair_of") is not None:
            raise ControlError(400, "APPROVAL_REPAIR_UNSUPPORTED",
                               "repair approvals stay on the terminal: issue_dispatcher.py approve --repair-of")
        if data.get("action", "approve") != "approve":
            raise ControlError(400, "APPROVAL_INVALID", "the only action is approve")
        number, mode, confirm, digest = data.get("issue"), data.get("mode"), data.get("confirm"), data.get("digest")
        if type(number) is not int or number < 1:
            raise ControlError(400, "APPROVAL_INVALID", "issue must be a positive integer")
        if mode not in MODES:
            raise ControlError(400, "APPROVAL_INVALID", "mode must be read-only or local-mutation")
        if confirm is not None and not isinstance(confirm, str):
            raise ControlError(400, "APPROVAL_INVALID", "confirm must be a string")
        if confirm is not None and not (isinstance(digest, str) and DIGEST_RE.fullmatch(digest)):
            raise ControlError(400, "APPROVAL_INVALID", "a confirmation must return the previewed digest")
        return number, mode, confirm, digest if confirm is not None else None

    def _confirmation(self, task, mode: str, actor: str) -> str:
        parts = ["approve", task.number, task.digest, task.repo, task.checkout, mode, actor]
        message = json.dumps(parts, separators=(",", ":")).encode()
        return hmac.new(self._secret, message, hashlib.sha256).hexdigest()

    def _handle(self, number: int, mode: str, confirm: str | None, digest: str | None, actor: str,
                entry: dict) -> dict:
        dispatcher = self.dispatcher
        try:
            if confirm is None:
                return self._preview(number, mode, actor)

            def confirmed(task, repair):
                # Runs inside the shared core, on the issue it has just re-read.
                entry["digest"] = task.digest
                if not hmac.compare_digest(confirm.encode(), self._confirmation(task, mode, actor).encode()):
                    raise ControlError(409, "CONTROL_PREVIEW_STALE",
                                       "the issue changed since the preview; reload and review it again")
                if confirm in self._done:
                    raise ControlError(409, "APPROVAL_ALREADY_SENT", "this approval was already recorded")
                return digest

            task = dispatcher.record_approval(self.cfg, number, MODES[mode], confirmed)
        except dispatcher.TaskInvalid as exc:
            raise ControlError(422, "APPROVAL_TASK_INVALID", str(exc)) from exc
        except dispatcher.AlreadyDispatched as exc:
            raise ControlError(409, "APPROVAL_ALREADY_DISPATCHED", str(exc)) from exc
        except dispatcher.DispatcherError as exc:
            if str(exc) == "approval cancelled":
                raise ControlError(409, "CONTROL_PREVIEW_STALE",
                                   "the issue changed since the preview; reload and review it again") from exc
            raise ControlError(502, "APPROVAL_DISPATCHER_ERROR", str(exc)[:300]) from exc
        except (OSError, sqlite3.Error) as exc:
            raise ControlError(503, "APPROVAL_STATE_UNAVAILABLE", "the dispatcher state cannot be written") from exc
        self._done.add(confirm)
        return {"ok": True, "stage": "done", "status": "APPROVED", "issue": number, "mode": mode,
                "digest_short": task.digest[:12], "actor": actor}

    def _preview(self, number: int, mode: str, actor: str) -> dict:
        dispatcher = self.dispatcher
        issue = dispatcher.issue_view(self.cfg, number)
        task = dispatcher.parse_task(issue, self.cfg)
        sections = dispatcher.task_sections(issue["body"])
        row = dispatcher.approval_states(self.cfg).get(number)
        if row is not None and row["state"] not in dispatcher.APPROVABLE:
            raise dispatcher.AlreadyDispatched("task was already dispatched; create a new issue for a new attempt")
        return {"ok": True, "stage": "preview", "confirm": self._confirmation(task, mode, actor),
                "preview": {"issue": number, "queue": self.cfg["queue"], "title": str(issue.get("title", ""))[:300],
                            "target": task.repo, "checkout": task.checkout, "mode": mode,
                            "mode_label": MODE_LABELS[mode], "objective": sections["Objective"],
                            "criteria": sections["Acceptance criteria"], "digest": task.digest,
                            "digest_short": task.digest[:12], "actor": actor,
                            "current_state": row["state"] if row else "not_approved",
                            "note": ("PICO autonomy policy still applies; this is not an OS sandbox or merge "
                                     "authorization. Editing the issue before dispatch invalidates this approval.")}}

    def _audit(self, entry: dict) -> None:
        """Append-only local record: time, issue, digest, mode, actor and result. Never the issue text."""
        if self.audit_path is None:
            return
        line = json.dumps({"at": _now(), **{key: (value[:300] if isinstance(value, str) else value)
                                            for key, value in entry.items()}}, sort_keys=True)
        try:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.audit_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as log:
                log.write(line + "\n")
        except OSError:
            pass  # the approval row is the authority; a log failure must not hide its result
