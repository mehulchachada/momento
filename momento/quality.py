"""Video quality presets: resolution, quality level and frame rate (60 or 120 fps)."""

FPS = 60  # default
FPS_CHOICES = (60, 120)

# Every preset Momento knows: name -> output size; None = keep the picture's own size.
PRESETS: dict[str, tuple[int, int] | None] = {
    "720p": (1280, 720),
    "1080p": (1920, 1080),
    "1440p": (2560, 1440),
    "2160p": (3840, 2160),
    "native": None,
}

# The tallest recording Momento makes (v1.0.0: 1080p). Presets taller than this
# are not offered, and "native" is scaled down to fit this many lines. To offer
# 1440p (and 4K) again, raise it to 1440 (2160); nothing else needs to change.
MAX_HEIGHT = 1080

# The presets offered (settings, the bar, `momento set`), in display order.
RESOLUTIONS: dict[str, tuple[int, int] | None] = {
    name: size for name, size in PRESETS.items() if size is None or size[1] <= MAX_HEIGHT}
# Known presets that are not offered yet ("1440p", "2160p"): refused when set,
# recorded as the tallest offered one (TALLEST) when an older config has them.
LATER = tuple(name for name in PRESETS if name not in RESOLUTIONS)
TALLEST = max((n for n, s in RESOLUTIONS.items() if s), key=lambda n: RESOLUTIONS[n][1])
# How the presets are called in words ("2160p" is "4K").
LABELS = {"720p": "720p", "1080p": "1080p", "1440p": "1440p", "2160p": "4K", "native": "Native"}
# Other names people use for the presets (`momento set resolution 4k`, a config file).
ALIASES = {"4k": "2160p", "uhd": "2160p", "2k": "1440p", "qhd": "1440p", "fhd": "1080p", "hd": "720p"}

QUALITIES = ("standard", "high", "ultra")

# H.264 bitrate in Mbps at 60 fps, per preset: standard / high / ultra. A picture
# of another size (native, or a preset above the source) gets the row of the
# smallest preset at least as tall (rate_class).
_MBPS = {
    "720p": (6, 10, 15),
    "1080p": (10, 15, 25),
    "1440p": (16, 24, 40),
    "2160p": (30, 45, 70),
}

DEFAULT_RESOLUTION = "1080p"
DEFAULT_QUALITY = "high"

# A preset is offered only when the recorded picture (the screen, or the picked
# window) is at least that tall, give or take this much: recording a 720p
# window at 1080p only upscales it, which costs disk space and bitrate and adds
# nothing. "native" always fits.
SOURCE_TOLERANCE = 0.02


def later_message() -> str:
    """The refusal for a preset that is not offered yet."""
    names = [LABELS.get(n, n) for n in LATER]
    what = " and ".join(names) + (" aren't" if len(names) > 1 else " isn't")
    return f"{what} available yet; Momento records up to {TALLEST} for now."


def _known(name) -> str:
    """A resolution name, lowercased, aliases resolved (may be unknown)."""
    name = str(name).strip().lower()
    return ALIASES.get(name, name)


def preset(name) -> str:
    """A configured resolution -> the offered preset really used.

    A known preset that is not offered yet ("2160p", "4k", "1440p" in an older
    config) records as TALLEST ("1080p"); the config file is left as it is.
    Unknown names raise ValueError.
    """
    name = _known(name)
    if name in RESOLUTIONS:
        return name
    if name in PRESETS:
        return TALLEST
    raise ValueError(f"unknown resolution {name!r} (choose: {', '.join(RESOLUTIONS)})")


def offered(name) -> str:
    """Like ``preset``, but an unknown name comes back lowercased instead of raising."""
    try:
        return preset(name)
    except ValueError:
        return str(name).strip().lower()


def configured(capture: dict) -> str:
    """The preset ``capture`` records at (see ``preset``); raises ValueError when unknown."""
    return preset(capture.get("resolution", DEFAULT_RESOLUTION))


def resolution(capture: dict) -> tuple[int, int] | None:
    return RESOLUTIONS[configured(capture)]


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
    size = RESOLUTIONS.get(offered(name))
    source = source_size(source)
    if size is None or source is None:
        return True
    return size[1] <= source[1] * (1 + SOURCE_TOLERANCE)


def allowed_resolutions(source) -> list[str]:
    """The resolution choices worth offering for ``source`` (all of them when it is unknown)."""
    return [name for name in RESOLUTIONS if fits_source(name, source)]


def effective_resolution(name: str, source) -> str:
    """What is actually recorded: the preset ``name`` stands for, or "native" when
    that is taller than the source.

    Momento never upscales: a preset above the source records at the source's
    own size. A preset not offered yet counts as TALLEST ("2160p" -> "1080p").
    """
    name = offered(name)
    return name if fits_source(name, source) else "native"


def native_size(source) -> tuple[int, int] | None:
    """The size "native" records ``source`` at: its own size, scaled down to at most
    MAX_HEIGHT lines (aspect kept), in even numbers (H.264). None when unknown.

    1920x1080 -> 1920x1080, 3840x2160 -> 1920x1080, 3440x1440 -> 2580x1080,
    a 1271x713 window -> 1270x712.
    """
    source = source_size(source)
    if source is None:
        return None
    w, h = source
    if h > MAX_HEIGHT:
        w, h = round(w * MAX_HEIGHT / h), MAX_HEIGHT
    return max(2, w - w % 2), max(2, h - h % 2)


def recorded_size(name: str, source) -> tuple[int, int] | None:
    """The picture size really recorded with resolution ``name`` on ``source``: the
    preset's size, or ``native_size`` for native and a preset above the source.
    None for native while the source is unknown."""
    name = effective_resolution(name, source)
    size = RESOLUTIONS.get(name)
    return size if size is not None else native_size(source)


def rate_class(source) -> str:
    """The preset whose bitrates suit a picture of size ``source``: the smallest
    one at least as tall.

    1920x1080 -> "1080p", 1920x1200 -> "1440p", a 1280x720 window -> "720p";
    taller than every preset -> the tallest ("2160p"). Unknown -> TALLEST, the
    tallest offered one ("1080p"): native never records taller than that.
    """
    source = source_size(source)
    if source is None:
        return TALLEST
    for name, size in PRESETS.items():
        if size is not None and size[1] * (1 + SOURCE_TOLERANCE) >= source[1]:
            return name
    return max(_MBPS, key=lambda n: PRESETS[n][1])


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

    ``source`` is the recorded picture's size, when known. The bitrate suits the
    size really recorded (``recorded_size``): "native" and a preset taller than
    the source get the bitrate of that size's class (``rate_class``) instead of
    a preset's, so 1080p on a 720p window costs what 720p does, and native on a
    4K screen (recorded at 1080p) what 1080p does.
    """
    explicit = int(capture.get("bitrate_kbps") or 0)
    if explicit > 0:
        return explicit
    res = configured(capture)
    q = str(capture.get("quality", DEFAULT_QUALITY)).lower()
    if q not in QUALITIES:
        raise ValueError(f"unknown quality {q!r} (choose: {', '.join(QUALITIES)})")
    if effective_resolution(res, source) != res or res == "native":
        res = rate_class(recorded_size(res, source))
    mbps = _MBPS[res][QUALITIES.index(q)]
    # Twice the frames needs ~1.5x the bits for the same look (motion between
    # frames is smaller, so each frame costs less).
    if fps(capture) == 120:
        mbps = round(mbps * 1.5)
    return mbps * 1000


def buffer_gb(kbps: int, seconds: int = 3600) -> float:
    """Disk used by a full buffer (video only; audio adds ~70 MB/h)."""
    return kbps * seconds / 8 / 1_000_000
