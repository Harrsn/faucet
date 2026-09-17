"""Fake-release defenses.

Brand-new episodes attract bait torrents: a single `Show.S01E01.1080p.exe`,
or a magnet whose display name looks like an episode but whose payload is an
installer. Three layers keep those out of the library and off the share:

1. **Name filter** (search + hunter): releases whose title ends in an
   executable/script extension are dropped before anyone can grab them.
2. **Payload check** (`check_transfers`, run every GUARD_INTERVAL_SECONDS):
   once a torrent's metadata arrives, a media torrent whose files are
   executables with no video is PAUSED and flagged for review — never deleted.
   Its release stays in the `grabbed` table (so it is never picked again) and
   the wants it was grabbed for go back to 'wanted', so the hunter tries a
   different release.
3. **Sorter backstop** (faucet/sort.py): anything that finishes before the
   check runs is quarantined with its executables renamed so they can't be
   launched from the share.

Env:
  BLOCK_EXECUTABLE_RELEASES  1 (default) / 0 — the name filter
  GUARD_INTERVAL_SECONDS     payload-check cadence (default 60)
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from pathlib import PurePosixPath

log = logging.getLogger("faucet.safety")

# Windows/macOS/Android executables and script hosts. '.com' is deliberately
# absent: tracker watermarks ("www.site.com") end in it far more often than
# real COM binaries appear.
EXEC_EXTS = {
    ".exe", ".scr", ".bat", ".cmd", ".msi", ".msix", ".appx", ".lnk", ".pif",
    ".js", ".jse", ".vbs", ".vbe", ".wsf", ".hta", ".ps1", ".jar", ".cpl",
    ".reg", ".apk",
}
VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts", ".m2ts",
              ".webm", ".flv"}
BLOCKED_SUFFIX = ".faucet-blocked"

_EXEC_NAME = re.compile(
    r"\.(" + "|".join(e[1:] for e in sorted(EXEC_EXTS)) + r")[\s\])]*$", re.IGNORECASE)
_SAMPLE = re.compile(r"(^|[ ._\-\[(/])sample($|[ ._\-\])/])", re.IGNORECASE)


def _truthy(key: str, default: str) -> bool:
    return os.environ.get(key, default).strip().lower() in ("1", "true", "yes", "on")


def blocking_enabled() -> bool:
    return _truthy("BLOCK_EXECUTABLE_RELEASES", "1")


def is_executable_name(name: str) -> bool:
    """A release title that is itself an executable ('Show.S01E01.1080p.exe')."""
    return bool(_EXEC_NAME.search((name or "").strip()))


def filter_release_names(results: list) -> tuple[list, int]:
    """Drop results whose title is an executable. Returns (kept, hidden)."""
    if not blocking_enabled():
        return list(results), 0
    kept = [r for r in results if not is_executable_name(r.get("title", ""))]
    return kept, len(results) - len(kept)


def payload_problem(files) -> str | None:
    """Why a media payload looks fake, or None.

    `files` is an iterable of objects with a `.name`/`.path` (client
    TransferFile) or plain path strings. Suspicious = at least one executable
    and no real (non-sample) video. A proper release that happens to bundle a
    codec installer next to its .mkv is left alone."""
    execs, videos = [], 0
    for f in files:
        path = f if isinstance(f, str) else (getattr(f, "path", "") or getattr(f, "name", ""))
        p = PurePosixPath(path.replace("\\", "/"))
        ext = p.suffix.lower()
        if ext in EXEC_EXTS:
            execs.append(p.name)
        elif ext in VIDEO_EXTS and not _SAMPLE.search(path):
            videos += 1
    if not execs or videos:
        return None
    shown = ", ".join(execs[:3]) + (f" (+{len(execs) - 3} more)" if len(execs) > 3 else "")
    return f"executable payload with no video: {shown}"


# ── transfer guard ───────────────────────────────────────────────────────────

def _ensure_table(c) -> None:
    c.execute(
        "CREATE TABLE IF NOT EXISTS transfer_checks ("
        "  id         TEXT PRIMARY KEY,"      # client transfer id/hash
        "  name       TEXT,"                  # checked under this name
        "  verdict    TEXT,"                  # ok | suspicious
        "  reason     TEXT,"
        "  checked_ts TEXT"
        ")")


def flags() -> dict[str, dict]:
    """{transfer id: {name, reason, checked_ts}} for flagged transfers."""
    from . import db
    with db.connect() as c:
        _ensure_table(c)
        rows = c.execute("SELECT id, name, reason, checked_ts FROM transfer_checks "
                         "WHERE verdict='suspicious'").fetchall()
    return {r["id"]: dict(r) for r in rows}


def _media_expected(name: str) -> bool:
    """Games and software legitimately ship executables — only media is checked."""
    try:
        from .classify import classify
        return classify(name or "")["type"] != "game"
    except Exception:                            # noqa: BLE001
        return True


def _notify(title: str, body: str) -> None:
    from . import config as cfgmod
    c = cfgmod.config
    if not c.notify_urls or not ({"failed", "suspicious"} & set(c.notify_on)):
        return
    try:
        from .notify import notify
        notify(c.notify_urls, title, body)
    except Exception as e:                       # noqa: BLE001
        log.warning("notification failed: %s", e)


def check_transfers(client=None) -> dict:
    """Inspect every transfer whose metadata has arrived, once.

    A transfer is checked a single time per (id, name): once an admin resumes
    a flagged torrent, the guard leaves it alone. Transfers without metadata
    yet (bare magnets) are retried on the next pass."""
    from . import config as cfgmod
    from . import db
    from .clients import make_client
    result = {"checked": 0, "flagged": [], "errors": []}
    try:
        if client is None:
            c = cfgmod.config
            client = make_client(c.client_kind, c.client_url, c.client_user,
                                 c.client_pass, c.request_timeout)
        transfers = client.list_transfers()
    except Exception as e:                       # noqa: BLE001
        result["errors"].append(f"client unreachable: {e}")
        return result

    with db.connect() as c:
        _ensure_table(c)
        seen = {r["id"]: r["name"] for r in
                c.execute("SELECT id, name FROM transfer_checks").fetchall()}
        live = {str(t.id) for t in transfers}
        for tid in set(seen) - live:            # client ids get reused after restarts
            c.execute("DELETE FROM transfer_checks WHERE id=?", (tid,))

    for t in transfers:
        tid = str(t.id)
        if seen.get(tid) == t.name:
            continue
        try:
            files = client.files(t.id)
        except Exception as e:                   # noqa: BLE001
            result["errors"].append(f"files() failed for {t.name}: {e}")
            continue
        if not files:
            continue                             # metadata not fetched yet
        result["checked"] += 1
        reason = payload_problem(files) if _media_expected(t.name) else None
        now = datetime.now().isoformat(timespec="seconds")
        verdict = "suspicious" if reason else "ok"
        if reason:
            try:
                client.pause(t.id)
            except Exception as e:               # noqa: BLE001
                # don't record a verdict: retry the pause next pass
                result["errors"].append(f"pause failed for {t.name}: {e}")
                log.error("SUSPICIOUS but could not pause '%s': %s", t.name, e)
                continue
        with db.connect() as c:
            c.execute(
                "INSERT INTO transfer_checks (id, name, verdict, reason, checked_ts) "
                "VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name, "
                "verdict=excluded.verdict, reason=excluded.reason, "
                "checked_ts=excluded.checked_ts",
                (tid, t.name, verdict, reason, now))
        if not reason:
            continue
        from .stalls import _flip_wants_for_release
        flipped = _flip_wants_for_release(t.name or "")
        db.add_history("suspicious", t.name, f"paused for review — {reason}; "
                                             f"{flipped} want(s) re-queued")
        log.warning("SUSPICIOUS: paused '%s' (%s); re-queued %d want(s)",
                    t.name, reason, flipped)
        _notify("Suspicious download paused", f"{t.name} — {reason}")
        result["flagged"].append({"id": tid, "name": t.name, "reason": reason,
                                  "flipped": flipped})
    return result


def neutralize(root) -> list:
    """Rename executables under `root` (a dir or single file) to
    '<name>.faucet-blocked' so they can't be double-clicked from an SMB share.
    Returns the renamed paths. Best effort: failures are logged, not raised."""
    from pathlib import Path
    root = Path(root)
    targets = [root] if root.is_file() else [p for p in root.rglob("*") if p.is_file()]
    renamed = []
    for p in targets:
        if p.suffix.lower() not in EXEC_EXTS:
            continue
        dest = p.with_name(p.name + BLOCKED_SUFFIX)
        try:
            p.rename(dest)
            renamed.append(dest)
        except OSError as e:
            log.warning("could not neutralize %s: %s", p, e)
    return renamed
