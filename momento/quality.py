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

# A preset is offered only when the recorded picture (the screen, or the picked
# window) is at least that tall, give or take this much: recording a 1080p
# screen at 4K only upscales it, which costs disk space and bitrate and adds
# nothing. "native" always fits.
SOURCE_TOLERANCE = 0.02


def resolution(capture: dict) -> tuple[int, int] | None:
    name = str(capture.get("resolution", DEFAULT_RESOLUTION)).lower()
    if name not in RESOLUTIONS:
        raise ValueError(f"unknown resolution {name!r} (choose: {', '.join(RESOLUTIONS)})")
    return RESOLUTIONS[name]


def source_size(value) -> tuple[int, int] | None:
    """[w, h] / (w, h) with positive integers -> (w, h); anything else -> None (unknown)."""
    try:
        w, h = value
    except (TypeError, ValueError):
        return None
    if any(isinstance(v, bool) or not isinstance(v, int) or v <= 0 for v in (w, h)):
        return None
    return w, h


def fits_source(name: str, source) -> bool:
    """Is preset ``name`` at most as tall as ``source`` (+ SOURCE_TOLERANCE)? Unknown source: yes."""
    size = RESOLUTIONS.get(str(name).lower())
    source = source_size(source)
    if size is None or source is None:
        return True
    return size[1] <= source[1] * (1 + SOURCE_TOLERANCE)


def allowed_resolutions(source) -> list[str]:
    """The resolution choices worth offering for ``source`` (all of them when it is unknown)."""
    return [name for name in RESOLUTIONS if fits_source(name, source)]


def effective_resolution(name: str, source) -> str:
    """What is actually recorded: ``name``, or "native" when ``name`` is taller than the source.

    Momento never upscales: a preset above the source records at the source's own size.
    """
    name = str(name).lower()
    return name if fits_source(name, source) else "native"


def rate_class(source) -> str:
    """The preset whose bitrates suit ``source``: the smallest one at least as tall.

    1920x1080 -> "1080p", 1920x1200 -> "1440p", a 1280x720 window -> "720p";
    taller than 4K (or unknown) -> "native".
    """
    source = source_size(source)
    if source is None:
        return "native"
    for name, size in RESOLUTIONS.items():
        if size is not None and size[1] * (1 + SOURCE_TOLERANCE) >= source[1]:
            return name
    return "native"


def height_label(source) -> str | None:
    """(1920, 1080) -> "1080p" (None when unknown)."""
    source = source_size(source)
    return f"{source[1]}p" if source else None


def fps(capture: dict) -> int:
    value = int(capture.get("fps") or FPS)
    if value not in FPS_CHOICES:
        raise ValueError(f"unsupported frame rate {value} (choose: {', '.join(map(str, FPS_CHOICES))})")
    return value


def bitrate_kbps(capture: dict, source=None) -> int:
    """Explicit bitrate_kbps wins; 0/absent means pick from resolution + quality (+ fps).

    ``source`` is the recorded picture's size, when known. A preset taller than
    it records at the source's size, so it gets the bitrate of the source's
    own class (``rate_class``) instead of the preset's: a 4K setting on a
    1080p screen costs what 1080p does.
    """
    explicit = int(capture.get("bitrate_kbps") or 0)
    if explicit > 0:
        return explicit
    res = str(capture.get("resolution", DEFAULT_RESOLUTION)).lower()
    q = str(capture.get("quality", DEFAULT_QUALITY)).lower()
    if q not in QUALITIES:
        raise ValueError(f"unknown quality {q!r} (choose: {', '.join(QUALITIES)})")
    if res in RESOLUTIONS and effective_resolution(res, source) != res:
        res = rate_class(source)
    mbps = _MBPS.get(res, _MBPS["native"])[QUALITIES.index(q)]
    # Twice the frames needs ~1.5x the bits for the same look (motion between
    # frames is smaller, so each frame costs less).
    if fps(capture) == 120:
        mbps = round(mbps * 1.5)
    return mbps * 1000


def buffer_gb(kbps: int, seconds: int = 3600) -> float:
    """Disk used by a full buffer (video only; audio adds ~70 MB/h)."""
    return kbps * seconds / 8 / 1_000_000
