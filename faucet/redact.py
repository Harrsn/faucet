"""Keep credentials out of error strings, history and logs.

`requests` exception messages embed the full request URL, and Torznab / TMDb
put their API keys in the query string — so a Jackett timeout used to surface
`...&apikey=<key>` in HTTP 502 bodies, the Settings "test connections"
output, the history table and the container log. Everything that turns an
exception into text a human (or the DB) will see goes through redact().

Dependency-free on purpose: faucet/sort.py runs as a hook subprocess and the
clients package must not import the app.
"""
from __future__ import annotations

import logging
import os
import re

MASK = "***"

# key=value pairs in URLs / query strings / form bodies
_PARAM = re.compile(
    r"(?i)\b(api_?key|apikey|passkey|token|access_token|password|passwd|pass|"
    r"secret|auth|sid|x-transmission-session-id)=([^&\s'\"<>]+)")
# user:password@ in URLs
_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)([^/\s:@]+):([^/\s@]+)@")
# Telegram bot tokens live in the path: /bot123456:ABC-DEF.../sendMessage
_TG_BOT = re.compile(r"/bot\d+:[A-Za-z0-9_\-]+")
# Discord webhook tokens: /api/webhooks/<id>/<token>
_DISCORD = re.compile(r"(/api/webhooks/\d+/)[A-Za-z0-9_\-]+")

# Live secret values from the environment, masked wherever they appear even
# outside a recognisable key=value shape. Read at call time: Settings saves
# swap them in os.environ.
_SECRET_ENV = ("JACKETT_API_KEY", "CLIENT_PASS", "TMDB_API_KEY", "SESSION_SECRET")


def _live_secrets() -> list[str]:
    vals = [os.environ.get(k, "") for k in _SECRET_ENV]
    return [v for v in vals if v and len(v) >= 6]


def redact(text, *secrets: str | None) -> str:
    """`text` with credentials masked. Extra `secrets` (e.g. the key a test
    connection is using, which may not be saved yet) are masked verbatim."""
    s = "" if text is None else str(text)
    if not s:
        return s
    for v in (*[x for x in secrets if x and len(x) >= 6], *_live_secrets()):
        s = s.replace(v, MASK)
    s = _PARAM.sub(lambda m: f"{m.group(1)}={MASK}", s)
    s = _USERINFO.sub(lambda m: f"{m.group(1)}{m.group(2)}:{MASK}@", s)
    s = _TG_BOT.sub(f"/bot{MASK}", s)
    s = _DISCORD.sub(lambda m: f"{m.group(1)}{MASK}", s)
    return s


class RedactingFilter(logging.Filter):
    """Masks credentials in every record that passes through a handler.

    Rewrites the record in place (message pre-formatted, args cleared) so
    handlers and formatters downstream only ever see the redacted text."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:                        # noqa: BLE001 - never drop a log line
            return True
        clean = redact(msg)
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if clean != msg or record.args:
            record.msg, record.args = clean, None
        return True


def install() -> None:
    """Attach the filter to every root handler (idempotent)."""
    root = logging.getLogger()
    for h in root.handlers:
        if not any(isinstance(f, RedactingFilter) for f in h.filters):
            h.addFilter(RedactingFilter())
