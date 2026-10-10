"""Which mount a path lives on — to tell whether moving a finished download
into a library is a rename or a copy.

rename(2) only works within one mount *as the process sees it*: two Docker
bind mounts of the same NAS share are still two mounts, so a move between
/downloads and /library falls back to copying the whole file over the
network (and stalls Transmission while it runs). Comparing st_dev isn't
enough for that reason — bind mounts of one filesystem share a device — so
this reads /proc/self/mountinfo and finds the mount each path is under.
"""
from __future__ import annotations

import os

MOUNTINFO = "/proc/self/mountinfo"


def _unescape(s: str) -> str:
    # mountinfo escapes space, tab, newline and backslash as \\ooo
    out, i = [], 0
    while i < len(s):
        if s[i] == "\\" and i + 3 < len(s) and s[i + 1:i + 4].isdigit():
            out.append(chr(int(s[i + 1:i + 4], 8)))
            i += 4
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def _mounts(text: str | None = None) -> list[dict]:
    if text is None:
        try:
            with open(MOUNTINFO) as f:
                text = f.read()
        except OSError:
            return []
    out = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 10 or "-" not in parts:
            continue
        sep = parts.index("-")
        out.append({"id": parts[0], "root": _unescape(parts[3]),
                    "point": _unescape(parts[4]),
                    "fstype": parts[sep + 1], "source": _unescape(parts[sep + 2])})
    return out


def mount_of(path: str, text: str | None = None) -> dict | None:
    """The mount `path` is under (deepest mount point that contains it)."""
    try:
        p = os.path.realpath(path)
    except OSError:
        return None
    best = None
    for m in _mounts(text):
        mp = m["point"].rstrip("/") or "/"
        if p == mp or p.startswith(mp.rstrip("/") + "/") or mp == "/":
            if best is None or len(mp) > len(best["point"].rstrip("/") or "/"):
                best = m
    return best


def same_mount(a: str, b: str, text: str | None = None) -> bool | None:
    """True if a move from a to b can be a rename; None when unknown."""
    ma, mb = mount_of(a, text), mount_of(b, text)
    if ma is None or mb is None:
        return None
    return ma["id"] == mb["id"]
