"""User-changeable settings: one table shared by the CLI, the daemon and the overlay.

Each setting has a short user-facing key and value ("audio_source" = "off")
that maps onto one or more keys of the TOML config. ``validate`` normalises
values, ``apply`` writes them with ``config.set_value`` (keeping the user's
comments), ``current`` reads them back out of a loaded config.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from . import config, quality

DEFAULT_MONITOR = "@DEFAULT_MONITOR@"
DEFAULT_SOURCE = "@DEFAULT_SOURCE@"

# key -> one-line help (also the order `momento set` lists them in)
KEYS = {
    "record": "window (only the window you pick; the default), screen (the whole screen)",
    "replay_length": "15m, 30m, 60m (how much of your game the replay keeps; 15m is the default, "
                     "longer needs more disk space)",
    "resolution": ", ".join(quality.RESOLUTIONS),
    "quality": ", ".join(quality.QUALITIES),
    "fps": ", ".join(map(str, quality.FPS_CHOICES)),
    "bitrate": "video kbps; 0 = automatic",
    "audio_source": "default, off, or an output's monitor source name",
    "mic": "on, off",
    "mic_device": "default, or an input source name",
    "controller": "off, on, or the shortcut that opens the bar: ps_down (PS / Xbox / Home + D-pad "
                  "Down, the default), view_menu, left_paddle, right_paddle, l3_r3, or buttons joined "
                  "with + (e.g. select+start)",
    "controller_exclusive": "on, off (take the controller over while the bar is open)",
    "controller_open": "hold, tap (tap, the default, opens the bar the instant the buttons are "
                       "down; hold waits until they have been held for 0.3 s)",
    "keep_history": "off, on (keep the replay when recording stops, and save every full replay "
                    "length to your clips folder)",
    "hour_warning": "10, 5, 3 (minutes before the replay is full to warn; any whole number 3-10)",
    "instant_bar": "on, off (keep the clip bar loaded so it opens instantly; uses ~80-120 MB)",
}

# Settings that only concern the controller: changing them never restarts recording.
CONTROLLER_KEYS = ("controller", "controller_exclusive", "controller_open")
# Every setting that takes effect without restarting the recording.
LIVE_KEYS = CONTROLLER_KEYS + ("replay_length", "keep_history", "hour_warning", "instant_bar")

# How a settings UI groups the keys: (tab name, keys in display order). "bitrate"
# is left out on purpose (terminal only: `momento set bitrate`).
TABS = (
    ("General", ("record", "replay_length", "keep_history")),
    ("Video", ("resolution", "fps", "quality")),
    ("Audio", ("audio_source", "mic", "mic_device")),
    ("Controller", ("controller", "controller_exclusive", "controller_open")),
    ("Misc", ("hour_warning", "instant_bar")),
)

# What gets recorded: user-facing value -> label (the bar, `momento settings`).
RECORD_LABELS = {"screen": "Full screen", "window": "Window"}
_RECORD_ALIASES = {"screen": "screen", "full": "screen", "fullscreen": "screen", "full screen": "screen",
                   "full-screen": "screen", "monitor": "screen", "display": "screen", "desktop": "screen",
                   "window": "window", "game": "window", "game window": "window", "game-window": "window",
                   "app": "window"}

# How the controller shortcut opens the bar: user-facing value -> clip-bar label.
# "tap" is [controller] hold_ms = 0 (the default: open the instant every button of
# the shortcut is down), "hold" is hold_ms above 0 (config.HOLD_MS, 0.3 s).
CONTROLLER_OPEN_LABELS = {"hold": "Hold", "tap": "Tap"}
_OPEN_ALIASES = {"hold": "hold", "long": "hold", "tap": "tap", "press": "tap", "instant": "tap"}

_ON = {"on", "true", "yes", "1"}
_OFF = {"off", "false", "no", "0"}


class Unavailable(ValueError):
    """A known value that is not offered yet (1440p, 4K). Its message is a whole
    sentence, so ``validate`` passes it on without the "key: " prefix."""


def _device(text) -> str:
    text = str(text).strip()
    if not text:
        raise ValueError("device name is empty")
    if len(text) > 256 or any(ord(c) < 32 for c in text):
        raise ValueError("not a valid device name")
    return text


def _on_off(value) -> str:
    if isinstance(value, bool):
        return "on" if value else "off"
    v = str(value).strip().lower()
    if v in _ON:
        return "on"
    if v in _OFF:
        return "off"
    raise ValueError("choose one of: on, off")


def _warn_minutes(value) -> int:
    """10 / "5" / "5m" / "3 min" -> minutes, a whole number 3-10."""
    lo, hi = config.WARN_RANGE
    choices = ", ".join(map(str, config.WARN_MINUTES))
    if isinstance(value, bool):
        raise ValueError(f"choose {choices} (minutes)")
    text = str(value).strip().lower()
    for unit in ("minutes", "minute", "mins", "min", "m"):
        if text.endswith(unit):
            text = text[: -len(unit)].strip()
            break
    try:
        v = int(text)
    except ValueError:
        raise ValueError(f"choose {choices} (minutes)") from None
    if not lo <= v <= hi:
        raise ValueError(f"choose {choices} (any whole number of minutes from {lo} to {hi})")
    return v


def _replay_minutes(value) -> int:
    """15 / "30m" / "60 min" / "1h" / 900 (seconds) -> minutes, one of config.REPLAY_MINUTES."""
    choices = config.REPLAY_MINUTES
    why = "choose " + ", ".join(f"{m}m" for m in choices)
    if isinstance(value, bool):
        raise ValueError(why)
    text = str(value).strip().lower()
    scale = None
    for units, factor in ((("hours", "hour", "hrs", "hr", "h"), 60),
                          (("minutes", "minute", "mins", "min", "m"), 1),
                          (("seconds", "second", "secs", "sec", "s"), 1 / 60)):
        unit = next((u for u in units if text.endswith(u)), None)
        if unit:
            text, scale = text[: -len(unit)].strip(), factor
            break
    try:
        n = int(text)
    except ValueError:
        raise ValueError(why) from None
    if scale is None:  # a bare number: minutes, or the seconds of one of the choices
        scale = 1 / 60 if n in [m * 60 for m in choices] else 1
    minutes = n * scale
    if minutes not in choices:
        raise ValueError(why)
    return int(minutes)


def replay_minutes(cfg: dict) -> int:
    """The replay length of a loaded config in whole minutes (a hand-edited 1234 s reads as 21)."""
    return max(1, round(int(cfg["buffer"]["max_seconds"]) / 60))


def normalize(key: str, value):
    """Return the canonical user-facing value for ``key`` or raise ValueError."""
    if key == "record":
        v = _RECORD_ALIASES.get(" ".join(str(value).strip().lower().split()))
        if v is None:
            raise ValueError("choose one of: screen, window")
        return v
    if key == "resolution":
        v = str(value).strip().lower()
        v = quality.ALIASES.get(v, v)
        if v in quality.LATER:
            raise Unavailable(quality.later_message())
        if v not in quality.RESOLUTIONS:
            raise ValueError(f"choose one of: {', '.join(quality.RESOLUTIONS)}")
        return v
    if key == "quality":
        v = str(value).strip().lower()
        if v not in quality.QUALITIES:
            raise ValueError(f"choose one of: {', '.join(quality.QUALITIES)}")
        return v
    if key == "fps":
        try:
            v = int(str(value).strip().lower().removesuffix("fps").strip())
        except ValueError:
            v = None
        if v not in quality.FPS_CHOICES:
            raise ValueError(f"choose one of: {', '.join(map(str, quality.FPS_CHOICES))}")
        return v
    if key == "bitrate":
        if isinstance(value, bool):
            raise ValueError("bitrate is a number of kbps")
        try:
            v = int(str(value).strip())
        except ValueError:
            raise ValueError("bitrate is a number of kbps") from None
        if v < 0 or 0 < v < 1000:
            raise ValueError("bitrate is in kbps: 0 (automatic) or at least 1000")
        return v
    if key == "audio_source":
        v = str(value).strip()
        if v.lower() in ("default", DEFAULT_MONITOR.lower()):
            return "default"
        if v.lower() == "off":
            return "off"
        return _device(v)
    if key in ("mic", "controller_exclusive", "keep_history", "instant_bar"):
        return _on_off(value)
    if key == "hour_warning":
        return _warn_minutes(value)
    if key == "replay_length":
        return _replay_minutes(value)
    if key == "mic_device":
        v = str(value).strip()
        if v.lower() in ("default", DEFAULT_SOURCE.lower()):
            return "default"
        return _device(v)
    if key == "controller":
        return _controller(value)
    if key == "controller_open":
        v = _OPEN_ALIASES.get(" ".join(str(value).strip().lower().split()))
        if v is None or isinstance(value, bool):
            raise ValueError("choose one of: hold, tap")
        return v
    raise ValueError(f"unknown setting {key!r} (choose: {', '.join(KEYS)})")


def controller_label(value: str) -> str:
    """"ps_down" -> "PS / Xbox + Down", "select+mode" -> "Select + Mode", "off" -> "Off"."""
    from . import gamepad

    if value in ("off", "on"):
        return value.capitalize()
    for key, label, _buttons in gamepad.CHORD_PRESETS:
        if key == value:
            return label
    try:
        return gamepad.chord_label(value)
    except ValueError:
        return str(value)


def _chord_value(buttons) -> str:
    """A preset key when the buttons match one, else "a+b"."""
    from . import gamepad

    names = gamepad.normalize_chord(buttons)
    for key, _label, preset in gamepad.CHORD_PRESETS:
        if tuple(preset) == names:
            return key
    return "+".join(names)


def _controller(value) -> str:
    """off | on | a preset key | "a+b" (validated button names)."""
    from . import gamepad

    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, (list, tuple)):
        return _chord_value(value)
    v = " ".join(str(value).strip().lower().split())
    if v in _ON:
        return "on"
    if v in _OFF:
        return "off"
    for key, label, buttons in gamepad.CHORD_PRESETS:
        if v in (key, label.lower(), key.replace("_", " "), key.replace("_", "-")):
            return key
    try:
        return _chord_value(v)
    except ValueError as e:
        presets = ", ".join(k for k, _l, _b in gamepad.CHORD_PRESETS)
        why = str(e).split(";")[0]  # "unknown controller button 'turbo'"
        raise ValueError(f"choose off, on, {presets}, or buttons joined with + ({why})") from None


def validate(changes: dict) -> dict:
    """Normalise every value; the first bad one raises ValueError("key: why")."""
    if not isinstance(changes, dict):
        raise ValueError("changes must be an object of setting -> value")
    out = {}
    for key, value in changes.items():
        try:
            out[key] = normalize(key, value)
        except Unavailable:
            raise
        except ValueError as e:
            raise ValueError(f"{key}: {e}") from None
    return out


def writes(key: str, value) -> list[tuple[str, str, object]]:
    """Config (section, key, value) triples for one normalised setting."""
    if key == "record":
        return [("capture", "target", value)]
    if key == "resolution":
        return [("capture", "resolution", value)]
    if key == "quality":
        return [("capture", "quality", value)]
    if key == "fps":
        return [("capture", "fps", value)]
    if key == "bitrate":
        return [("capture", "bitrate_kbps", value)]
    if key == "audio_source":
        if value == "off":
            return [("audio", "desktop", False)]
        device = DEFAULT_MONITOR if value == "default" else value
        return [("audio", "desktop", True), ("audio", "desktop_device", device)]
    if key == "mic":
        return [("audio", "microphone", value == "on")]
    if key == "mic_device":
        return [("audio", "microphone_device", DEFAULT_SOURCE if value == "default" else value)]
    if key == "controller":
        if value in ("on", "off"):
            return [("controller", "enabled", value == "on")]
        from . import gamepad

        buttons = next((list(b) for k, _l, b in gamepad.CHORD_PRESETS if k == value), None)
        return [("controller", "enabled", True),
                ("controller", "open_chord", buttons or list(gamepad.normalize_chord(value)))]
    if key == "controller_exclusive":
        return [("controller", "exclusive", value == "on")]
    if key == "controller_open":
        hold = config.HOLD_MS if value == "hold" else 0
        return [("controller", "hold_ms", hold)]
    if key == "keep_history":
        return [("buffer", "keep_history", value == "on")]
    if key == "hour_warning":
        return [("buffer", "warn_minutes", int(value))]
    if key == "replay_length":
        return [("buffer", "max_seconds", int(value) * 60)]
    if key == "instant_bar":
        return [("ui", "keep_bar_loaded", value == "on")]
    raise ValueError(f"unknown setting {key!r}")


def current(cfg: dict) -> dict:
    """User-facing values of a loaded config."""
    cap, a = cfg["capture"], cfg["audio"]
    dev = a.get("desktop_device") or DEFAULT_MONITOR
    mic_dev = a.get("microphone_device") or DEFAULT_SOURCE
    ctl = config.controller(cfg)
    return {
        "record": config.capture_target(cap),
        "replay_length": replay_minutes(cfg),
        # an older config's 1440p/2160p reads as what it records at (1080p)
        "resolution": quality.offered(cap.get("resolution", quality.DEFAULT_RESOLUTION)),
        "quality": str(cap.get("quality", quality.DEFAULT_QUALITY)).lower(),
        "fps": int(cap.get("fps") or quality.FPS),
        "bitrate": int(cap.get("bitrate_kbps") or 0),
        "audio_source": "off" if not a.get("desktop") else "default" if dev == DEFAULT_MONITOR else dev,
        "mic": "on" if a.get("microphone") else "off",
        "mic_device": "default" if mic_dev == DEFAULT_SOURCE else mic_dev,
        "controller": _chord_value(ctl["chord"]) if ctl["enabled"] else "off",
        "controller_exclusive": "on" if ctl["exclusive"] else "off",
        # any hold_ms above 0 (e.g. a hand-edited 500) is "hold"
        "controller_open": "tap" if ctl["hold_ms"] == 0 else "hold",
        "keep_history": "on" if config.keep_history(cfg) else "off",
        "hour_warning": config.warn_minutes(cfg),
        "instant_bar": "on" if (cfg.get("ui") or {}).get("keep_bar_loaded", True) else "off",
    }


# Settings that several config values read as (controller_open "hold" is any
# hold_ms above 0; replay_length 15 is a hand-edited max_seconds = 910 too):
# choosing the value they already have writes nothing, so a hand-edited value
# behind it survives.
_KEEP_IF_SAME = ("controller_open", "replay_length")


def apply(changes: dict, path: Path | str | None = None) -> dict:
    """Validate ``changes`` and write them to the config file.

    Nothing is written unless every value is valid. Returns the settings whose
    value actually changed (empty dict: nothing to restart).
    """
    clean = validate(changes)
    path = Path(path) if path else config.default_path()
    before = current(config.load(path))
    for key, value in clean.items():
        if key in _KEEP_IF_SAME and before.get(key) == value:
            continue  # "hold" over a hand-edited hold_ms = 500 keeps the 500
        for section, name, val in writes(key, value):
            config.set_value(section, name, val, path)
    after = current(config.load(path))  # "controller": "on" reads back as the shortcut it enables
    return {k: after.get(k, v) for k, v in clean.items() if before.get(k) != after.get(k, v)}


def preview(cfg: dict, changes: dict) -> dict:
    """A copy of a loaded config with ``changes`` applied in memory (nothing is written)."""
    import copy

    out = copy.deepcopy(cfg)
    before = current(cfg)
    for key, value in validate(changes).items():
        if key in _KEEP_IF_SAME and before.get(key) == value:
            continue
        for section, name, val in writes(key, value):
            out.setdefault(section, {})[name] = val
    return out


def describe(cfg: dict, devices: dict | None = None, source=None) -> dict:
    """Everything a settings UI needs: current values, choices, tabs, audio devices.

    ``source`` is the size of the recorded picture when the daemon knows it (the
    screen, or the picked window): resolutions taller than it are not worth
    offering (``resolution_allowed``) and would record at its size
    (``resolution_effective``). Unknown: every resolution is allowed.
    """
    from . import gamepad

    values = current(cfg)
    source = quality.source_size(source)
    return {
        "ok": True,
        "values": values,
        "source_size": list(source) if source else None,
        "resolution_allowed": quality.allowed_resolutions(source),
        "resolution_effective": quality.effective_resolution(values["resolution"], source),
        "choices": {"record": list(config.CAPTURE_TARGETS), "replay_length": list(config.REPLAY_MINUTES),
                    "resolution": list(quality.RESOLUTIONS),
                    "quality": list(quality.QUALITIES), "fps": list(quality.FPS_CHOICES),
                    "controller": ["off"] + [k for k, _l, _b in gamepad.CHORD_PRESETS],
                    "controller_open": list(CONTROLLER_OPEN_LABELS),
                    "keep_history": ["off", "on"], "hour_warning": list(config.WARN_MINUTES),
                    "instant_bar": ["on", "off"]},
        "tabs": [[name, list(keys)] for name, keys in TABS],
        # python-evdev importable: without it the controller settings are saved but unused
        "controller_available": gamepad.available(),
        "devices": list_audio_devices() if devices is None else devices,
        "fps": quality.fps(cfg["capture"]),
        "max_seconds": int(cfg["buffer"]["max_seconds"]),
        "config": cfg.get("_path") or str(config.default_path()),
    }


# --------------------------------------------------------------------------
# audio devices (pactl works on both PulseAudio and PipeWire-Pulse)
# --------------------------------------------------------------------------

def _is_monitor(src: dict) -> bool:
    props = src.get("properties") or {}
    return (props.get("device.class") == "monitor"
            or props.get("media.class") == "Audio/Sink"
            or str(src.get("name", "")).endswith(".monitor"))


def parse_devices(sinks_json: str | None, sources_json: str | None,
                  default_sink: str | None = None, default_source: str | None = None) -> dict:
    """Turn ``pactl -f json list sinks/sources`` output into outputs/inputs lists."""

    def load(text):
        try:
            data = json.loads(text) if text else []
        except ValueError:
            return []
        return [d for d in data if isinstance(d, dict) and d.get("name")] if isinstance(data, list) else []

    def label(d):
        props = d.get("properties") or {}
        return str(d.get("description") or props.get("device.description") or d["name"])

    outputs = []
    for s in load(sinks_json):
        outputs.append({"name": s.get("monitor_source") or f"{s['name']}.monitor",
                        "label": label(s), "default": s["name"] == default_sink})
    inputs = []
    for s in load(sources_json):
        if _is_monitor(s):
            continue
        inputs.append({"name": s["name"], "label": label(s), "default": s["name"] == default_source})
    return {"outputs": outputs, "inputs": inputs}


def list_audio_devices(run=subprocess.run) -> dict:
    """Outputs (as monitor sources to record) and inputs; empty lists without pactl."""
    if shutil.which("pactl") is None:
        return {"outputs": [], "inputs": []}

    def pactl(*args):
        try:
            r = run(["pactl", *args], capture_output=True, text=True, errors="replace", timeout=3)
        except (OSError, subprocess.SubprocessError):
            return None
        return r.stdout.strip() if r.returncode == 0 else None

    return parse_devices(pactl("-f", "json", "list", "sinks"), pactl("-f", "json", "list", "sources"),
                         pactl("get-default-sink"), pactl("get-default-source"))
