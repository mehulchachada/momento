"""Momento's log files, and ``momento logs``.

The daemon writes ``~/.local/state/momento/logs/momento.log`` and the clip bar
``bar.log`` next to it (``$XDG_STATE_HOME``), one line per event::

    2026-09-27 12:30:45 daemon INFO pipeline: starting capture: source=portal ...

Each file is rotated at 2 MB and the newest 5 are kept, so they never grow
without bound. Everything still goes to the journal as well
(``journalctl --user -u momento``); ``momento logs`` reads the files and falls
back to the journal. Window titles are never written verbatim (``hidden``).
"""

from __future__ import annotations

import logging
import logging.handlers
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import config

UNIT = "momento.service"
FILES = {"daemon": "momento.log", "bar": "bar.log"}
MAX_BYTES = 2_000_000
BACKUPS = 4                      # + the current file: 5 files per log
FORMAT = "%(asctime)s %(component)s %(levelname)s %(where)s: %(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S"
HANDLER_NAME = "momento-file"
DEFAULT_LINES = 200
_STAMP = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) ")
_SINCE = re.compile(r"\s*(\d+)\s*(s|sec|m|min|h|hr|hour|hours|d|day|days)\s*", re.I)

log = logging.getLogger(__name__)


def log_dir() -> Path:
    """``$XDG_STATE_HOME/momento/logs`` (read now, so tests can point it elsewhere)."""
    return config._xdg("XDG_STATE_HOME", ".local/state") / "momento" / "logs"


def hidden(title) -> str:
    """A window title as logs and reports show it: how long it is, never the words
    (a window can be a browser tab or a chat)."""
    return f"<redacted, {len(str(title))} chars>"


# --- writing -------------------------------------------------------------------------

class _Context(logging.Filter):
    """Adds the columns of a log file line: the process (daemon / bar) and the module."""

    def __init__(self, component: str):
        super().__init__()
        self.component = component

    def filter(self, record: logging.LogRecord) -> bool:
        record.component = self.component
        name = record.name
        record.where = name[len("momento."):] if name.startswith("momento.") else name
        return True


def file_handler() -> logging.Handler | None:
    return next((h for h in logging.getLogger().handlers if h.get_name() == HANDLER_NAME), None)


def setup(component: str, level: int = logging.INFO, directory: Path | None = None) -> Path | None:
    """Also log to this process's file (``component``: "daemon" or "bar"); its path, or None.

    Calling it again replaces the handler. A file that can't be opened only costs
    the file: the journal keeps everything.
    """
    directory = Path(directory) if directory is not None else log_dir()
    path = directory / FILES[component]
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(path, maxBytes=MAX_BYTES, backupCount=BACKUPS,
                                                       encoding="utf-8")
    except OSError as e:
        log.warning("no log file %s: %s", path, e)
        return None
    handler.set_name(HANDLER_NAME)
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(FORMAT, DATEFMT))
    handler.addFilter(_Context(component))
    root = logging.getLogger()
    old = file_handler()
    if old is not None:
        root.removeHandler(old)
        old.close()
    root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > level:
        root.setLevel(level)
    _hook_uncaught()
    return path


_hooked = False


def _to_file(msg: str, exc_info) -> None:
    """An uncaught error, into the file only (Python prints it to stderr, the journal, itself)."""
    handler = file_handler()
    if handler is None:
        return
    handler.handle(logging.LogRecord("momento", logging.ERROR, __file__, 0, msg, None, exc_info))


def _hook_uncaught() -> None:
    """Uncaught errors (main thread and threads) also land in the log file."""
    global _hooked
    if _hooked:
        return
    _hooked = True
    before, before_thread = sys.excepthook, threading.excepthook

    def hook(t, v, tb):
        if not issubclass(t, KeyboardInterrupt):
            _to_file("uncaught error", (t, v, tb))
        before(t, v, tb)

    def thread_hook(args):
        if args.exc_type is not SystemExit:
            name = args.thread.name if args.thread is not None else "?"
            _to_file(f"uncaught error in thread {name}", (args.exc_type, args.exc_value, args.exc_traceback))
        before_thread(args)

    sys.excepthook = hook
    threading.excepthook = thread_hook


# --- reading -------------------------------------------------------------------------

def file_paths(directory: Path | None = None, components=tuple(FILES)) -> list[Path]:
    """The log files that exist, oldest first within each log (momento.log.4 ... momento.log)."""
    directory = Path(directory) if directory is not None else log_dir()
    out = []
    for comp in components:
        name = FILES[comp]
        for p in [directory / f"{name}.{i}" for i in range(BACKUPS, 0, -1)] + [directory / name]:
            if p.is_file():
                out.append(p)
    return out


def read_entries(paths) -> list[tuple[str, str]]:
    """(timestamp, text) per event, in time order across the files. A traceback's
    lines stay with the line that started it."""
    out: list[list[str]] = []
    for p in paths:
        try:
            text = Path(p).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        mine: list[list[str]] = []
        for line in text.splitlines():
            m = _STAMP.match(line)
            if m or not mine:
                mine.append([m.group(1) if m else "", line])
            else:
                mine[-1][1] += "\n" + line
        out.extend(mine)
    out.sort(key=lambda e: e[0])       # stable: one file's order is kept within a second
    return [(s, t) for s, t in out]


def boot_time(stat: str = "/proc/stat") -> datetime | None:
    """When this boot started (the kernel's btime), or None."""
    try:
        for line in Path(stat).read_text().splitlines():
            if line.startswith("btime "):
                return datetime.fromtimestamp(int(line.split()[1]))
    except (OSError, ValueError, IndexError):
        pass
    return None


def parse_since(text: str) -> timedelta:
    """"1h", "30m", "2d", "90s" (also "30 min", "2 days")."""
    m = _SINCE.fullmatch(text or "")
    if not m or int(m.group(1)) <= 0:
        raise ValueError(f"not a time span: {text!r} (try 30m, 1h or 2d)")
    unit = m.group(2).lower()[0]
    return timedelta(seconds=int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit])


def select(entries, start: datetime | None = None, lines: int | None = None) -> list[str]:
    """The entries at or after ``start``, as lines; only the last ``lines`` when given."""
    if start is not None:
        cut = start.strftime(DATEFMT)
        entries = [e for e in entries if e[0] >= cut]
    out = [line for _stamp, text in entries for line in text.split("\n")]
    return out[-lines:] if lines else out


def journal_available() -> bool:
    return shutil.which("journalctl") is not None


def journal_command(boot: str | None = "0", since: timedelta | None = None, lines: int | None = None,
                    follow: bool = False, output: str = "short") -> list[str]:
    cmd = ["journalctl", "--user", "-u", UNIT, "--no-pager", "-q", "--no-hostname", "-o", output]
    if boot is not None:
        cmd += ["-b", boot]
    if since is not None:
        cmd.append(f"--since=-{int(since.total_seconds())}s")
    if lines:
        cmd += ["-n", str(lines)]
    if follow:
        cmd.append("-f")
    return cmd


def follow(paths, out=None, poll: float = 0.5, stop=None, sleep=time.sleep) -> None:
    """Print what gets added to ``paths`` (the current files) until ``stop()`` or Ctrl+C.
    A rotated file (new inode, or shorter than before) is read again from its start."""
    out = out or sys.stdout
    pos: dict[Path, tuple[int, int]] = {}
    for p in paths:
        try:
            st = Path(p).stat()
            pos[Path(p)] = (st.st_ino, st.st_size)
        except OSError:
            pos[Path(p)] = (0, 0)
    partial = {p: "" for p in pos}
    while not (stop and stop()):
        for p, (ino, at) in pos.items():
            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_ino != ino or st.st_size < at:
                at = 0                                  # rotated: the new file from its start
            if st.st_size > at:
                try:
                    with p.open("r", encoding="utf-8", errors="replace") as f:
                        f.seek(at)
                        chunk = f.read()
                        at = f.tell()
                except OSError:
                    continue
                text = partial[p] + chunk
                done, _, partial[p] = text.rpartition("\n")
                if done:
                    out.write(done + "\n")
                    out.flush()
            pos[p] = (st.st_ino, at)
        sleep(poll)


def open_folder(path) -> bool:
    """Show ``path`` in the file manager (xdg-open, else gio); False when neither ran."""
    for cmd in (["xdg-open", str(path)], ["gio", "open", str(path)]):
        if shutil.which(cmd[0]) is None:
            continue
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
        except OSError:
            continue
        threading.Thread(target=proc.wait, daemon=True).start()   # reaped, no zombie
        return True
    return False


def shown_path(path) -> str:
    """``path`` with the home folder written as ~ (for messages)."""
    path, home = str(path), str(Path.home())
    return "~" + path[len(home):] if path == home or path.startswith(home + "/") else path


def main(args) -> int:
    """``momento logs``: the recent log, in plain lines."""
    folder = log_dir()
    if args.open:
        try:
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError:
            pass
        print(shown_path(folder))
        if not open_folder(folder):
            print("momento: couldn't open a file manager (xdg-open is missing); the logs are in the folder above",
                  file=sys.stderr)
            return 1
        return 0
    since = None
    if args.since:
        try:
            since = parse_since(args.since)
        except ValueError as e:
            print(f"momento: {e}", file=sys.stderr)
            return 2
    # Default: the last 200 lines of this boot. --boot: all of this boot; --since: all of
    # that span; --all: everything kept. -n caps any of them.
    lines = args.lines if args.lines else (None if (args.boot or args.all or since) else DEFAULT_LINES)
    paths = file_paths(folder)
    if paths:
        if since is not None:
            start = datetime.now() - since
        elif args.all:
            start = None
        else:
            start = boot_time()
        for line in select(read_entries(paths), start, lines):
            print(line)
        sys.stdout.flush()
        if args.follow:
            try:
                follow([folder / name for name in FILES.values()])
            except KeyboardInterrupt:
                pass
        else:
            print(f"(from {shown_path(folder)} · `momento logs --open` shows the folder)", file=sys.stderr)
        return 0
    if not journal_available():
        print("No Momento logs yet. Momento writes them to "
              f"{shown_path(folder)} while it runs; if you started it in a terminal "
              "(`momento daemon`), its messages are in that terminal.", file=sys.stderr)
        return 1
    boot = None if (args.all or since is not None) else "0"
    cmd = journal_command(boot=boot, since=since, lines=lines, follow=args.follow)
    try:
        if args.follow:
            return subprocess.call(cmd)
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    except KeyboardInterrupt:
        return 0
    except OSError as e:
        print(f"momento: can't read the system log: {e}", file=sys.stderr)
        return 1
    if r.returncode != 0:
        print(f"momento: can't read the system log: {r.stderr.strip() or 'journalctl failed'}", file=sys.stderr)
        return 1
    if not r.stdout.strip():
        print("Nothing from Momento in the system log yet. If you started it in a terminal "
              "(`momento daemon`), its messages are in that terminal.", file=sys.stderr)
        return 0
    sys.stdout.write(r.stdout)
    return 0
