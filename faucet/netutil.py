"""Who is actually on the other end of a request, and how hard they're knocking.

Faucet usually sits behind a reverse proxy (Cloudflare Tunnel -> Caddy ->
docker-proxy), so the TCP peer is the proxy, not the browser. Forwarding
headers are only believed when the peer is a configured trusted proxy;
otherwise any LAN client could claim to be anyone and walk straight past the
login throttle.

Env:
  TRUSTED_PROXIES   comma-separated IPs/CIDRs whose forwarding headers are
                    believed, or 'auto' = the container's default gateway
                    (where Docker's port proxy connects from). Default: none.
  CLIENT_IP_HEADERS comma-separated, checked in order (default
                    'cf-connecting-ip,x-real-ip,x-forwarded-for'; for
                    X-Forwarded-For the right-most address is used).
  COOKIE_SECURE     'auto' (default: from the request scheme / trusted proxy
                    headers), '1' always, '0' never.
  LOGIN_MAX_FAILED      failed logins per (IP, username) before that pair is
                        throttled (default 5)
  LOGIN_IP_MAX_FAILED   failed logins per IP, any username (default 20)
  LOGIN_LOCK_MINUTES    the window, and how long a throttle lasts (default 15)
  REGISTER_IP_MAX       registrations per IP per hour (default 5)
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import threading
import time
from collections import deque

log = logging.getLogger("faucet.net")

_DEFAULT_HEADERS = "cf-connecting-ip,x-real-ip,x-forwarded-for"


def _default_gateway() -> str | None:
    """The IPv4 default gateway from /proc/net/route (Linux), or None."""
    try:
        with open("/proc/net/route") as f:
            next(f, None)
            for line in f:
                parts = line.split()
                if len(parts) >= 3 and parts[1] == "00000000":
                    gw = int(parts[2], 16)
                    return str(ipaddress.IPv4Address(gw.to_bytes(4, "little")))
    except (OSError, ValueError):
        pass
    return None


def trusted_networks() -> list:
    raw = os.environ.get("TRUSTED_PROXIES", "").strip()
    nets = []
    for item in (x.strip() for x in raw.split(",")):
        if not item:
            continue
        if item.lower() == "auto":
            item = _default_gateway() or ""
            if not item:
                continue
        try:
            nets.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            log.warning("TRUSTED_PROXIES: ignoring invalid entry %r", item)
    return nets


def _ip(value: str | None):
    try:
        return ipaddress.ip_address((value or "").strip())
    except ValueError:
        return None


def _peer(request) -> str:
    return request.client.host if getattr(request, "client", None) else ""


def peer_trusted(request) -> bool:
    ip = _ip(_peer(request))
    return bool(ip) and any(ip in n for n in trusted_networks())


def client_ip(request) -> str:
    """Best-effort real client address."""
    peer = _peer(request)
    if not peer_trusted(request):
        return peer
    names = os.environ.get("CLIENT_IP_HEADERS", _DEFAULT_HEADERS)
    for name in (n.strip().lower() for n in names.split(",") if n.strip()):
        val = request.headers.get(name)
        if not val:
            continue
        candidates = [v for v in val.split(",") if v.strip()]
        if name == "x-forwarded-for":
            candidates = candidates[-1:]          # the hop our proxy added
        for c in candidates:
            if _ip(c):
                return str(_ip(c))
    return peer


def is_https(request) -> bool:
    """Was the browser's connection HTTPS? Proxy headers count only from a
    trusted proxy. Cf-Visitor is checked first: Caddy rewrites
    X-Forwarded-Proto to its own (plain-HTTP) scheme behind the tunnel."""
    mode = os.environ.get("COOKIE_SECURE", "auto").strip().lower()
    if mode in ("1", "true", "yes", "on"):
        return True
    if mode in ("0", "false", "no", "off"):
        return False
    if request.url.scheme == "https":
        return True
    if not peer_trusted(request):
        return False
    try:
        if json.loads(request.headers.get("cf-visitor") or "{}").get("scheme") == "https":
            return True
    except (ValueError, AttributeError):
        pass
    return request.headers.get("x-forwarded-proto", "").lower() == "https"


# ── throttling ──────────────────────────────────────────────────────────────

def _int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except ValueError:
        return default


class Throttle:
    """Sliding-window failure counter keyed by arbitrary strings. In-process
    (Faucet runs a single worker); state resets on restart, which is fine for
    blunting online guessing."""

    def __init__(self):
        self._hits: dict[str, deque] = {}
        self._lock = threading.Lock()

    def _prune(self, key: str, window: float, now: float) -> deque:
        q = self._hits.get(key)
        if q is None:
            q = self._hits[key] = deque()
        while q and q[0] <= now - window:
            q.popleft()
        return q

    def blocked_for(self, key: str, limit: int, window: float) -> float:
        """Seconds until `key` may try again (0 = not blocked)."""
        if limit <= 0:
            return 0.0
        now = time.monotonic()
        with self._lock:
            q = self._prune(key, window, now)
            if len(q) < limit:
                return 0.0
            return max(0.0, q[-limit] + window - now)

    def hit(self, key: str, window: float) -> None:
        now = time.monotonic()
        with self._lock:
            self._prune(key, window, now).append(now)
            if len(self._hits) > 50_000:              # bound memory under a spray
                for k in [k for k, v in self._hits.items() if not v][:25_000]:
                    del self._hits[k]

    def clear(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)


_throttle = Throttle()


def _window() -> float:
    return max(1, _int("LOGIN_LOCK_MINUTES", 15)) * 60.0


def login_blocked(ip: str, username: str) -> float:
    """Seconds this login attempt must wait, 0 if allowed. Checked BEFORE the
    password, and identical for real and unknown usernames, so it neither
    leaks which accounts exist nor lets a stranger lock the admin out from
    their own IP (the per-pair key includes the IP)."""
    w = _window()
    user = (username or "").strip().lower()
    return max(_throttle.blocked_for(f"ip:{ip}", _int("LOGIN_IP_MAX_FAILED", 20), w),
               _throttle.blocked_for(f"pair:{ip}:{user}", _int("LOGIN_MAX_FAILED", 5), w))


def login_failed(ip: str, username: str) -> None:
    w = _window()
    user = (username or "").strip().lower()
    _throttle.hit(f"ip:{ip}", w)
    _throttle.hit(f"pair:{ip}:{user}", w)


def login_succeeded(ip: str, username: str) -> None:
    _throttle.clear(f"pair:{ip}:{(username or '').strip().lower()}")


def register_blocked(ip: str) -> float:
    return _throttle.blocked_for(f"reg:{ip}", _int("REGISTER_IP_MAX", 5), 3600.0)


def register_hit(ip: str) -> None:
    _throttle.hit(f"reg:{ip}", 3600.0)


def reset_state() -> None:
    """Tests only."""
    global _throttle
    _throttle = Throttle()
