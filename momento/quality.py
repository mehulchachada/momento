"""Video quality presets: resolution, quality level and frame rate (60 or 120 fps)."""

FPS = 60  # default
FPS_CHOICES = (60, 120)

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


def fps(capture: dict) -> int:
    value = int(capture.get("fps") or FPS)
    if value not in FPS_CHOICES:
        raise ValueError(f"unsupported frame rate {value} (choose: {', '.join(map(str, FPS_CHOICES))})")
    return value


def bitrate_kbps(capture: dict) -> int:
    """Explicit bitrate_kbps wins; 0/absent means pick from resolution + quality (+ fps)."""
    explicit = int(capture.get("bitrate_kbps") or 0)
    if explicit > 0:
        return explicit
    res = str(capture.get("resolution", DEFAULT_RESOLUTION)).lower()
    q = str(capture.get("quality", DEFAULT_QUALITY)).lower()
    if q not in QUALITIES:
        raise ValueError(f"unknown quality {q!r} (choose: {', '.join(QUALITIES)})")
    mbps = _MBPS.get(res, _MBPS["native"])[QUALITIES.index(q)]
    # Twice the frames needs ~1.5x the bits for the same look (motion between
    # frames is smaller, so each frame costs less).
    if fps(capture) == 120:
        mbps = round(mbps * 1.5)
    return mbps * 1000


def buffer_gb(kbps: int, seconds: int = 3600) -> float:
    """Disk used by a full buffer (video only; audio adds ~70 MB/h)."""
    return kbps * seconds / 8 / 1_000_000
