"""Quick human feedback on an observed delivery, sent from the Garden (`pico watch --feedback`).

A person writes a short note on a delivery card and sends it to the delivery's pull
request as their own GitHub account (whatever `gh` is logged in as): either a PR
comment, or a "request changes" review pinned to the commit Pico observed. Garden
never approves, merges or starts a model, and a comment grants no authority; a repair
still needs a separately authorized execution.

Every send previews first. The preview names the author, repository, PR, observed
head and exact text, and returns a confirmation bound to all of them. The send
re-reads the delivery and the live PR immediately before posting and refuses when
anything moved: a new head, a closed PR, a cross-repository PR, or a delivery that
is no longer open. A change request is pinned to the observed commit; a comment is
not bound to a commit, so a push landing in the instant between that check and the
post is not excluded for comments. The PR always comes from the dispatcher's
observation, never from the browser, and every GitHub call is pinned to github.com.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path

import delivery_store
import garden_controls
import github_sync
from garden_controls import ControlError

KINDS = ("comment", "request_changes")
MAX_TEXT = 4000
CLOSED_DELIVERIES = frozenset({"merged", "closed", "cancelled"})  # as the Garden hides them
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clip(text: object, limit: int = 200) -> str:
    return str(text)[:limit]


class DeliveryFeedback:
    """Serves POST /api/delivery-feedback. One send at a time.

    Confirmations are deterministic per (delivery, head, kind, text, author), and a sent one is
    remembered for the session: the same text cannot be sent twice to the same commit by mistake."""

    def __init__(self, port: int, dispatcher_state: Path, *, repository: str | None = None,
                 audit_path: Path | None = None, adapter=github_sync, token: str | None = None,
                 allowed_hosts=(), required_logins=()):
        self.port = port
        self.allowed_hosts = frozenset(allowed_hosts)
        self.required_logins = frozenset(required_logins)
        if self.allowed_hosts and not self.required_logins:
            raise ValueError("remote feedback (--allowed-host) needs --require-tailscale-login")
        self.dispatcher_state = Path(dispatcher_state)
        self.repository = repository
        self.audit_path = audit_path
        self.adapter = adapter
        self.token = token or secrets.token_urlsafe(32)
        self._secret = secrets.token_bytes(32)
        self._lock = threading.Lock()
        self._sent: set[str] = set()

    # -- request checks (shared with the workspace controls) ----------------
    def check_token(self, headers) -> None:
        garden_controls.check_session_token(headers, self.token)

    def authorize(self, headers, body: bytes) -> dict:
        """Loopback origin as before; the https origin of an allowed host only with a verified Tailscale login."""
        data = garden_controls.authorize_json(headers, body, port=self.port, token=self.token,
                                              allowed_hosts=self.allowed_hosts)
        if not garden_controls.origin_allowed(headers.get("Origin"), self.port):
            garden_controls.tailscale_login(headers, self.required_logins)
        return data

    # -- the action ----------------------------------------------------------
    def act(self, data: dict) -> dict:
        if not self._lock.acquire(blocking=False):
            raise ControlError(409, "CONTROL_BUSY", "another send is running; try again when it finishes")
        try:
            issue, kind, text, confirm = self._parse(data)
            entry = {"issue": issue, "kind": kind}
            try:
                result = self._handle(issue, kind, text, confirm, entry)
            except ControlError as exc:
                result = {"ok": False, "stage": "refused", "error_code": exc.code, "error": exc.detail}
            if confirm is not None:
                self._audit({**entry, "stage": result.get("stage"), "ok": bool(result.get("ok")),
                             "error_code": result.get("error_code"),
                             "text_sha256": hashlib.sha256(text.encode()).hexdigest(), "text_length": len(text)})
            return {"action": "delivery_feedback", **result}
        finally:
            self._lock.release()

    @staticmethod
    def _parse(data: dict) -> tuple[int, str, str, str | None]:
        issue, kind, text, confirm = data.get("issue"), data.get("kind"), data.get("body"), data.get("confirm")
        if type(issue) is not int or issue < 1:
            raise ControlError(400, "FEEDBACK_INVALID", "issue must be a positive integer")
        if kind not in KINDS:
            raise ControlError(400, "FEEDBACK_INVALID", "kind must be comment or request_changes")
        if not isinstance(text, str):
            raise ControlError(400, "FEEDBACK_INVALID", "body must be text")
        text = text.replace("\r\n", "\n").strip()
        if not text:
            raise ControlError(400, "FEEDBACK_EMPTY", "write some feedback first")
        if len(text) > MAX_TEXT:
            raise ControlError(400, "FEEDBACK_TOO_LONG", f"feedback is limited to {MAX_TEXT} characters")
        if _CONTROL_CHARS.search(text):
            raise ControlError(400, "FEEDBACK_INVALID", "feedback contains control characters")
        if confirm is not None and not isinstance(confirm, str):
            raise ControlError(400, "FEEDBACK_INVALID", "confirm must be a string")
        return issue, kind, text, confirm

    def _delivery(self, issue: int) -> dict:
        try:
            rows = delivery_store.list_deliveries(self.dispatcher_state)
        except (OSError, ValueError) as exc:
            raise ControlError(503, "DELIVERY_STATE_UNAVAILABLE", "delivery observations are unavailable") from exc
        for row in rows:
            if row.get("issue") != issue:
                continue
            if self.repository is not None and str(row.get("repository", "")).casefold() != self.repository.casefold():
                break  # outside this Garden's repository: treated as unknown
            return row
        raise ControlError(404, "DELIVERY_UNKNOWN", "no observed delivery for this queue issue")

    def _target(self, issue: int, kind: str) -> tuple[dict, dict, str]:
        """Observed delivery, live PR and author login, or a refusal naming what moved."""
        row = self._delivery(issue)
        if row.get("delivery") in CLOSED_DELIVERIES:
            raise ControlError(409, "DELIVERY_CLOSED", "this delivery is already merged, closed or cancelled")
        number, head = row.get("pr_number"), row.get("head_sha")
        if row.get("publication") != "observed" or type(number) is not int or not isinstance(head, str):
            raise ControlError(409, "DELIVERY_PR_UNVERIFIED", "no verified pull request was observed for this delivery")
        repository = row["repository"]
        try:
            login = self.adapter.current_login()
            live = self.adapter.pr_review_target(number, repo=repository)
        except github_sync.GhAuthError as exc:
            raise ControlError(503, "FEEDBACK_GH_AUTH", "gh is not logged in; run gh auth login") from exc
        except github_sync.GhUnavailableError as exc:
            raise ControlError(503, "FEEDBACK_GH_UNAVAILABLE", "the gh CLI is not installed") from exc
        except (github_sync.GithubSyncError, ValueError) as exc:
            raise ControlError(502, "FEEDBACK_GITHUB_FAILED", "could not read the pull request from GitHub") from exc
        if live["state"] != "OPEN":
            raise ControlError(409, "DELIVERY_PR_NOT_OPEN", f"the pull request is {live['state'].lower()}")
        if live["cross_repository"]:
            raise ControlError(409, "DELIVERY_PR_CROSS_REPOSITORY", "the pull request head is in another repository")
        if live["head"] != head:
            raise ControlError(409, "DELIVERY_HEAD_CHANGED",
                               "the pull request moved past the observed commit; wait for the next observation")
        if kind == "request_changes" and live["author"].casefold() == login.casefold():
            raise ControlError(422, "FEEDBACK_OWN_PR",
                               "GitHub does not let you request changes on your own pull request; send a comment")
        return row, live, login

    def _confirmation(self, row: dict, kind: str, text: str, login: str) -> str:
        parts = ["delivery_feedback", row["issue"], row["repository"], row["pr_number"], row["head_sha"], kind,
                 hashlib.sha256(text.encode()).hexdigest(), login]
        message = json.dumps(parts, separators=(",", ":")).encode()
        return hmac.new(self._secret, message, hashlib.sha256).hexdigest()

    def _handle(self, issue: int, kind: str, text: str, confirm: str | None, entry: dict) -> dict:
        row, live, login = self._target(issue, kind)
        entry.update(repository=row["repository"], pr=row["pr_number"], head=row["head_sha"], author=login)
        expected = self._confirmation(row, kind, text, login)
        if confirm is None:
            return {"ok": True, "stage": "preview", "confirm": expected,
                    "preview": {"repository": row["repository"], "pr_number": row["pr_number"],
                                "pr_url": f"https://github.com/{row['repository']}/pull/{row['pr_number']}",
                                "head_sha": row["head_sha"], "kind": kind, "body": text, "author": login,
                                "note": ("Posts to GitHub as you. A comment grants no authority; "
                                         "a repair still needs a separately authorized execution.")}}
        if not hmac.compare_digest(confirm, expected):
            raise ControlError(409, "CONTROL_PREVIEW_STALE", "the delivery or text changed since the preview; review it again")
        if expected in self._sent:
            raise ControlError(409, "FEEDBACK_ALREADY_SENT", "this feedback was already sent")
        try:
            if kind == "comment":
                url = self.adapter.post_pr_comment(row["pr_number"], text, repo=row["repository"])
            else:
                url = self.adapter.request_changes(row["pr_number"], text, repo=row["repository"], commit=row["head_sha"])
        except github_sync.GhTimeoutError as exc:
            raise ControlError(504, "FEEDBACK_OUTCOME_UNKNOWN",
                               "GitHub did not answer in time; check the pull request before sending again") from exc
        except github_sync.GhAuthError as exc:
            raise ControlError(503, "FEEDBACK_GH_AUTH", "gh is not logged in; run gh auth login") from exc
        except (github_sync.GithubSyncError, ValueError) as exc:
            raise ControlError(502, "FEEDBACK_GITHUB_FAILED", "GitHub refused the feedback") from exc
        self._sent.add(expected)
        prefix = f"https://github.com/{row['repository']}/pull/{row['pr_number']}"
        url = url if isinstance(url, str) and (url == prefix or url.startswith(prefix + "#")) else prefix
        entry["url"] = url
        return {"ok": True, "stage": "done", "status": "COMMENT_POSTED" if kind == "comment" else "CHANGES_REQUESTED",
                "url": url}

    def _audit(self, entry: dict) -> None:
        """Append-only local record of sends: who, where, which commit, a digest of the text (never the text)."""
        if self.audit_path is None:
            return
        line = json.dumps({"at": _now(), **{key: (_clip(value) if isinstance(value, str) else value)
                                            for key, value in entry.items()}}, sort_keys=True)
        try:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.audit_path, "a", encoding="utf-8") as log:
                log.write(line + "\n")
        except OSError:
            pass  # the send itself is recorded on GitHub; a local log failure must not hide its result
