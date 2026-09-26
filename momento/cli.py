"""Command line: momento daemon | overlay | save 5m | status | settings | set KEY VALUE | pause | resume | quit."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import __version__, config, durations, quality, settings, storage


def _duration(text: str) -> int:
    try:
        return durations.parse(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from e


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="momento", description="PS5-style replay buffer recorder.")
    p.add_argument("--config", type=Path, metavar="PATH", help="config file (default: %(default)s)",
                   default=config.CONFIG_DIR / "config.toml")
    p.add_argument("-v", "--verbose", action="count", default=0, help="more logging (-vv for debug)")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", metavar="COMMAND")
    sub.add_parser("daemon", help="run the recorder in the foreground")
    ov = sub.add_parser("overlay", help="show (or hide) the save overlay")
    ov.add_argument("--resident", action="store_true",
                    help="keep a hidden bar loaded for instant opening (the daemon starts this itself)")
    presets = ", ".join(label for _, label in durations.PRESETS)
    s = sub.add_parser("save", help="save the last N of footage")
    s.add_argument("duration", type=_duration, help=f"e.g. {presets}, or 90s / 2m")
    sub.add_parser("status", help="show recorder status")
    sub.add_parser("settings", help="show video and audio settings (and audio devices)")
    keys = "; ".join(f"{k}: {h}" for k, h in settings.KEYS.items())
    st = sub.add_parser("set", help="change a setting, e.g. `set resolution 1440p`",
                        description=f"Settings: {keys}.")
    st.add_argument("key", choices=list(settings.KEYS))
    st.add_argument("value")
    ctl = sub.add_parser("controller", help="list the game controllers Momento can use")
    ctl.add_argument("--watch", action="store_true",
                     help="print what each button does until Ctrl+C (to check the buttons; "
                          "the controller keeps working in games)")
    sub.add_parser("pause", help="pause recording (what is buffered can still be saved)")
    sub.add_parser("resume", help="resume recording (earlier footage stays in the replay buffer)")
    sub.add_parser("stop", help="stop recording and clear the replay history (Momento keeps running; "
                                "the shortcut still opens the bar)")
    q = sub.add_parser("quit", help="shut down the Momento service completely and clear the replay buffer")
    q.add_argument("--keep-buffer", action="store_true",
                   help="keep the recorded footage on disk; it is saveable again after the next start")
    return p


def _setup_logging(verbose: int) -> None:
    level = logging.WARNING if verbose == 0 else logging.INFO if verbose == 1 else logging.DEBUG
    logging.basicConfig(level=level, format="%(asctime)s %(name)s %(levelname)s: %(message)s")


def _request(msg: dict, timeout: float = 120) -> dict | None:
    from . import ipc

    try:
        return ipc.request(msg, timeout=timeout)
    except ipc.DaemonNotRunning:
        print("momento: daemon is not running (start it with `momento daemon`)", file=sys.stderr)
    except (ipc.IPCError, OSError, ValueError) as e:
        print(f"momento: {e}", file=sys.stderr)
    return None


def storage_line(st: dict) -> str:
    """'3.1 GB free, needs 8.2 GB - not enough' (+ the buffer a restart would free, if any)."""
    free = f"{storage.human(st.get('free', 0))} free"
    if st.get("reclaimable"):
        free += f" + {storage.human(st['reclaimable'])} buffer"
    verdict = "ok" if st.get("ok") else "not enough"
    return f"{free}, needs {storage.human(st.get('required', 0))} \u2014 {verdict}"


def controller_line(cfg: dict) -> str:
    """"hold View + Menu (0.3 s) to open or close the bar" / "off"."""
    from . import gamepad

    ctl = config.controller(cfg)
    if not ctl["enabled"]:
        return "off"
    label = settings.controller_label(settings.current(cfg)["controller"])
    line = f"hold {label} ({ctl['hold_ms'] / 1000:g} s) to open or close the bar"
    if not ctl["exclusive"]:
        line += "; the game also sees the presses"
    if not gamepad.available():
        line += " (needs python-evdev, not installed)"
    return line


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 2
    # The daemon is long-running; give it INFO by default so the journal is useful.
    _setup_logging(max(args.verbose, 1) if args.command == "daemon" else args.verbose)

    if args.command == "overlay":
        from . import overlay

        if args.resident:
            return overlay.main(["--resident"]) or 0
        # A loaded bar (see [ui] keep_bar_loaded) toggles instantly; else start one.
        if overlay.toggle():
            return 0
        return overlay.main([]) or 0

    if args.command == "controller":
        from . import gamepad

        try:
            ctl = config.controller(config.load(args.config))
        except (OSError, ValueError):
            ctl = config.controller({})
        print(f"shortcut: {controller_line(config.load(args.config)) if ctl['enabled'] else 'off'}")
        return gamepad.main((["--watch"] if args.watch else [])
                            + ["--chord", "+".join(ctl["chord"]), "--hold-ms", str(ctl["hold_ms"])])

    if args.command == "daemon":
        from . import daemon

        try:
            cfg = config.load(args.config)
        except (OSError, ValueError) as e:
            print(f"momento: bad config {args.config}: {e}", file=sys.stderr)
            return 1
        return daemon.main(cfg)

    if args.command == "save":
        r = _request({"cmd": "save", "seconds": args.duration})
        if r is None:
            return 1
        if not r.get("ok"):
            print(f"momento: {r.get('error', 'save failed')}", file=sys.stderr)
            return 1
        if r.get("partial"):
            print(f"momento: only {durations.label(r['seconds'])} was buffered "
                  f"(asked for {durations.label(r['requested'])})", file=sys.stderr)
        print(r["path"])
        return 0

    if args.command == "status":
        r = _request({"cmd": "status"}, timeout=10)
        if r is None:
            return 1
        if not r.get("ok"):
            print(f"momento: {r.get('error', 'status failed')}", file=sys.stderr)
            return 1
        rows = [
            ("state", r.get("state", "?") + (f" ({r['error']})" if r.get("error") else "")),
            ("buffered", f"{durations.clock(r.get('buffered', 0))} / {durations.clock(r.get('max_seconds', 0))}"),
            ("record", settings.RECORD_LABELS.get(r.get("target") or "screen", r.get("target") or "-").lower()),
            ("video", f"{r.get('resolution', '?')} {r.get('fps', 60)} fps, {r.get('quality', '?')} "
                      f"({r.get('bitrate_kbps', 0) / 1000:g} Mbps)"),
            ("source", r.get("source") or "-"),
            ("encoder", r.get("encoder") or "-"),
            ("output", r.get("output_dir") or "-"),
        ]
        if isinstance(r.get("storage"), dict):
            rows.append(("storage", storage_line(r["storage"])))
        for key, value in rows:
            print(f"{key:>9}: {value}")
        return 0

    if args.command == "settings":
        try:
            cfg = config.load(args.config)
            kbps = quality.bitrate_kbps(cfg["capture"])
        except (OSError, ValueError) as e:
            print(f"momento: bad config {args.config}: {e}", file=sys.stderr)
            return 1
        cur = settings.current(cfg)
        auto = "" if cur["bitrate"] else " (automatic)"
        devices = settings.list_audio_devices()
        labels = {d["name"]: d["label"] for d in devices["outputs"] + devices["inputs"]}
        sound = {"off": "off", "default": "default output (follows your speakers/headphones)"}.get(
            cur["audio_source"], labels.get(cur["audio_source"], cur["audio_source"]))
        mic = "off"
        if cur["mic"] == "on":
            mic = "on, " + ("default input" if cur["mic_device"] == "default"
                            else labels.get(cur["mic_device"], cur["mic_device"]))
        record = settings.RECORD_LABELS[cur["record"]].lower()
        if cur["record"] == "window":
            record += " (only the window you pick; the bar and notifications stay out)"
        rows = [
            ("record", record),
            ("resolution", cur["resolution"]),
            ("quality", cur["quality"]),
            ("frame rate", f"{quality.fps(cfg['capture'])} fps"),
            ("bitrate", f"{kbps / 1000:g} Mbps{auto}"),
            ("disk use", f"about {storage.human(storage.buffer_bytes(cfg))} for the full buffer"),
            ("storage", storage_line(storage.check(cfg, storage.dir_bytes(storage.buffer_dir(cfg))))),
            ("sound", sound),
            ("mic", mic),
            ("controller", controller_line(cfg)),
            ("clips", cfg["output"]["dir"]),
            ("config", cfg["_path"]),
        ]
        for key, value in rows:
            print(f"{key:>10}: {value}")
        for title, kind in (("outputs (momento set audio_source NAME)", "outputs"),
                            ("inputs (momento set mic_device NAME)", "inputs")):
            if devices[kind]:
                print(f"\n{title}:")
                for d in devices[kind]:
                    mark = "  (default)" if d["default"] else ""
                    print(f"  {d['name']}  {d['label']}{mark}")
        return 0

    if args.command == "set":
        try:
            clean = settings.validate({args.key: args.value})
        except ValueError as e:
            print(f"momento: {e}", file=sys.stderr)
            return 1
        from . import ipc

        # With the daemon running, it validates storage and writes the config itself.
        try:
            r = ipc.request({"cmd": "configure", "changes": clean}, timeout=30)
        except ipc.DaemonNotRunning:
            r = None
        except (ipc.IPCError, OSError) as e:
            print(f"momento: could not reach the daemon (nothing saved): {e}", file=sys.stderr)
            return 1
        if r is not None:
            if not r.get("ok"):
                print(f"momento: {r.get('error', 'not saved')}", file=sys.stderr)
                return 1
            changed = r.get("changed") or {}
            print(f"{args.key} = {changed.get(args.key, clean[args.key])}")
            if set(clean) <= set(settings.CONTROLLER_KEYS):
                line = controller_line(config.load(args.config))
                print(f"Saved. {line[:1].upper()}{line[1:]}." if changed else "Saved (nothing changed).")
            elif r.get("warning"):
                print(f"momento: warning: {r['warning']}. Recording stays off until there is room.",
                      file=sys.stderr)
            elif r.get("paused"):
                print("Saved. Recording is paused; the new setting applies when you resume.")
            elif r.get("restarted") and clean.get("record") == "window":
                print("Recording restarted. Pick your game window in the dialog that opens.")
            elif r.get("restarted"):
                print("Recording restarted with the new setting.")
            else:
                print("Saved (nothing changed).")
            return 0
        # Daemon off: write the file; warn (non-fatal) if the new settings won't fit.
        try:
            settings.apply(clean, args.config)
            cfg = config.load(args.config)
            # The daemon empties its buffer dir on start, so that space counts as free.
            chk = storage.check(cfg, storage.dir_bytes(storage.buffer_dir(cfg)))
        except (OSError, ValueError) as e:
            print(f"momento: {e}", file=sys.stderr)
            return 1
        print(f"{args.key} = {clean[args.key]}  (saved to {args.config})")
        if not chk["ok"]:
            print(f"momento: warning: {storage.label(cfg)} needs {storage.human(chk['required'])} free, "
                  f"{storage.human(chk['free'] + chk['reclaimable'])} available; "
                  "Momento won't record until there is room.", file=sys.stderr)
        return 0

    if args.command in ("pause", "resume"):
        r = _request({"cmd": args.command}, timeout=30)
        if r is None:
            return 1
        if not r.get("ok"):
            print(f"momento: {r.get('error', args.command + ' failed')}", file=sys.stderr)
            return 1
        if args.command == "pause":
            print("Paused. What was buffered can still be saved; `momento resume` continues the same replay buffer.")
        else:
            print("Recording resumed (earlier footage is kept).")
        return 0

    if args.command == "stop":
        r = _request({"cmd": "stop"}, timeout=10)
        if r and r.get("ok"):
            print("Recording stopped and the replay history cleared. Start again with `momento resume` "
                  "or the play button in the clip bar.")
            return 0
        return 1

    if args.command == "quit":
        msg = {"cmd": "quit"}
        if getattr(args, "keep_buffer", False):
            msg["keep_buffer"] = True
        r = _request(msg, timeout=10)
        return 0 if r and r.get("ok") else 1

    parser.error(f"unknown command {args.command}")
    return 2
