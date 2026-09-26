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
# The resident clip bar's control socket (toggle / show / hide / quit), see overlay.py.
OVERLAY_SOCKET = RUNTIME_DIR / "overlay.sock"


def _videos_dir() -> Path:
    try:
        import subprocess
        out = subprocess.run(["xdg-user-dir", "VIDEOS"], capture_output=True, text=True, timeout=2)
        if out.returncode == 0 and out.stdout.strip() and out.stdout.strip() != str(Path.home()):
            return Path(out.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return Path.home() / "Videos"


# The hold that "Open with: Hold" writes to [controller] hold_ms, in ms.
HOLD_MS = 300

DEFAULTS = {
    "capture": {
        # auto | portal | gamescope | x11 | test
        "source": "auto",
        # 60 | 120 frames per second (120 needs a 120 Hz display to be useful).
        "fps": 60,
        # 720p | 1080p | native (screen size, scaled down to at most 1080 lines).
        # 1440p / 2160p are not offered yet (quality.MAX_HEIGHT); an older config
        # that has them records at 1080p.
        # Other shapes are fitted with black bars, never stretched.
        "resolution": "1080p",
        # standard | high | ultra
        "quality": "high",
        # 0 = derive from resolution + quality; any number overrides (kbps).
        "bitrate_kbps": 0,
        # auto | vah264enc | vaapih264enc | nvh264enc | qsvh264enc | x264enc | openh264enc
        "encoder": "auto",
        "show_cursor": False,
        # window = only the window the user picks (screen-share portal only; the bar
        # and notifications are then never recorded) | screen = the whole monitor.
        # Window mode waits for the play button instead of starting at login.
        "target": "window",
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
        # Keep the replay when recording stops (Stop, or the recorded window closing);
        # with this on, every full buffer length ("hour") is also saved to the clips
        # folder. Off: stopping clears it.
        "keep_history": False,
        # Minutes before the hour mark to warn (3-10; the bar offers 10, 5, 3).
        "warn_minutes": 10,
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
    "ui": {
        # Keep the clip bar loaded (hidden) in the background so the hotkey shows it
        # instantly. Costs ~80-120 MB of RAM; false = start a new bar on every press
        # (about 0.3-0.5 s until it appears).
        "keep_bar_loaded": True,
    },
    "controller": {
        # Game controllers (needs python-evdev): press the shortcut to open or close
        # the clip bar, then use the D-pad / stick and A / B.
        "enabled": True,
        # Buttons held together, by position: mode (PS / Xbox / Home), dpad_up /
        # dpad_down / dpad_left / dpad_right, select (View / Share / Minus), start
        # (Menu / Options / Plus), thumbl / thumbr (stick clicks), tl / tr (bumpers),
        # south / east / north / west, or left_paddle / right_paddle (back buttons on
        # Elite-style pads and handhelds). With a D-pad direction, the controller is
        # held while the other button is down, so the game doesn't see the D-pad.
        "open_chord": ["mode", "dpad_down"],
        "hold_ms": 0,     # 0 = open on a tap; HOLD_MS for "Open with: Hold"
        # Take the controller over while the bar is open, so the game doesn't see
        # the presses (falls back to sharing it where that isn't possible).
        "exclusive": True,
    },
}


CAPTURE_TARGETS = ("screen", "window")


def capture_target(capture: dict) -> str:
    """"screen" or "window" for a [capture] table (anything unknown counts as "screen")."""
    return "window" if str(capture.get("target") or "").strip().lower() == "window" else "screen"


def portal_token_path(target: str = "screen") -> Path:
    """Where the ScreenCast restore token for ``target`` is kept.

    One token per target, so switching between full screen and a window never
    throws away the other one. The screen token keeps its original file name.
    """
    return STATE_DIR / ("portal_token_window" if target == "window" else "portal_token")


def forget_portal_token(target: str) -> bool:
    """Drop the stored restore token for ``target`` (the next session asks again)."""
    try:
        portal_token_path(target).unlink()
        return True
    except OSError:  # not there (nothing to forget) or not removable
        return False


WARN_MINUTES = (10, 5, 3)  # what the settings UI offers; any whole number 3-10 is valid
WARN_RANGE = (3, 10)


def keep_history(cfg: dict) -> bool:
    """[buffer] keep_history: keep the replay when recording stops (and save each full hour)."""
    return bool((cfg.get("buffer") or {}).get("keep_history", DEFAULTS["buffer"]["keep_history"]))


def warn_minutes(cfg: dict) -> int:
    """[buffer] warn_minutes, checked: a value outside 3-10 falls back to the default (logged)."""
    import logging

    value = (cfg.get("buffer") or {}).get("warn_minutes", DEFAULTS["buffer"]["warn_minutes"])
    try:
        minutes = int(value)
        if isinstance(value, bool) or minutes != value or not WARN_RANGE[0] <= minutes <= WARN_RANGE[1]:
            raise ValueError
    except (ValueError, TypeError):
        minutes = DEFAULTS["buffer"]["warn_minutes"]
        logging.getLogger(__name__).warning("[buffer] warn_minutes must be %d-%d; using %d",
                                            *WARN_RANGE, minutes)
    return minutes


def controller(cfg: dict) -> dict:
    """The [controller] table, checked: {"enabled", "chord" (tuple), "hold_ms", "exclusive"}.

    A hand-edited value that makes no sense falls back to its default (and is logged),
    so a typo never takes controller support down with it.
    """
    import logging

    from . import gamepad

    c = cfg.get("controller")
    c = c if isinstance(c, dict) else {}
    d = DEFAULTS["controller"]
    out = {"enabled": bool(c.get("enabled", d["enabled"])),
           "exclusive": bool(c.get("exclusive", d["exclusive"]))}
    try:
        out["chord"] = gamepad.normalize_chord(c.get("open_chord") or d["open_chord"])
    except (ValueError, TypeError) as e:
        logging.getLogger(__name__).warning("[controller] open_chord: %s; using %s", e,
                                            " + ".join(d["open_chord"]))
        out["chord"] = tuple(d["open_chord"])
    try:
        hold = int(c.get("hold_ms", d["hold_ms"]))
        if isinstance(c.get("hold_ms"), bool) or not 0 <= hold <= 5000:
            raise ValueError
    except (ValueError, TypeError):
        logging.getLogger(__name__).warning("[controller] hold_ms must be 0-5000; using %d", d["hold_ms"])
        hold = d["hold_ms"]
    out["hold_ms"] = hold
    return out


def load_controller(path: Path | None = None) -> dict:
    """``controller()`` of the saved file, without the rest of ``load()`` (the bar reads
    this on every open, so it skips the Videos-folder lookup)."""
    data = {}
    try:
        with open(path or default_path(), "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        pass
    except (OSError, tomllib.TOMLDecodeError):
        data = {}  # a broken file: the defaults (load() reports the error elsewhere)
    table = data.get("controller")
    return controller({"controller": table if isinstance(table, dict) else {}})


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
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
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
