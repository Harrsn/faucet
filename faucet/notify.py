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

log = logging.getLogger("faucet.notify")

_TIMEOUT = 10


def _discord(url: str, title: str, body: str, image: str | None = None):
    # accept both discord:// shorthand and full webhook URLs
    if url.startswith("discord://"):
        rest = url[len("discord://"):]
        webhook = f"https://discord.com/api/webhooks/{rest}"
    else:
        webhook = url
    # rich embed (with the poster as a thumbnail when we have one) instead of
    # a bare text message
    embed = {"title": title, "description": body, "color": 0xC8A24B}
    if image:
        embed["thumbnail"] = {"url": image}
    r = requests.post(webhook, json={"embeds": [embed]}, timeout=_TIMEOUT)
    r.raise_for_status()


def _telegram(url: str, title: str, body: str):
    rest = url[len("telegram://"):]
    token, _, chat = rest.partition("/")
    # HTML, escaped: Markdown mode rejected any message whose release name had
    # an odd number of '_' or '*' (most of them), so notifications silently
    # failed with HTTP 400
    import html
    text = f"<b>{html.escape(title)}</b>\n{html.escape(body)}"
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": text, "parse_mode": "HTML"},
                      timeout=_TIMEOUT)
    r.raise_for_status()


def _ntfy(url: str, title: str, body: str):
    # ntfy://host/topic  -> https://host/topic ; ntfys:// forces https too
    scheme = "https"
    rest = url.split("://", 1)[1]
    target = f"{scheme}://{rest}"
    # title as a query parameter: HTTP headers are latin-1, so a non-latin-1
    # title (curly quotes, CJK, emoji) raised before anything was sent
    r = requests.post(target, data=body.encode("utf-8"), params={"title": title},
                      timeout=_TIMEOUT)
    r.raise_for_status()


def _gotify(url: str, title: str, body: str):
    rest = url[len("gotify://"):]
    host, _, token = rest.rpartition("/")
    requests.post(f"https://{host}/message?token={token}",
                  json={"title": title, "message": body, "priority": 5},
                  timeout=_TIMEOUT)


def _generic(url: str, title: str, body: str):
    requests.post(url, json={"title": title, "message": body}, timeout=_TIMEOUT)


def _dispatch(url: str, title: str, body: str, image: str | None = None):
    if url.startswith("discord://") or "discord.com/api/webhooks" in url:
        _discord(url, title, body, image)
    elif url.startswith("telegram://"):
        _telegram(url, title, body)
    elif url.startswith(("ntfy://", "ntfys://")):
        _ntfy(url, title, body)
    elif url.startswith("gotify://"):
        _gotify(url, title, body)
    else:
        _generic(url, title, body)


def notify(urls: list[str], title: str, body: str, image: str | None = None) -> None:
    """Send to every configured target. Best-effort; logs and swallows errors.
    `image` (a poster URL) renders as a thumbnail where supported (Discord)."""
    for url in urls:
        try:
            _dispatch(url, title, body, image)
        except Exception as e:                       # noqa: BLE001 - never break caller
            from .redact import redact
            log.warning("notify failed for %s: %s", urlparse(url).scheme or "?", redact(e))
