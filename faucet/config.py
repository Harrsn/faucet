"""Central configuration.

Settings live in the database (the `settings` table, keys prefixed `cfg.`):
the Settings pages and the setup wizard write them, and they are loaded into
the process environment at import so every module keeps reading one place.

The environment is for bootstrap only — where the data lives (EVENTS_FILE),
SESSION_SECRET, and the tunables that have no UI yet. On the first start of a
version with this store, every setting found in the old persisted file
(faucet.env / cascade.env) or in the environment is IMPORTED ONCE into the
database; from then on the database wins and an environment value for a
setting is ignored, with a warning at startup naming it. That removes the
three-layer precedence (stack env < secrets file < faucet.env) that made a
changed compose value silently do nothing.

This module must not import faucet.db (db imports config for the data
path), so it talks to SQLite directly.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("faucet.config")

_explicit_cfg = os.environ.get("FAUCET_CONFIG_FILE") or os.environ.get("CASCADE_CONFIG_FILE")
CONFIG_FILE = _explicit_cfg or "/config/faucet.env"
# Back-compat: only when no env var was set, prefer an existing legacy cascade.env
if not _explicit_cfg and not os.path.exists(CONFIG_FILE) and os.path.exists("/config/cascade.env"):
    CONFIG_FILE = "/config/cascade.env"

# Messages from import time, before the app has configured logging (they'd
# otherwise vanish: Python only prints WARNING+ without a handler). The app
# replays them once logging is up.
pending_messages: list = []

PREFIX = "cfg."
IMPORTED_KEY = "cfg._imported"
_warned: set = set()


def _db_path() -> Path:
    # the same file faucet.db uses (next to the events file)
    return Path(os.environ.get("EVENTS_FILE", "/config/events.jsonl")).parent / "cascade.db"


def _connect() -> sqlite3.Connection:
    p = _db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=10)
    conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    return conn


def _db_read() -> dict:
    """{KEY: value} for every stored setting, plus IMPORTED_KEY if set."""
    try:
        conn = _connect()
        try:
            rows = conn.execute("SELECT key, value FROM settings WHERE key LIKE 'cfg.%'").fetchall()
        finally:
            conn.close()
    except (OSError, sqlite3.Error) as e:
        log.error("settings store unreadable (%s); using the environment", e)
        return {}
    out = {}
    for k, v in rows:
        try:
            out[k] = json.loads(v)
        except (TypeError, ValueError):
            out[k] = v
    return out


def _db_write(values: dict) -> None:
    conn = _connect()
    try:
        with conn:
            for k, v in values.items():
                conn.execute("INSERT INTO settings (key, value) VALUES (?,?) "
                             "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                             (k, json.dumps(v)))
    finally:
        conn.close()


def _read_env_file(path: str) -> dict:
    data = {}
    try:
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            data[k.strip()] = v.strip()
    except OSError:
        pass
    return data


def _import_once(stored: dict) -> dict:
    """First start with the database store: adopt the old settings."""
    found = {k: os.environ[k] for k in WIZARD_KEYS if os.environ.get(k)}
    legacy = Path(CONFIG_FILE)
    from_file = _read_env_file(CONFIG_FILE) if legacy.exists() else {}
    found.update({k: v for k, v in from_file.items() if k in WIZARD_KEYS})   # file won before
    values = {PREFIX + k: v for k, v in found.items()}
    values[IMPORTED_KEY] = {"keys": sorted(found), "from_file": bool(from_file),
                            "tunables": sorted(set(found) & _tunables.KEYS),
                            "tunables_done": True}
    _db_write(values)
    if from_file:
        try:
            legacy.replace(legacy.with_name(legacy.name + ".imported"))
        except OSError as e:
            log.warning("imported %s but could not rename it: %s", legacy, e)
    if found:
        msg = "Settings imported into the database: " + ", ".join(sorted(found))
        log.info(msg)
        pending_messages.append((logging.INFO, msg))
    stored.update(values)
    return stored


def _import_tunables(stored: dict) -> dict:
    """1.7 → 1.8: the behaviour settings joined the store. Adopt any the
    environment sets (the stack's HUNT_MAX_ACTIVE, say) once, like the
    connection settings were."""
    found = {}
    for k in sorted(_tunables.KEYS):
        if PREFIX + k in stored or not os.environ.get(k):
            continue
        try:
            found[k] = _tunables.validate(k, os.environ[k])
        except ValueError as e:
            log.warning("not importing %s: %s", k, e)
    marker = dict(stored.get(IMPORTED_KEY) or {})
    marker["tunables"] = sorted(found)
    marker["tunables_done"] = True
    values = {PREFIX + k: v for k, v in found.items()}
    values[IMPORTED_KEY] = marker
    _db_write(values)
    if found:
        msg = "Behaviour settings imported into the database: " + ", ".join(sorted(found))
        log.info(msg)
        pending_messages.append((logging.INFO, msg))
    stored.update(values)
    return stored


def _load_persisted(into_env: bool = True) -> dict:
    """{KEY: value} of stored settings (bare keys), imported once from the old
    file/environment, and — with into_env — pushed into os.environ so they
    win over anything the container was started with."""
    if not _db_path().exists() and not Path(CONFIG_FILE).exists() \
            and not any(os.environ.get(k) for k in WIZARD_KEYS):
        return {}                     # nothing stored, nothing to import: don't create files
    try:
        stored = _db_read()
        if IMPORTED_KEY not in stored:
            stored = _import_once(stored)
        elif not (stored.get(IMPORTED_KEY) or {}).get("tunables_done"):
            stored = _import_tunables(stored)
    except (OSError, sqlite3.Error) as e:
        # an unwritable/missing data volume must not stop the app (or a CLI
        # tool) from importing config: fall back to the environment as-is
        log.error("settings store unavailable at %s (%s); using the environment",
                  _db_path(), e)
        return {}
    data = {k[len(PREFIX):]: str(v) for k, v in stored.items()
            if k.startswith(PREFIX) and k != IMPORTED_KEY and k[len(PREFIX):] in WIZARD_KEYS}
    if into_env:
        for k, v in data.items():
            env = os.environ.get(k)
            if env is not None and env != v and k not in _warned:
                _warned.add(k)
                log.warning("%s is set in the environment but Faucet reads it from its "
                            "database now; the environment value is ignored. Change it "
                            "in Settings.", k)
            os.environ[k] = v
    return data


def _bool(key: str, default: bool = False) -> bool:
    return os.environ.get(key, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _list(key: str) -> list[str]:
    raw = os.environ.get(key, "").strip()
    return [x.strip() for x in raw.split(",") if x.strip()]


@dataclass
class Config:
    # All fields read env via default_factory so a fresh Config() picks up
    # changes after the wizard writes + reload() runs. (Plain `= os.environ...`
    # defaults are evaluated once at class definition and would never update.)
    # --- indexer (Jackett/Prowlarr Torznab) ---
    jackett_url: str = field(default_factory=lambda: os.environ.get("JACKETT_URL", "http://127.0.0.1:9117").rstrip("/"))
    jackett_api_key: str = field(default_factory=lambda: os.environ.get("JACKETT_API_KEY", ""))
    jackett_indexer: str = field(default_factory=lambda: os.environ.get("JACKETT_INDEXER", "all"))

    # --- download client ---
    client_kind: str = field(default_factory=lambda: os.environ.get("DOWNLOAD_CLIENT", "transmission"))
    client_url: str = field(default_factory=lambda: os.environ.get("CLIENT_URL", "http://127.0.0.1:9091/transmission/rpc"))
    client_user: str = field(default_factory=lambda: os.environ.get("CLIENT_USER", ""))
    client_pass: str = field(default_factory=lambda: os.environ.get("CLIENT_PASS", ""))
    download_dir: str = field(default_factory=lambda: os.environ.get("DOWNLOAD_DIR", ""))

    # --- paths ---
    browse_root: str = field(default_factory=lambda: os.environ.get("BROWSE_ROOT", "")
                             or os.environ.get("LIBRARY_ROOT", "") or "/library")
    disk_path: str = field(default_factory=lambda: os.environ.get("DISK_PATH", "") or
                           os.environ.get("DOWNLOAD_DIR", "") or "/downloads")
    events_file: str = field(default_factory=lambda: os.environ.get("EVENTS_FILE", "/config/events.jsonl"))

    # --- behavior ---
    request_timeout: int = field(default_factory=lambda: int(os.environ.get("REQUEST_TIMEOUT", "30")))
    search_limit: int = field(default_factory=lambda: int(os.environ.get("SEARCH_LIMIT", "150")))
    big_download_gb: int = field(default_factory=lambda: int(os.environ.get("BIG_DOWNLOAD_GB", "20")))

    # --- notifications ---
    notify_urls: list[str] = field(default_factory=lambda: _list("NOTIFY_URLS"))
    notify_on: list[str] = field(default_factory=lambda: _list("NOTIFY_ON") or
                                 ["completed", "sorted", "failed"])

    # --- UI ---
    ui_theme: str = field(default_factory=lambda: os.environ.get("UI_THEME", "dark"))
    # The product name is fixed (it was a setting until 1.7).
    app_title: str = "Faucet"

    def configured(self) -> bool:
        """True when the minimum required settings are present."""
        return bool(self.jackett_api_key and self.client_url)


def reload() -> "Config":
    """Re-read the settings store and rebuild the module-level config."""
    global config
    _load_persisted(into_env=True)
    config = Config()
    return config


# Settings the store holds (whitelist — no arbitrary writes; SESSION_SECRET,
# the data path and the bind port are bootstrap and stay in the stack).
WIZARD_KEYS = {
    # connections
    "JACKETT_URL", "JACKETT_API_KEY", "JACKETT_INDEXER",
    "DOWNLOAD_CLIENT", "CLIENT_URL", "CLIENT_USER", "CLIENT_PASS",
    # paths
    "LIBRARY_ROOT", "DOWNLOAD_DIR", "DISK_PATH", "BROWSE_ROOT",
    # behavior
    "REMOVE_ON_COMPLETE", "REQUEST_TIMEOUT", "SEARCH_LIMIT", "BIG_DOWNLOAD_GB",
    "NOTIFY_URLS", "NOTIFY_ON", "AIR_DELAY_DAYS",
    # metadata / ui
    "UI_THEME",
}

# Behaviour settings (Settings → General / Hunting / Maintenance). The code
# reads them from os.environ where they're used; the store pushes them there.
from . import tunables as _tunables  # noqa: E402 - pure data, no db import
WIZARD_KEYS |= _tunables.KEYS

# Subset that point at filesystem locations — the editor validates these before
# saving (existence / writability inside the container) to avoid silent breakage.
PATH_KEYS = {"LIBRARY_ROOT", "DOWNLOAD_DIR", "DISK_PATH", "BROWSE_ROOT"}


def _check_value(key: str, value: str) -> str:
    """A single-line, bounded value. Settings are pushed into the process
    environment and handed to the sorter subprocess; a newline in one used to
    inject arbitrary variables (SESSION_SECRET included) via faucet.env."""
    if any(ch in value for ch in ("\n", "\r", "\x00")):
        raise ValueError(f"{key}: value must be a single line")
    if len(value) > 4096:
        raise ValueError(f"{key}: value too long")
    return value


def save(values: dict) -> None:
    """Store whitelisted settings and reload. Partial saves are fine. Raises
    ValueError for an unstorable value before anything is written."""
    updates = {k: _check_value(k, str(v)) for k, v in values.items()
               if k in WIZARD_KEYS and v is not None}
    for k in list(updates):
        if k in _tunables.KEYS:
            updates[k] = _tunables.validate(k, updates[k])
    if not updates:
        return
    _db_write({PREFIX + k: v for k, v in updates.items()})
    for k, v in updates.items():
        os.environ[k] = v
    reload()


def stored() -> dict:
    """The stored settings (bare keys) — what the Settings UI edits."""
    return _load_persisted(into_env=False)


def harden_permissions() -> None:
    """The database now holds the client password and API keys: 0600, as the
    old settings file was (and tighten that file too if it still exists)."""
    for p in (_db_path(), Path(CONFIG_FILE)):
        try:
            if p.exists() and (p.stat().st_mode & 0o077):
                os.chmod(p, 0o600)
        except OSError:
            pass


_load_persisted(into_env=True)
config = Config()
harden_permissions()
