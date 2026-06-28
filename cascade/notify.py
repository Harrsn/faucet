"""Notifications — fan out events to Discord, Telegram, ntfy, Gotify, or a
generic webhook. Targets are configured as a comma-separated list of URLs in
NOTIFY_URLS; the scheme/host decides the provider.

Examples:
  discord://<webhook_id>/<token>           (or a raw discord.com/api/webhooks URL)
  telegram://<bot_token>/<chat_id>
  ntfy://ntfy.sh/<topic>                    (https assumed; use ntfys:// for TLS host)
  gotify://<host>/<app_token>
  https://example.com/hook                  (generic JSON POST)

This deliberately mirrors the shorthand style people know from Apprise without
the dependency. Each send is best-effort and never raises into the caller.
"""
from __future__ import annotations

import logging
from urllib.parse import urlparse

import requests

log = logging.getLogger("cascade.notify")

_TIMEOUT = 10


def _discord(url: str, title: str, body: str):
    # accept both discord:// shorthand and full webhook URLs
    if url.startswith("discord://"):
        rest = url[len("discord://"):]
        webhook = f"https://discord.com/api/webhooks/{rest}"
    else:
        webhook = url
    requests.post(webhook, json={"content": f"**{title}**\n{body}"}, timeout=_TIMEOUT)


def _telegram(url: str, title: str, body: str):
    rest = url[len("telegram://"):]
    token, _, chat = rest.partition("/")
    requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                  json={"chat_id": chat, "text": f"{title}\n{body}",
                        "parse_mode": "Markdown"}, timeout=_TIMEOUT)


def _ntfy(url: str, title: str, body: str):
    # ntfy://host/topic  -> https://host/topic ; ntfys:// forces https too
    scheme = "https"
    rest = url.split("://", 1)[1]
    target = f"{scheme}://{rest}"
    requests.post(target, data=body.encode("utf-8"),
                  headers={"Title": title}, timeout=_TIMEOUT)


def _gotify(url: str, title: str, body: str):
    rest = url[len("gotify://"):]
    host, _, token = rest.rpartition("/")
    requests.post(f"https://{host}/message?token={token}",
                  json={"title": title, "message": body, "priority": 5},
                  timeout=_TIMEOUT)


def _generic(url: str, title: str, body: str):
    requests.post(url, json={"title": title, "message": body}, timeout=_TIMEOUT)


def _dispatch(url: str, title: str, body: str):
    if url.startswith("discord://") or "discord.com/api/webhooks" in url:
        _discord(url, title, body)
    elif url.startswith("telegram://"):
        _telegram(url, title, body)
    elif url.startswith(("ntfy://", "ntfys://")):
        _ntfy(url, title, body)
    elif url.startswith("gotify://"):
        _gotify(url, title, body)
    else:
        _generic(url, title, body)


def notify(urls: list[str], title: str, body: str) -> None:
    """Send to every configured target. Best-effort; logs and swallows errors."""
    for url in urls:
        try:
            _dispatch(url, title, body)
        except Exception as e:                       # noqa: BLE001 - never break caller
            log.warning("notify failed for %s: %s", urlparse(url).scheme or "?", e)
