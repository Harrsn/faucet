"""Behaviour settings ("tunables") editable in Settings → General, Hunting and
Maintenance.

Each was an environment variable read where it's used; that's unchanged — the
code still reads os.environ — but the value now lives in the settings store
(faucet.config), which pushes it into os.environ, so a change in the UI
applies on the next tick without a restart. Defaults here must match the
defaults at the read sites.

Pure data + validation: imported by config.py, so it may not import db.
"""
from __future__ import annotations

# key, section, group, label, kind, default, min, max, unit, help, choices
_SPEC = [
    # ── General ────────────────────────────────────────────────────────────
    ("AIR_DELAY_DAYS", "general", "Episodes", "Start hunting an episode", "int", 1, 0, 30, "days after it airs",
     "Releases usually appear within a day; searching on air day mostly finds nothing.", None),
    ("REMOVE_ON_COMPLETE", "general", "Downloads", "Remove torrents once sorted", "bool", False, None, None, "",
     "Off keeps them seeding in the client; on removes them (files stay in the library).", None),
    ("BIG_DOWNLOAD_GB", "general", "Downloads", "Warn before grabbing anything larger than", "int", 20, 1, 1000, "GB",
     "Manual grabs above this ask for confirmation.", None),
    ("REQUEST_TIMEOUT", "general", "Connections", "Timeout for the client and other services", "int", 30, 5, 300, "s",
     "Searches use their own per-indexer timeout under Hunting.", None),
    ("SEARCH_LIMIT", "general", "Connections", "Results kept per search", "int", 150, 10, 1000, "",
     "", None),

    # ── Hunting ────────────────────────────────────────────────────────────
    ("HUNT_MAX_ACTIVE", "hunting", "How much at once", "Downloads at once", "int", 5, 1, 50, "",
     "The hunter stops grabbing while this many downloads are active.", None),
    ("HUNT_MAX_PER_RUN", "hunting", "How much at once", "Grabs per tick", "int", 3, 1, 50, "",
     "At most this many new downloads each time the scheduler runs (every 30 minutes).", None),
    ("HUNT_MAX_SEARCHES", "hunting", "How much at once", "Searches per tick", "int", 40, 1, 500, "",
     "Each search fans out to every indexer; the rest wait for the next tick.", None),
    ("HUNT_UPGRADES", "hunting", "What to hunt", "Hunt for better copies", "bool", True, None, None, "",
     "Below-target and CAM copies are searched for upgrades. Off hunts only what's missing.", None),
    ("HUNT_RELAX_SEEDERS", "hunting", "Hard-to-find episodes", "Accept thinly seeded releases for long-missing episodes",
     "bool", True, None, None, "",
     "Old episodes often only exist on releases with a seeder or two, below the quality profile's minimum. "
     "Better-seeded releases still win; a grab that never starts is dropped and blocklisted.", None),
    ("HUNT_RELAX_AFTER_DAYS", "hunting", "Hard-to-find episodes", "…once it aired at least", "int", 7, 1, 365, "days ago", "", None),
    ("HUNT_RELAX_AFTER_SEARCHES", "hunting", "Hard-to-find episodes", "…and has been searched", "int", 4, 1, 50,
     "times without a grab", "", None),
    ("HUNT_RELAX_MIN_SEEDERS", "hunting", "Hard-to-find episodes", "…accept down to", "int", 1, 0, 20, "seeders",
     "0 also allows releases nobody seeds right now (they may come back).", None),
    ("WANT_BACKOFF_RECENT_HOURS", "hunting", "Retrying", "Retry new episodes at least every", "int", 6, 1, 168, "h",
     "Searches that find nothing back off 1h, 2h, 4h… up to this for recent episodes.", None),
    ("WANT_BACKOFF_MAX_HOURS", "hunting", "Retrying", "Retry older episodes at least every", "int", 72, 1, 720, "h",
     "The backoff ceiling for everything else (old backlog rarely appears overnight).", None),
    ("WANT_RECENT_DAYS", "hunting", "Retrying", "An episode counts as recent for", "int", 14, 1, 365, "days",
     "Recent episodes are searched first and retried more often.", None),
    ("GRAB_RETRY_HOURS", "hunting", "Stuck downloads", "Re-queue a grab that never arrived after", "int", 48, 1, 720, "h",
     "", None),
    ("STALL_HOURS", "hunting", "Stuck downloads", "A download is stalled after no progress for", "int", 4, 1, 168, "h",
     "", None),
    ("STALL_ACTION", "hunting", "Stuck downloads", "When a download stalls", "choice", "remove", None, None, "",
     "Remove: drop it, blocklist the release and hunt another. Flag: only mark it in Activity.",
     [("remove", "Remove and hunt another release"), ("flag", "Only flag it")]),
    ("HUNT_HEALTHY_SEEDERS", "hunting", "Ranking", "Healthy seeder count", "int", 5, 0, 1000, "",
     "Below this a release is ranked under a healthy one a resolution step lower.", None),
    ("HUNT_TRACKER_MIN_GRABS", "hunting", "Ranking", "Judge a tracker after", "int", 4, 1, 100, "grabs",
     "", None),
    ("HUNT_TRACKER_STALL_RATE", "hunting", "Ranking", "Penalise trackers whose grabs stall this often", "float", 0.5, 0.05, 1, "",
     "0.5 = half its grabs. Penalised, not excluded.", None),
    ("SEARCH_TIMEOUT", "hunting", "Searching", "Per-indexer timeout", "int", 60, 5, 300, "s",
     "A cold Cloudflare challenge takes 10–15s; FlareSolverr solves one at a time.", None),
    ("SEARCH_STRAGGLER_SECONDS", "hunting", "Searching", "Then wait for slow indexers", "int", 10, 0, 120, "s",
     "Once most indexers have answered, the rest get this long.", None),
    ("SEARCH_CONCURRENCY", "hunting", "Searching", "Indexers searched at once", "int", 8, 1, 32, "", "", None),
    ("SERIES_REFRESH_HOURS", "hunting", "Episode lists", "Re-read a show's episodes every", "int", 12, 1, 168, "h",
     "Ended shows are re-read weekly.", None),
    ("SERIES_REFRESH_PER_TICK", "hunting", "Episode lists", "Shows refreshed per tick", "int", 5, 1, 100, "", "", None),
    ("SWEEP_ENABLED", "hunting", "Downloads folder", "Sort finished downloads the hook missed", "bool", True, None, None, "",
     "", None),

    # ── Alerts (Settings → Integrations) ───────────────────────────────────
    ("ALERT_INDEXER", "alerts", "Send an alert when…", "An indexer is down", "bool", True, None, None, "",
     "Every search it was asked failed for the time below.", None),
    ("ALERT_INDEXER_MINUTES", "alerts", "Send an alert when…", "…for", "int", 60, 10, 1440, "minutes", "", None),
    ("ALERT_DISK", "alerts", "Send an alert when…", "A library's disk is nearly full", "bool", True, None, None, "",
     "Checks each library's disk and the downloads folder's.", None),
    ("ALERT_DISK_FREE_PCT", "alerts", "Send an alert when…", "…below", "int", 10, 1, 50, "% free", "", None),
    ("ALERT_CLIENT", "alerts", "Send an alert when…", "The download client stops answering", "bool", True, None, None, "",
     "", None),
    ("ALERT_CLIENT_MINUTES", "alerts", "Send an alert when…", "…for", "int", 10, 2, 240, "minutes", "", None),
    ("ALERT_STALLS", "alerts", "Send an alert when…", "The same episode or movie keeps stalling", "bool", True, None, None, "",
     "Its releases are probably dead.", None),
    ("ALERT_STALL_COUNT", "alerts", "Send an alert when…", "…this many times in a day", "int", 3, 2, 20, "", "", None),
    ("ALERT_CRASH", "alerts", "Send an alert when…", "Faucet restarted unexpectedly", "bool", True, None, None, "",
     "Killed, out of memory, or the host lost power — not a normal deploy or restart.", None),

    # ── Maintenance ────────────────────────────────────────────────────────
    ("SUPERSEDED_ACTION", "maintenance", "Library", "After an upgrade lands", "choice", "move", None, None, "",
     "Move parks the old copy in _superseded/ beside its library (reversible).",
     [("move", "Move the old copy to _superseded/"), ("keep", "Keep both")]),
    ("HISTORY_DAYS", "maintenance", "Retention", "Keep history for", "int", 365, 7, 3650, "days", "", None),
    ("HISTORY_FAILED_DAYS", "maintenance", "Retention", "Keep failed grabs for", "int", 14, 1, 365, "days", "", None),
    ("HUNT_LOG_DAYS", "maintenance", "Retention", "Keep hunt logs for", "int", 14, 1, 365, "days", "", None),
    ("INDEXER_STATS_DAYS", "maintenance", "Retention", "Keep indexer statistics for", "int", 7, 1, 90, "days", "", None),
    ("INDEXER_PROBE_HOURS", "maintenance", "Indexers", "Probe every indexer every", "int", 24, 1, 168, "h", "", None),
]

SPEC = {s[0]: dict(zip(("key", "section", "group", "label", "kind", "default", "min", "max",
                        "unit", "help", "choices"), s, strict=True)) for s in _SPEC}
KEYS = set(SPEC)


def validate(key: str, value) -> str:
    """The canonical string to store for `value`, or ValueError."""
    s = SPEC.get(key)
    if s is None:
        raise ValueError(f"{key}: not a setting")
    v = str(value).strip()
    if s["kind"] == "bool":
        if v.lower() in ("1", "true", "yes", "on"):
            return "1"
        if v.lower() in ("0", "false", "no", "off"):
            return "0"
        raise ValueError(f"{s['label']}: on or off")
    if s["kind"] == "choice":
        allowed = [c[0] for c in s["choices"]]
        if v not in allowed:
            raise ValueError(f"{s['label']}: one of {', '.join(allowed)}")
        return v
    try:
        n = int(v) if s["kind"] == "int" else float(v)
    except ValueError:
        raise ValueError(f"{s['label']}: a number") from None
    if s["min"] is not None and n < s["min"] or s["max"] is not None and n > s["max"]:
        raise ValueError(f"{s['label']}: between {s['min']} and {s['max']}")
    return str(n)


def default_str(key: str) -> str:
    d = SPEC[key]["default"]
    return ("1" if d else "0") if isinstance(d, bool) else str(d)
