"""Video quality presets. Recording is always 60 fps."""

FPS = 60

# name -> output size; None = keep the screen's own size.
RESOLUTIONS: dict[str, tuple[int, int] | None] = {
    "720p": (1280, 720),
    "1080p": (1920, 1080),
    "1440p": (2560, 1440),
    "2160p": (3840, 2160),
    "native": None,
}

QUALITIES = ("standard", "high", "ultra")

# H.264 bitrate in Mbps at 60 fps, per resolution: standard / high / ultra.
_MBPS = {
    "720p": (6, 10, 15),
    "1080p": (10, 15, 25),
    "1440p": (16, 24, 40),
    "2160p": (30, 45, 70),
    "native": (16, 24, 40),
}

DEFAULT_RESOLUTION = "1080p"
DEFAULT_QUALITY = "high"


def resolution(capture: dict) -> tuple[int, int] | None:
    name = str(capture.get("resolution", DEFAULT_RESOLUTION)).lower()
    if name not in RESOLUTIONS:
        raise ValueError(f"unknown resolution {name!r} (choose: {', '.join(RESOLUTIONS)})")
    return RESOLUTIONS[name]


def bitrate_kbps(capture: dict) -> int:
    """Explicit bitrate_kbps wins; 0/absent means pick from resolution + quality."""
    explicit = int(capture.get("bitrate_kbps") or 0)
    if explicit > 0:
        return explicit
    res = str(capture.get("resolution", DEFAULT_RESOLUTION)).lower()
    q = str(capture.get("quality", DEFAULT_QUALITY)).lower()
    if q not in QUALITIES:
        raise ValueError(f"unknown quality {q!r} (choose: {', '.join(QUALITIES)})")
    return _MBPS.get(res, _MBPS["native"])[QUALITIES.index(q)] * 1000


def buffer_gb(kbps: int, seconds: int = 3600) -> float:
    """Disk used by a full buffer (video only; audio adds ~70 MB/h)."""
    return kbps * seconds / 8 / 1_000_000
