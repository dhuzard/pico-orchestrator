#!/usr/bin/env python3
"""Append-only dispatcher activity log, with an optional send-only Telegram notice.

Every event is one JSON line in `<state dir>/activity.jsonl` (mode 0600):
`{at, kind, title, summary, url, issue?, pr?, sha?}`. The Garden shows the latest
events (`GET /api/activity`); the person is informed afterwards, never asked.

Telegram is send-only: `sendMessage` to one configured chat. This module never
calls `getUpdates`, never reads a reply and never acts on a message. A failed
notice is logged (without the bot token) and never blocks the caller. Messages
carry the title, the summary and the URL only: no issue body, diff or secret.
When `notify.garden_url` is configured, a notice that asks the person to look or
act (approval needed, failed audit, error, a run that did not complete, a new
delivery) ends with a "Garden: <url>" line. That URL never carries the session token.

CLI for external tools (the local deploy script calls it over SSH):

    python3 tools/activity.py emit --config <dispatcher.json> --kind deploy \
        --title "Deployed abc1234" --summary "..." [--url https://...]
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

KINDS = ("approval_needed", "approval", "run_finished", "delivery_opened", "audit_passed", "audit_failed", "merged", "deploy", "error")
ICONS = {"approval_needed": "🙋", "approval": "✅", "run_finished": "🏁", "delivery_opened": "📬", "audit_passed": "🔎",
         "audit_failed": "⛔", "merged": "🌱", "deploy": "🚀", "error": "⚠️"}
SHA = re.compile(r"^[0-9a-f]{40}$")
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
TELEGRAM_TOKEN = re.compile(r"^[0-9]{1,20}:[A-Za-z0-9_-]{20,200}$")
TELEGRAM_CHAT = re.compile(r"^-?[0-9]{1,20}$")
ENV_LINE = re.compile(r"^([A-Z][A-Z0-9_]{0,63})=(.*)$")
LIMITS = {"title": 200, "summary": 1200, "url": 500}
FILENAME = "activity.jsonl"
# Kinds whose notice links the Garden: there is something to look at or do there. A finished run
# links it only when it did not complete; the dispatcher says so explicitly (`emit(..., garden=True)`).
GARDEN_KINDS = frozenset({"approval_needed", "audit_failed", "error", "delivery_opened"})


class ActivityError(ValueError):
    pass


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _text(value, limit: int) -> str:
    text = CONTROL.sub(" ", str(value or "")).strip()
    return text[:limit]


def safe_url(value) -> str | None:
    """Only absolute https URLs without credentials; anything else is dropped."""
    if not isinstance(value, str) or not value or len(value) > LIMITS["url"] or CONTROL.search(value):
        return None
    try:
        parts = urllib.parse.urlsplit(value)
    except ValueError:
        return None
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        return None
    return value


def garden_url(value) -> str:
    """`notify.garden_url`: an absolute https URL without credentials, query or fragment.

    The Garden's session token travels in the fragment (`#token=`); a configured link must never
    carry it, nor anything else that a chat history would keep. Raises ActivityError otherwise."""
    if not isinstance(value, str) or not value or len(value) > 300 or CONTROL.search(value) \
            or any(char.isspace() for char in value):
        raise ActivityError("notify.garden_url must be an https URL")
    if "#" in value or "?" in value:
        raise ActivityError("notify.garden_url must not have a query or a fragment (never put the session token in it)")
    try:
        parts = urllib.parse.urlsplit(value)
        parts.port  # noqa: B018 - raises ValueError on an invalid port
    except ValueError:
        raise ActivityError("notify.garden_url must be an https URL") from None
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or "@" in parts.netloc:
        raise ActivityError("notify.garden_url must be an https URL without credentials")
    return value


def event(kind: str, title: str, summary: str = "", url: str | None = None, *, issue=None, pr=None,
          sha=None, at: str | None = None) -> dict:
    """A validated event; raises ActivityError on an unknown kind or an empty title."""
    if kind not in KINDS:
        raise ActivityError(f"kind must be one of {', '.join(KINDS)}")
    entry = {"at": at or now(), "kind": kind, "title": _text(title, LIMITS["title"]),
             "summary": _text(summary, LIMITS["summary"]), "url": safe_url(url)}
    if not entry["title"]:
        raise ActivityError("title is required")
    if type(issue) is int and issue > 0:
        entry["issue"] = issue
    if type(pr) is int and pr > 0:
        entry["pr"] = pr
    if isinstance(sha, str) and SHA.fullmatch(sha):
        entry["sha"] = sha
    return entry


def log_path(cfg: dict) -> Path:
    return Path(cfg["state"]).parent / FILENAME


def append(path: Path, entry: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(json.dumps(entry, sort_keys=True, ensure_ascii=False) + "\n")


def read_private_env(path: Path) -> dict:
    """KEY=VALUE lines from a regular 0600 file owned by this user; nothing else is accepted.

    Same rule as the dispatcher's credential files. Values are never logged.
    """
    path = Path(path)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise ActivityError("credential file unavailable") from exc
    with os.fdopen(fd, "r", encoding="utf-8") as handle:
        info = os.fstat(handle.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise ActivityError("credential file must be owned by the service user and have mode 0600")
        try:
            text = handle.read(65536)
        except (OSError, UnicodeError) as exc:
            raise ActivityError("credential file unreadable") from exc
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        found = ENV_LINE.match(line)
        if found:
            value = found.group(2).strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            values[found.group(1)] = value
    return values


def telegram_message(entry: dict, garden: str | None = None) -> str:
    """Plain text: icon and title, summary, URL and, when given, a last "Garden: <url>" line."""
    lines = [f"{ICONS.get(entry['kind'], '•')} {entry['title']}"]
    if entry.get("summary"):
        lines.append(entry["summary"])
    if entry.get("url"):
        lines.append(entry["url"])
    text = "\n".join(lines)
    if garden and garden != entry.get("url"):
        tail = f"\nGarden: {garden}"
        return text[:4000 - len(tail)] + tail
    return text[:4000]


def send_telegram(env_file: Path, text: str, *, opener=urllib.request.urlopen, timeout: int = 10) -> None:
    """One `sendMessage` call. Raises ActivityError with a message that never contains the token."""
    values = read_private_env(env_file)
    token, chat = values.get("PICO_TELEGRAM_BOT_TOKEN", ""), values.get("PICO_TELEGRAM_CHAT_ID", "")
    if not TELEGRAM_TOKEN.fullmatch(token) or not TELEGRAM_CHAT.fullmatch(chat):
        raise ActivityError("PICO_TELEGRAM_BOT_TOKEN or PICO_TELEGRAM_CHAT_ID missing or malformed")
    body = json.dumps({"chat_id": chat, "text": text, "disable_web_page_preview": True}).encode()
    request = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body,
                                     headers={"Content-Type": "application/json"}, method="POST")
    try:
        with opener(request, timeout=timeout) as response:
            answer = json.loads(response.read(65536) or b"{}")
    except urllib.error.HTTPError as exc:
        raise ActivityError(f"Telegram refused the message (HTTP {exc.code})") from None
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise ActivityError(f"Telegram unreachable ({type(exc).__name__})") from None
    except ValueError:
        raise ActivityError("Telegram returned an invalid answer") from None
    if not isinstance(answer, dict) or answer.get("ok") is not True:
        raise ActivityError("Telegram did not accept the message")


def notify(cfg: dict, entry: dict, *, sender=None, garden: bool | None = None) -> bool:
    """Best-effort notice; True when sent. Never raises.

    `garden` asks for the Garden link (None: only for GARDEN_KINDS); it is added only when
    `notify.garden_url` is configured."""
    sender = sender or send_telegram
    settings = cfg.get("notify") or {}
    env_file = settings.get("telegram_env_file")
    if not env_file:
        return False
    wanted = entry.get("kind") in GARDEN_KINDS if garden is None else garden
    try:
        sender(Path(env_file), telegram_message(entry, settings.get("garden_url") if wanted else None))
        return True
    except Exception as exc:  # noqa: BLE001 - a notice must never stop the dispatcher
        detail = str(exc) if isinstance(exc, ActivityError) else type(exc).__name__
        print(f"pico-activity: notification not sent: {detail}", file=sys.stderr, flush=True)
        return False


def emit(cfg: dict, kind: str, title: str, summary: str = "", url: str | None = None, *, sender=None,
         garden: bool | None = None, **fields) -> dict | None:
    """Append one event to the activity log and notify. Never raises: activity is a report, not authority."""
    try:
        entry = event(kind, title, summary, url, **fields)
    except ActivityError as exc:
        print(f"pico-activity: event refused: {exc}", file=sys.stderr, flush=True)
        return None
    try:
        append(log_path(cfg), entry)
    except (OSError, KeyError, TypeError) as exc:
        print(f"pico-activity: activity log not written: {type(exc).__name__}", file=sys.stderr, flush=True)
    notify(cfg, entry, sender=sender, garden=garden)
    return entry


def recent(path: Path, limit: int = 50, *, max_bytes: int = 1_048_576) -> list[dict]:
    """The latest `limit` valid events, newest first. Only known fields, re-validated, are returned."""
    path = Path(path)
    if not path.is_file():
        return []
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - max_bytes))
        raw = handle.read()
    lines = raw.decode("utf-8", errors="replace").splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]  # the first line may be cut
    result = []
    for line in reversed(lines):
        try:
            data = json.loads(line)
            if not isinstance(data, dict) or not isinstance(data.get("at"), str):
                continue
            entry = event(data.get("kind"), data.get("title"), data.get("summary") or "", data.get("url"),
                          issue=data.get("issue"), pr=data.get("pr"), sha=data.get("sha"), at=_text(data["at"], 40))
        except (ValueError, ActivityError):
            continue
        result.append(entry)
        if len(result) >= limit:
            break
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Append a dispatcher activity event and notify")
    sub = parser.add_subparsers(dest="action", required=True)
    emit_parser = sub.add_parser("emit", help="append one event and send its notice")
    emit_parser.add_argument("--config", type=Path, required=True, help="dispatcher.json")
    emit_parser.add_argument("--kind", required=True, choices=KINDS)
    emit_parser.add_argument("--title", required=True)
    emit_parser.add_argument("--summary", default="")
    emit_parser.add_argument("--url")
    emit_parser.add_argument("--issue", type=int)
    emit_parser.add_argument("--pr", type=int)
    emit_parser.add_argument("--sha")
    args = parser.parse_args(argv)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import issue_dispatcher  # late: the dispatcher imports this module
    try:
        cfg = issue_dispatcher.load_config(args.config)
        entry = event(args.kind, args.title, args.summary, args.url, issue=args.issue, pr=args.pr, sha=args.sha)
    except (issue_dispatcher.DispatcherError, ActivityError, OSError, ValueError) as exc:
        print(f"pico-activity: {exc}", file=sys.stderr)
        return 1
    if args.url and entry["url"] is None:
        print("pico-activity: --url must be an absolute https URL", file=sys.stderr)
        return 1
    try:
        append(log_path(cfg), entry)
    except OSError as exc:
        print(f"pico-activity: activity log not written: {type(exc).__name__}", file=sys.stderr)
        return 1
    notify(cfg, entry)
    print(json.dumps(entry, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
