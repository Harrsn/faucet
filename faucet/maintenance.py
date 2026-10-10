"""Housekeeping the scheduler runs, and the database backup download.

Env:
  EVENTS_MAX_BYTES     rotate events.jsonl past this size (default 5 MiB;
                       one previous generation kept as events.jsonl.1)
  DB_VACUUM_FREE_PCT   VACUUM when free pages exceed this share of the file
                       (default 25), at most once a day
  HISTORY_DAYS         history retention (default 365)
  HISTORY_FAILED_DAYS  retention for grab_failed / remove_failed (default 14)
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from . import db

log = logging.getLogger("faucet.maintenance")

# never leave a backup: live sessions and reset tokens are bearer credentials
_STRIP_TABLES = ("sessions", "reset_tokens")


# ── database backup ─────────────────────────────────────────────────────────

def backup_db() -> Path:
    """A consistent copy of the database in a temp file, made with SQLite's
    online backup API (safe while the app is writing), with session and
    reset tokens removed and the file vacuumed. Caller deletes the file."""
    fd, tmp = tempfile.mkstemp(prefix="faucet-backup-", suffix=".db")
    os.close(fd)
    src = sqlite3.connect(str(db._db_path()), timeout=30)
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)
        for t in _STRIP_TABLES:
            try:
                dst.execute(f"DELETE FROM {t}")
            except sqlite3.Error:
                pass
        # the settings-table TMDb cache is re-fetchable and most of the bulk
        dst.execute("DELETE FROM settings WHERE key LIKE '\\_tmdbcache:%' ESCAPE '\\'")
        dst.commit()
        dst.execute("VACUUM")
    except BaseException:
        dst.close()
        src.close()
        os.unlink(tmp)
        raise
    dst.close()
    src.close()
    return Path(tmp)


def backup_filename() -> str:
    return f"faucet-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"


# ── database hygiene ────────────────────────────────────────────────────────

def prune_tmdb_cache() -> int:
    """Drop expired TMDb cache rows. They were written on every search,
    season fetch and detail view and never removed."""
    now = time.time()
    n = 0
    with db.connect() as c:
        rows = c.execute("SELECT key, value FROM settings "
                         "WHERE key LIKE '\\_tmdbcache:%' ESCAPE '\\'").fetchall()
        for r in rows:
            try:
                exp = float(json.loads(r["value"]).get("exp", 0))
            except (ValueError, TypeError, AttributeError):
                exp = 0
            if exp < now:
                c.execute("DELETE FROM settings WHERE key=?", (r["key"],))
                n += 1
    return n


def prune_history() -> int:
    """History retention. Ordinary events are kept HISTORY_DAYS (365);
    failure noise (grab_failed, remove_failed) HISTORY_FAILED_DAYS (14) —
    one bug once wrote 38k grab_failed rows, 50 MB, in a month."""
    from datetime import timedelta
    try:
        keep = max(1, int(os.environ.get("HISTORY_DAYS", "365")))
        keep_failed = max(1, int(os.environ.get("HISTORY_FAILED_DAYS", "14")))
    except ValueError:
        keep, keep_failed = 365, 14
    now = datetime.now()
    with db.connect() as c:
        n = c.execute("DELETE FROM history WHERE ts < ?",
                      ((now - timedelta(days=keep)).isoformat(timespec="seconds"),)).rowcount
        n += c.execute("DELETE FROM history WHERE event IN ('grab_failed','remove_failed') "
                       "AND ts < ?",
                       ((now - timedelta(days=keep_failed)).isoformat(timespec="seconds"),)
                       ).rowcount
    if n:
        log.info("History retention: removed %d row(s)", n)
    return n


def db_stats() -> dict:
    """File size, free-page share, and the largest tables (when the dbstat
    virtual table is compiled in, which it is in CPython's SQLite)."""
    p = db._db_path()
    out = {"bytes": p.stat().st_size if p.exists() else 0, "tables": []}
    with db.connect() as c:
        pages = c.execute("PRAGMA page_count").fetchone()[0]
        free = c.execute("PRAGMA freelist_count").fetchone()[0]
        out["free_pct"] = round(100.0 * free / pages, 1) if pages else 0.0
        try:
            out["tables"] = [dict(r) for r in c.execute(
                "SELECT name, SUM(pgsize) AS bytes FROM dbstat GROUP BY name "
                "ORDER BY bytes DESC LIMIT 10")]
        except sqlite3.Error:
            pass
    return out


def maybe_vacuum(force: bool = False) -> bool:
    """VACUUM when the file is mostly free pages (after a large prune), at
    most once a day. Returns True if it ran."""
    try:
        pct = float(os.environ.get("DB_VACUUM_FREE_PCT", "25"))
    except ValueError:
        pct = 25.0
    last = db.get_setting("_last_vacuum", 0) or 0
    if not force and time.time() - float(last) < 86400:
        return False
    stats = db_stats()
    if not force and stats.get("free_pct", 0) < pct:
        return False
    before = stats["bytes"]
    conn = sqlite3.connect(str(db._db_path()), timeout=60)
    try:
        conn.execute("VACUUM")
        conn.execute("PRAGMA optimize")
    finally:
        conn.close()
    db.set_setting("_last_vacuum", time.time())
    log.info("VACUUM: %d -> %d bytes", before, db._db_path().stat().st_size)
    return True


# ── events.jsonl ────────────────────────────────────────────────────────────

def _events_path() -> Path:
    from . import config as cfgmod
    return Path(cfgmod.config.events_file)


def rotate_events() -> bool:
    """Rotate events.jsonl to events.jsonl.1 past EVENTS_MAX_BYTES."""
    try:
        limit = int(os.environ.get("EVENTS_MAX_BYTES", str(5 * 1024 * 1024)))
    except ValueError:
        limit = 5 * 1024 * 1024
    p = _events_path()
    try:
        if limit <= 0 or not p.exists() or p.stat().st_size <= limit:
            return False
        os.replace(p, p.with_name(p.name + ".1"))
        return True
    except OSError as e:
        log.warning("events rotation failed: %s", e)
        return False


def tail_events(limit: int) -> list[dict]:
    """The last `limit` events, newest first, reading only the end of the
    file (it was read whole on every Activity poll)."""
    p = _events_path()
    if not p.exists():
        return []
    chunk = max(64 * 1024, limit * 1024)
    try:
        with open(p, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - chunk))
            data = f.read().decode("utf-8", errors="ignore")
    except OSError:
        return []
    lines = data.splitlines()
    if size > chunk and lines:
        lines = lines[1:]                        # first line is probably cut
    out: deque = deque(maxlen=limit)
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            out.append({"ts": "", "event": "raw", "msg": line})
    return list(reversed(out))


def run() -> dict:
    """Scheduler housekeeping. Never raises."""
    out = {}
    for name, fn in (("tmdb_pruned", prune_tmdb_cache), ("history_pruned", prune_history),
                     ("events_rotated", rotate_events), ("vacuumed", maybe_vacuum)):
        try:
            out[name] = fn()
        except Exception as e:                   # noqa: BLE001
            log.warning("maintenance %s failed: %s", name, e)
    return out
