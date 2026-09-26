"""User configuration: ~/.config/momento/config.toml, merged over defaults."""

import copy
import os
import tomllib
from pathlib import Path

APP_ID = "io.github.mehulchachada.Momento"


def _xdg(var: str, fallback: str) -> Path:
    return Path(os.environ.get(var) or Path.home() / fallback)


CONFIG_DIR = _xdg("XDG_CONFIG_HOME", ".config") / "momento"
CACHE_DIR = _xdg("XDG_CACHE_HOME", ".cache") / "momento"
STATE_DIR = _xdg("XDG_STATE_HOME", ".local/state") / "momento"
RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/momento-{os.getuid()}")
SOCKET_PATH = RUNTIME_DIR / "momento.sock"


def _videos_dir() -> Path:
    try:
        import subprocess
        out = subprocess.run(["xdg-user-dir", "VIDEOS"], capture_output=True, text=True, timeout=2)
        if out.returncode == 0 and out.stdout.strip() and out.stdout.strip() != str(Path.home()):
            return Path(out.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return Path.home() / "Videos"


DEFAULTS = {
    "capture": {
        # auto | portal | gamescope | x11 | test
        "source": "auto",
        # 60 | 120 frames per second (120 needs a 120 Hz display to be useful).
        "fps": 60,
        # 720p | 1080p | 1440p | 2160p | native (screen size).
        # Other shapes are fitted with black bars, never stretched.
        "resolution": "1080p",
        # standard | high | ultra
        "quality": "high",
        # 0 = derive from resolution + quality; any number overrides (kbps).
        "bitrate_kbps": 0,
        # auto | vah264enc | vaapih264enc | nvh264enc | qsvh264enc | x264enc | openh264enc
        "encoder": "auto",
        "show_cursor": False,
    },
    "audio": {
        "desktop": True,
        # PulseAudio/PipeWire source name; @DEFAULT_MONITOR@ = whatever you hear.
        "desktop_device": "@DEFAULT_MONITOR@",
        "microphone": False,
        "microphone_device": "@DEFAULT_SOURCE@",
        "bitrate_kbps": 160,
    },
    "buffer": {
        "max_seconds": 3600,
        "segment_seconds": 10,
        "dir": str(CACHE_DIR / "buffer"),
    },
    "output": {
        "dir": "",  # empty = XDG Videos dir / Momento
        "filename": "Momento_{date}_{time}_{length}.mp4",
    },
    "hotkey": {
        # Registered through the xdg-desktop-portal GlobalShortcuts interface.
        "enabled": True,
        "trigger": "LOGO+SHIFT+g",
    },
}


def _merge(base: dict, over: dict) -> dict:
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


def default_path() -> Path:
    return CONFIG_DIR / "config.toml"


def load(path: Path | None = None) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    path = path or default_path()
    if path.exists():
        with open(path, "rb") as f:
            _merge(cfg, tomllib.load(f))
    cfg["_path"] = str(path)
    cfg["buffer"]["max_seconds"] = min(int(cfg["buffer"]["max_seconds"]), 3600)
    if not cfg["output"]["dir"]:
        cfg["output"]["dir"] = str(_videos_dir() / "Momento")
    cfg["buffer"]["dir"] = os.path.expanduser(cfg["buffer"]["dir"])
    cfg["output"]["dir"] = os.path.expanduser(cfg["output"]["dir"])
    return cfg


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def set_value(section: str, key: str, value, path: Path | None = None) -> Path:
    """Set one key in the user's config file, keeping every other line (and comment) as is."""
    import re

    path = path or default_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text().splitlines() if path.exists() else []
    line = f"{key} = {_toml_value(value)}"
    old_text = "\n".join(lines) + "\n" if lines else ""
    current, insert_at, replaced = None, None, False
    for i, raw in enumerate(lines):
        stripped = raw.strip()
        m = re.fullmatch(r"\[\s*([A-Za-z0-9_-]+)\s*\]", stripped)
        if m:
            if current == section:
                break  # end of our section
            current = m.group(1)
            if current == section:
                insert_at = i + 1
        elif current == section:
            if re.match(rf"{re.escape(key)}\s*=", stripped):
                lines[i] = line
                replaced = True
                break
            if stripped:
                insert_at = i + 1
    if not replaced:
        if insert_at is None:
            lines += ([""] if lines else []) + [f"[{section}]", line]
        else:
            lines.insert(insert_at, line)
    new_text = "\n".join(lines) + "\n"
    try:
        tomllib.loads(new_text)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"{path} would not be valid TOML after the change: {e}") from e
    if new_text != old_text:
        path.write_text(new_text)
    return path
