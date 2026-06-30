"""Central configuration, loaded once from the environment.

Everything tunable lives here so the rest of the app imports `config` rather
than reading os.environ ad hoc. A .env file (see .env.example) is the intended
way to set these in both bare-metal and Docker deployments.

A second, runtime layer is the persisted file at CASCADE_CONFIG_FILE (default
/config/cascade.env). The setup wizard writes there, and it's loaded *over*
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
    ui_accent: str = field(default_factory=lambda: os.environ.get("UI_ACCENT", "blue"))
    app_title: str = field(default_factory=lambda: os.environ.get("APP_TITLE", "Cascade"))

    def configured(self) -> bool:
        """True when the minimum required settings are present."""
        return bool(self.jackett_api_key and self.client_url)


def reload() -> "Config":
    """Re-read persisted file + env and rebuild the module-level config."""
    global config
    _load_persisted(into_env=True)
    config = Config()
    return config


# Keys the wizard is allowed to persist (whitelist — no arbitrary writes).
WIZARD_KEYS = {
    "JACKETT_URL", "JACKETT_API_KEY", "JACKETT_INDEXER",
    "DOWNLOAD_CLIENT", "CLIENT_URL", "CLIENT_USER", "CLIENT_PASS",
    "LIBRARY_ROOT", "DOWNLOAD_DIR", "DISK_PATH",
    "REMOVE_ON_COMPLETE", "NOTIFY_URLS", "NOTIFY_ON",
    "UI_THEME", "UI_ACCENT", "APP_TITLE",
}


def save(values: dict) -> None:
    """Persist whitelisted key/values to CONFIG_FILE, then reload config.
    Merges with anything already persisted so partial saves are fine."""
    existing = _load_persisted(into_env=False)
    for k, v in values.items():
        if k in WIZARD_KEYS and v is not None:
            existing[k] = str(v)
    p = Path(CONFIG_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Written by the Cascade setup wizard. Safe to edit by hand.",
             "# Values here override process environment.", ""]
    for k in sorted(existing):
        lines.append(f"{k}={existing[k]}")
    p.write_text("\n".join(lines) + "\n")
    reload()


config = Config()
