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
    "record": "screen (the whole screen), window (only the game window you pick)",
    "resolution": ", ".join(quality.RESOLUTIONS),
    "quality": ", ".join(quality.QUALITIES),
    "fps": ", ".join(map(str, quality.FPS_CHOICES)),
    "bitrate": "video kbps; 0 = automatic",
    "audio_source": "default, off, or an output's monitor source name",
    "mic": "on, off",
    "mic_device": "default, or an input source name",
}

# What gets recorded: user-facing value -> label (the bar, `momento settings`).
RECORD_LABELS = {"screen": "Full screen", "window": "Game window"}
_RECORD_ALIASES = {"screen": "screen", "full": "screen", "fullscreen": "screen", "full screen": "screen",
                   "full-screen": "screen", "monitor": "screen", "display": "screen", "desktop": "screen",
                   "window": "window", "game": "window", "game window": "window", "game-window": "window",
                   "app": "window"}

_RES_ALIASES = {"4k": "2160p", "uhd": "2160p", "2k": "1440p", "qhd": "1440p", "fhd": "1080p", "hd": "720p"}
_ON = {"on", "true", "yes", "1"}
_OFF = {"off", "false", "no", "0"}


def _device(text) -> str:
    text = str(text).strip()
    if not text:
        raise ValueError("device name is empty")
    if len(text) > 256 or any(ord(c) < 32 for c in text):
        raise ValueError("not a valid device name")
    return text


def normalize(key: str, value):
    """Return the canonical user-facing value for ``key`` or raise ValueError."""
    if key == "record":
        v = _RECORD_ALIASES.get(" ".join(str(value).strip().lower().split()))
        if v is None:
            raise ValueError("choose one of: screen, window")
        return v
    if key == "resolution":
        v = str(value).strip().lower()
        v = _RES_ALIASES.get(v, v)
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
    if key == "mic":
        if isinstance(value, bool):
            return "on" if value else "off"
        v = str(value).strip().lower()
        if v in _ON:
            return "on"
        if v in _OFF:
            return "off"
        raise ValueError("choose one of: on, off")
    if key == "mic_device":
        v = str(value).strip()
        if v.lower() in ("default", DEFAULT_SOURCE.lower()):
            return "default"
        return _device(v)
    raise ValueError(f"unknown setting {key!r} (choose: {', '.join(KEYS)})")


def validate(changes: dict) -> dict:
    """Normalise every value; the first bad one raises ValueError("key: why")."""
    if not isinstance(changes, dict):
        raise ValueError("changes must be an object of setting -> value")
    out = {}
    for key, value in changes.items():
        try:
            out[key] = normalize(key, value)
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
    raise ValueError(f"unknown setting {key!r}")


def current(cfg: dict) -> dict:
    """User-facing values of a loaded config."""
    cap, a = cfg["capture"], cfg["audio"]
    dev = a.get("desktop_device") or DEFAULT_MONITOR
    mic_dev = a.get("microphone_device") or DEFAULT_SOURCE
    return {
        "record": config.capture_target(cap),
        "resolution": str(cap.get("resolution", quality.DEFAULT_RESOLUTION)).lower(),
        "quality": str(cap.get("quality", quality.DEFAULT_QUALITY)).lower(),
        "fps": int(cap.get("fps") or quality.FPS),
        "bitrate": int(cap.get("bitrate_kbps") or 0),
        "audio_source": "off" if not a.get("desktop") else "default" if dev == DEFAULT_MONITOR else dev,
        "mic": "on" if a.get("microphone") else "off",
        "mic_device": "default" if mic_dev == DEFAULT_SOURCE else mic_dev,
    }


def apply(changes: dict, path: Path | str | None = None) -> dict:
    """Validate ``changes`` and write them to the config file.

    Nothing is written unless every value is valid. Returns the settings whose
    value actually changed (empty dict: nothing to restart).
    """
    clean = validate(changes)
    path = Path(path) if path else config.default_path()
    before = current(config.load(path))
    for key, value in clean.items():
        for section, name, val in writes(key, value):
            config.set_value(section, name, val, path)
    return {k: v for k, v in clean.items() if before.get(k) != v}


def preview(cfg: dict, changes: dict) -> dict:
    """A copy of a loaded config with ``changes`` applied in memory (nothing is written)."""
    import copy

    out = copy.deepcopy(cfg)
    for key, value in validate(changes).items():
        for section, name, val in writes(key, value):
            out.setdefault(section, {})[name] = val
    return out


def describe(cfg: dict, devices: dict | None = None) -> dict:
    """Everything a settings UI needs: current values, choices, audio devices."""
    return {
        "ok": True,
        "values": current(cfg),
        "choices": {"record": list(config.CAPTURE_TARGETS), "resolution": list(quality.RESOLUTIONS),
                    "quality": list(quality.QUALITIES), "fps": list(quality.FPS_CHOICES)},
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
