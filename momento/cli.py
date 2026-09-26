"""Command line: momento daemon | overlay | save 5m | status | settings | set KEY VALUE | quit."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import __version__, config, durations, quality


def _duration(text: str) -> int:
    try:
        return durations.parse(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from e


# `momento set KEY VALUE`: key -> (config section, config key, parser)
def _choice(options):
    def parse(text: str):
        text = text.lower()
        if text not in options:
            raise ValueError(f"choose one of: {', '.join(options)}")
        return text
    return parse


def _kbps(text: str) -> int:
    value = int(text)
    if value < 0 or 0 < value < 1000:
        raise ValueError("bitrate is in kbps: 0 (automatic) or at least 1000")
    return value


SETTINGS = {
    "resolution": ("capture", "resolution", _choice(list(quality.RESOLUTIONS))),
    "quality": ("capture", "quality", _choice(list(quality.QUALITIES))),
    "bitrate": ("capture", "bitrate_kbps", _kbps),
}


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
    sub.add_parser("settings", help="show video quality settings")
    st = sub.add_parser("set", help="change a setting, e.g. `set resolution 1440p`")
    st.add_argument("key", choices=sorted(SETTINGS))
    st.add_argument("value")
    sub.add_parser("quit", help="stop the daemon")
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
        auto = "" if int(cfg["capture"].get("bitrate_kbps") or 0) else " (automatic)"
        rows = [
            ("resolution", cfg["capture"]["resolution"]),
            ("quality", cfg["capture"]["quality"]),
            ("frame rate", f"{quality.FPS} fps"),
            ("bitrate", f"{kbps / 1000:g} Mbps{auto}"),
            ("disk use", f"about {quality.buffer_gb(kbps, cfg['buffer']['max_seconds']):.1f} GB for the full buffer"),
            ("clips", cfg["output"]["dir"]),
            ("config", cfg["_path"]),
        ]
        for key, value in rows:
            print(f"{key:>10}: {value}")
        return 0

    if args.command == "set":
        section, key, parse = SETTINGS[args.key]
        try:
            value = parse(args.value)
            path = config.set_value(section, key, value, args.config)
        except ValueError as e:
            print(f"momento: {args.key}: {e}", file=sys.stderr)
            return 1
        print(f"{args.key} = {value}  (saved to {path})")
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
        print("Recording restarted with the new setting.")
        return 0

    if args.command == "quit":
        r = _request({"cmd": "quit"}, timeout=10)
        return 0 if r and r.get("ok") else 1

    parser.error(f"unknown command {args.command}")
    return 2
