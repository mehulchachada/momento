"""Command line: momento daemon | overlay | save 5m | status | settings | set KEY VALUE | pause | resume | quit."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import __version__, config, durations, quality, settings


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
    sub.add_parser("overlay", help="show (or hide) the save overlay")
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
    sub.add_parser("pause", help="pause recording (what is buffered can still be saved)")
    sub.add_parser("resume", help="resume recording (starts a fresh replay buffer)")
    sub.add_parser("quit", aliases=["stop"], help="stop the daemon")
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

        return overlay.main([]) or 0

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
            ("video", f"{r.get('resolution', '?')} {r.get('fps', 60)} fps, {r.get('quality', '?')} "
                      f"({r.get('bitrate_kbps', 0) / 1000:g} Mbps)"),
            ("source", r.get("source") or "-"),
            ("encoder", r.get("encoder") or "-"),
            ("output", r.get("output_dir") or "-"),
        ]
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
        rows = [
            ("resolution", cur["resolution"]),
            ("quality", cur["quality"]),
            ("frame rate", f"{quality.fps(cfg['capture'])} fps"),
            ("bitrate", f"{kbps / 1000:g} Mbps{auto}"),
            ("disk use", f"about {quality.buffer_gb(kbps, cfg['buffer']['max_seconds']):.1f} GB for the full buffer"),
            ("sound", sound),
            ("mic", mic),
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
            settings.apply(clean, args.config)
        except (OSError, ValueError) as e:
            print(f"momento: {e}", file=sys.stderr)
            return 1
        print(f"{args.key} = {clean[args.key]}  (saved to {args.config})")
        from . import ipc

        try:
            r = ipc.request({"cmd": "reload"}, timeout=30)
        except ipc.DaemonNotRunning:
            return 0  # applies next time the daemon starts
        except (ipc.IPCError, OSError) as e:
            print(f"momento: saved, but could not reach the daemon: {e}", file=sys.stderr)
            return 1
        if not r.get("ok"):
            print(f"momento: {r.get('error')}", file=sys.stderr)
            return 1
        if r.get("paused"):
            print("Saved. Recording is paused; the new setting applies when you resume.")
        else:
            print("Recording restarted with the new setting.")
        return 0

    if args.command in ("pause", "resume"):
        r = _request({"cmd": args.command}, timeout=30)
        if r is None:
            return 1
        if not r.get("ok"):
            print(f"momento: {r.get('error', args.command + ' failed')}", file=sys.stderr)
            return 1
        if args.command == "pause":
            print("Paused. What was buffered can still be saved; `momento resume` starts a fresh replay.")
        else:
            print("Recording resumed (fresh replay buffer).")
        return 0

    if args.command in ("quit", "stop"):
        r = _request({"cmd": "quit"}, timeout=10)
        return 0 if r and r.get("ok") else 1

    parser.error(f"unknown command {args.command}")
    return 2
