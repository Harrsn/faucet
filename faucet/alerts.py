"""Alerts: tell the admin when something needs attention — once when it
starts, once when it clears.

Checks (each can be switched off in Settings → Integrations → Alerts):

  indexer     an indexer has failed every search for ALERT_INDEXER_MINUTES
  disk        a library's (or the downloads folder's) disk is below
              ALERT_DISK_FREE_PCT free
  client      the download client hasn't answered for ALERT_CLIENT_MINUTES
  stalls      the same episode/movie stalled ALERT_STALL_COUNT times in 24h
  crash       Faucet started after stopping uncleanly (killed, out of
              memory, host lost power) — sent once, nothing to clear

State lives in `alert_state`, so a condition that persists across checks (or
restarts) isn't re-sent, and a flapping one sends at most one message per
transition. Messages go to the notification targets; active alerts also show
on Home for admins. Runs every few minutes from the scheduler's guard loop.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import time
from datetime import datetime, timedelta

from . import db

log = logging.getLogger("faucet.alerts")

RUNNING_KEY = "process_running"
_STARTED = time.monotonic()


def _on(key: str, default: str = "1") -> bool:
    return os.environ.get(key, default).strip().lower() not in ("0", "false", "no", "off")


def _int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except ValueError:
        return default


def ensure_schema(c) -> None:
    c.execute("CREATE TABLE IF NOT EXISTS alert_state ("
              "  key     TEXT PRIMARY KEY,"
              "  active  INTEGER NOT NULL DEFAULT 0,"
              "  since   TEXT,"
              "  title   TEXT,"
              "  detail  TEXT)")


# ── checks: each returns [(key, active, title, detail)] for what it evaluated ─

def check_indexers() -> list:
    if not _on("ALERT_INDEXER"):
        return []
    minutes = _int("ALERT_INDEXER_MINUTES", 60)
    since = (datetime.now() - timedelta(minutes=minutes)).isoformat(timespec="seconds")
    out = []
    with db.connect() as c:
        try:
            rows = c.execute("SELECT indexer, COUNT(*) AS n, SUM(ok) AS good, "
                             "MAX(CASE WHEN ok=0 THEN error END) AS err "
                             "FROM search_events WHERE ts >= ? GROUP BY indexer", (since,)).fetchall()
        except Exception:                        # noqa: BLE001 - no telemetry table yet
            return []
    for r in rows:
        if not r["indexer"] or r["n"] < 2:
            continue                             # not enough evidence either way
        down = (r["good"] or 0) == 0
        out.append((f"indexer:{r['indexer']}", down, f"Indexer {r['indexer']} is down",
                    f"Every search for the last {minutes} minutes failed"
                    + (f": {r['err'][:160]}" if r["err"] else ".")))
    return out


def check_disks() -> list:
    if not _on("ALERT_DISK"):
        return []
    from . import libraries, sweep
    threshold = _int("ALERT_DISK_FREE_PCT", 10)
    paths = [(lib["name"], lib["path"]) for lib in libraries.all_libraries() if lib.get("enabled", 1)]
    paths.append(("Downloads", str(sweep._complete_dir())))
    seen: dict = {}
    for name, p in paths:
        try:
            dev = os.stat(p).st_dev
            usage = shutil.disk_usage(p)
        except OSError:
            continue
        if dev in seen:
            seen[dev]["names"].append(name)
            continue
        seen[dev] = {"names": [name], "usage": usage, "path": p}
    out = []
    for dev, d in seen.items():
        u = d["usage"]
        pct = 100.0 * u.free / u.total if u.total else 100.0
        names = ", ".join(d["names"])
        out.append((f"disk:{dev}", pct < threshold, f"Low disk space: {names}",
                    f"{pct:.1f}% free ({u.free / 1e9:.0f} GB of {u.total / 1e9:.0f} GB) on the disk "
                    f"holding {names}. Downloads may fail."))
    return out


def check_client() -> list:
    if not _on("ALERT_CLIENT"):
        return []
    from . import transfers
    minutes = _int("ALERT_CLIENT_MINUTES", 10)
    snap = transfers.snapshot()
    if snap["ok"] is None:
        return []
    age = snap["age"]
    if age is None:                              # never answered since Faucet started
        age = time.monotonic() - _STARTED
    down = snap["ok"] is False and age > minutes * 60
    err = (snap["error"] or "").split(" (Caused by")[0][:140]
    return [("client", down, "Download client unreachable",
             f"No answer for over {minutes} minutes" + (f": {err}" if err else "."))]


_SXE = re.compile(r"(?i)\bS(\d{1,2})(?:\s*E(\d{1,3}))?\b")
_YEAR = re.compile(r"\b(19|20)\d{2}\b")


def _stall_key(name: str) -> tuple[str, str]:
    """('family guy s23e04', 'Family Guy S23E04') from a release name."""
    clean = re.sub(r"[._]+", " ", name or "")
    m = _SXE.search(clean) or _YEAR.search(clean)
    head = clean[:m.start()].strip(" -[]()") if m else " ".join(clean.split()[:4])
    code = ""
    if m and m.re is _SXE:
        code = f"S{int(m.group(1)):02d}" + (f"E{int(m.group(2)):02d}" if m.group(2) else "")
    elif m:
        code = m.group(0)
    label = f"{head} {code}".strip()
    return label.lower(), label


def check_stalls() -> list:
    if not _on("ALERT_STALLS"):
        return []
    need = _int("ALERT_STALL_COUNT", 3)
    since = (datetime.now() - timedelta(hours=24)).isoformat(timespec="seconds")
    with db.connect() as c:
        names = [r["title"] for r in c.execute(
            "SELECT title FROM history WHERE event='stalled' AND ts >= ?", (since,))]
    counts: dict = {}
    for n in names:
        key, label = _stall_key(n)
        counts.setdefault(key, [label, 0])[1] += 1
    out = [(f"stalls:{k}", n >= need, f"{label} keeps stalling",
            f"{n} downloads of it stalled in the last 24 hours — its releases may be dead; "
            f"consider a different quality profile or searching by hand.")
           for k, (label, n) in counts.items()]
    # anything that was alerting and has no stalls in the window any more
    with db.connect() as c:
        ensure_schema(c)
        for r in c.execute("SELECT key FROM alert_state WHERE key LIKE 'stalls:%' AND active=1"):
            if r["key"][len("stalls:"):] not in counts:
                out.append((r["key"], False, "", ""))
    return out


# ── state machine + delivery ────────────────────────────────────────────────

def _send(title: str, body: str) -> None:
    from . import config, notify
    urls = config.config.notify_urls
    if urls:
        notify.notify(urls, title, body)


def apply(results: list) -> dict:
    """Record each evaluated condition; notify on transitions only."""
    fired, cleared = [], []
    now = datetime.now().isoformat(timespec="seconds")
    with db.connect() as c:
        ensure_schema(c)
        state = {r["key"]: dict(r) for r in c.execute("SELECT * FROM alert_state")}
        for key, active, title, detail in results:
            prev = state.get(key)
            was = bool(prev and prev["active"])
            if active and not was:
                c.execute("INSERT INTO alert_state (key, active, since, title, detail) VALUES (?,1,?,?,?) "
                          "ON CONFLICT(key) DO UPDATE SET active=1, since=excluded.since, "
                          "title=excluded.title, detail=excluded.detail", (key, now, title, detail))
                fired.append((title, detail))
            elif active:
                c.execute("UPDATE alert_state SET detail=? WHERE key=?", (detail, key))
            elif was:
                c.execute("UPDATE alert_state SET active=0 WHERE key=?", (key,))
                cleared.append((prev["title"], prev["since"]))
    for title, detail in fired:
        log.warning("ALERT: %s — %s", title, detail)
        db.add_history("alert", title, detail)
        _send(f"⚠ {title}", detail)
    for title, since in cleared:
        log.info("Resolved: %s", title)
        db.add_history("alert_resolved", title, f"since {since}")
        _send(f"✓ Resolved: {title}", f"It's been fine since this check (alerting since {since[:16].replace('T', ' ')}).")
    return {"fired": len(fired), "cleared": len(cleared)}


_CHECKS = (("ALERT_INDEXER", "indexer:", "check_indexers"), ("ALERT_DISK", "disk:", "check_disks"),
           ("ALERT_CLIENT", "client", "check_client"), ("ALERT_STALLS", "stalls:", "check_stalls"))


def check() -> dict:
    results = []
    for toggle, prefix, name in _CHECKS:
        if not _on(toggle):
            # switched off: drop its open alerts quietly rather than leave them up
            with db.connect() as c:
                ensure_schema(c)
                c.execute("UPDATE alert_state SET active=0 WHERE active=1 AND key LIKE ?", (prefix + "%",))
            continue
        try:
            results += globals()[name]()
        except Exception as e:                   # noqa: BLE001 - one check never stops the rest
            log.warning("alert check %s failed: %s", name, e)
    return apply(results)


def active() -> list[dict]:
    with db.connect() as c:
        ensure_schema(c)
        return [dict(r) for r in c.execute(
            "SELECT key, since, title, detail FROM alert_state WHERE active=1 ORDER BY since")]


# ── unclean-shutdown detection ──────────────────────────────────────────────

def started() -> bool:
    """Call at startup. True (and alerts, if enabled) when the previous run
    never reached a clean shutdown."""
    unclean = bool(db.get_setting(RUNNING_KEY))
    db.set_setting(RUNNING_KEY, datetime.now().isoformat(timespec="seconds"))
    if unclean and _on("ALERT_CRASH"):
        detail = ("Faucet started again after its previous run ended without shutting down "
                  "(killed, out of memory, or the host lost power). Check `docker logs faucet`.")
        log.warning("ALERT: unclean shutdown detected")
        db.add_history("alert", "Faucet restarted unexpectedly", detail)
        _send("⚠ Faucet restarted unexpectedly", detail)
    return unclean


def stopped() -> None:
    """Call at clean shutdown."""
    db.set_setting(RUNNING_KEY, "")
