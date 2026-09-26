"""Clip length presets."""

import re

# (seconds, short label) — the options shown in the overlay, in order.
PRESETS = [
    (15, "15s"),
    (30, "30s"),
    (60, "1m"),
    (180, "3m"),
    (300, "5m"),
    (900, "15m"),
    (1800, "30m"),
    (3600, "60m"),
]

MAX_SECONDS = 3600

_UNITS = {"s": 1, "sec": 1, "m": 60, "min": 60, "h": 3600, "hr": 3600}


def parse(text: str) -> int:
    """Parse '15s', '5m', '1h', '90' (seconds) into seconds."""
    m = re.fullmatch(r"\s*(\d+)\s*([a-z]*)\s*", text.lower())
    if not m:
        raise ValueError(f"not a duration: {text!r}")
    value, unit = int(m.group(1)), m.group(2) or "s"
    if unit not in _UNITS:
        raise ValueError(f"unknown unit {unit!r} in {text!r}")
    seconds = value * _UNITS[unit]
    if not 1 <= seconds <= MAX_SECONDS:
        raise ValueError(f"duration must be between 1s and {MAX_SECONDS // 60}m")
    return seconds


def label(seconds: float) -> str:
    """Human label: 15s, 1m, 3m20s."""
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    return f"{m}m" if s == 0 else f"{m}m{s:02d}s"


def clock(seconds: float) -> str:
    """mm:ss or h:mm:ss."""
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
