"""Central configuration, loaded once from the environment.

Everything tunable lives here so the rest of the app imports `config` rather
than reading os.environ ad hoc. A .env file (see .env.example) is the intended
way to set these in both bare-metal and Docker deployments.

A second, runtime layer is the persisted file at CASCADE_CONFIG_FILE (default
/config/cascade.env). Faucet's Settings panel writes there, and it is loaded *over*
process env so first-run config survives restarts without editing .env by hand.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

_explicit_cfg = os.environ.get("FAUCET_CONFIG_FILE") or os.environ.get("CASCADE_CONFIG_FILE")
CONFIG_FILE = _explicit_cfg or "/config/faucet.env"
# Back-compat: only when no env var was set, prefer an existing legacy cascade.env
if not _explicit_cfg and not os.path.exists(CONFIG_FILE) and os.path.exists("/config/cascade.env"):
    CONFIG_FILE = "/config/cascade.env"


def _load_persisted(into_env: bool = True) -> dict:
    """Read the wizard-written env file. Persisted values win over process env
    for the keys it defines (the wizard is the most recent intent)."""
    p = Path(CONFIG_FILE)
    data = {}
    if not p.exists():
        return data
    try:
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip()
            data[k] = v
            if into_env:
                os.environ[k] = v
    except OSError:
        pass
    return data


# Load persisted config into the environment before the dataclass reads it.
_load_persisted(into_env=True)


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
    app_title: str = field(default_factory=lambda: os.environ.get("APP_TITLE", "Faucet"))

    def configured(self) -> bool:
        """True when the minimum required settings are present."""
        return bool(self.jackett_api_key and self.client_url)


def reload() -> "Config":
    """Re-read persisted file + env and rebuild the module-level config."""
    global config
    _load_persisted(into_env=True)
    config = Config()
    return config


# Keys the settings editor / wizard may persist (whitelist — no arbitrary
# writes, and SESSION_SECRET / DB path / bind port are deliberately excluded as
# structural/secret and must stay in the stack).
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
    "UI_THEME", "APP_TITLE",
}

# Subset that point at filesystem locations — the editor validates these before
# saving (existence / writability inside the container) to avoid silent breakage.
PATH_KEYS = {"LIBRARY_ROOT", "DOWNLOAD_DIR", "DISK_PATH", "BROWSE_ROOT"}


def _check_value(key: str, value: str) -> str:
    """A value that can be written as one KEY=VALUE line. A newline in a value
    used to start a new line in faucet.env, i.e. set an arbitrary variable —
    SESSION_SECRET included — on the next load."""
    if any(ch in value for ch in ("\n", "\r", "\x00")):
        raise ValueError(f"{key}: value must be a single line")
    if len(value) > 4096:
        raise ValueError(f"{key}: value too long")
    return value


def save(values: dict) -> None:
    """Persist whitelisted key/values to CONFIG_FILE, then reload config.
    Merges with anything already persisted so partial saves are fine.

    The file holds CLIENT_PASS and JACKETT_API_KEY, so it is written 0600 and
    atomically (temp file + rename): a crash mid-write can't leave a
    truncated config behind. Raises ValueError for an unwritable value
    before touching the file."""
    import tempfile
    updates = {k: _check_value(k, str(v)) for k, v in values.items()
               if k in WIZARD_KEYS and v is not None}
    existing = _load_persisted(into_env=False)
    existing.update(updates)
    p = Path(CONFIG_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Written by Faucet (Settings). Safe to edit by hand.",
             "# Values here override process environment.", ""]
    for k in sorted(existing):
        lines.append(f"{k}={existing[k]}")
    fd, tmp = tempfile.mkstemp(prefix=".faucet-env-", dir=str(p.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(lines) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    reload()


def harden_permissions() -> None:
    """Tighten an existing config file written by an older version (0644)."""
    try:
        p = Path(CONFIG_FILE)
        if p.exists() and (p.stat().st_mode & 0o077):
            os.chmod(p, 0o600)
    except OSError:
        pass


config = Config()
harden_permissions()
