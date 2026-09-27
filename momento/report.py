"""``momento report``: one text file to attach to a GitHub issue.

It is written to the home folder (``~/Momento-report-YYYY-MM-DD_HH-MM.txt``),
where a file picker in the browser opens first, and holds: facts about the PC,
``momento status`` / ``momento settings``, the video format test, Momento's
recent log (journal and log files), graphics driver errors from the kernel log
and the list of recent Momento crashes. Before anything is written it goes
through ``Redactor``: the home folder, user name, computer name, email, IP and
MAC addresses are replaced and window titles are cut down to their length.
Nothing is uploaded. The bar's Settings -> Misc -> Make a report runs the same
``write``.
"""

from __future__ import annotations

import getpass
import ipaddress
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from . import __version__, codecs, config, logs

ISSUE_URL = "https://github.com/mehulchachada/momento/issues/new?template=problem.yml"
JOURNAL_LINES = 1000        # this boot and the one before, together
FILE_LINES = 400            # per log file (momento.log, bar.log)
KERNEL_LINES = 200
CRASH_LINES = 20
PACKAGE_DIR = Path(__file__).resolve().parent

HEADER = """\
Momento problem report, made {when}

What's in this file: your Momento version and settings, basic facts about this PC
(Linux system, desktop, graphics, processor, memory), which video formats work here,
Momento's recent log, graphics driver errors from the kernel log and a list of recent
Momento crashes. Your home folder, user name, computer name, email and IP addresses
are replaced, and window titles are hidden.
Nothing is uploaded; you choose whether to share it.

Attach this file to your issue: {url}
"""


# --- running things --------------------------------------------------------------------

def run(cmd: list[str], timeout: float = 20, env: dict | None = None) -> tuple[int, str, str] | None:
    """(exit code, stdout, stderr); None when the program isn't installed."""
    if shutil.which(cmd[0]) is None:
        return None
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout,
                           stdin=subprocess.DEVNULL, env=env)
    except subprocess.TimeoutExpired:
        return -1, "", f"{cmd[0]} took longer than {timeout:g} s"
    except OSError as e:
        return -1, "", str(e)
    return p.returncode, p.stdout, p.stderr


def _read(root: Path, rel: str) -> str:
    try:
        return (root / rel).read_text(errors="replace")
    except OSError:
        return ""


# --- about this PC -------------------------------------------------------------------

def install_method(package_dir: Path = PACKAGE_DIR, env=os.environ) -> str:
    """How Momento was installed: install.sh (~/.local/share/momento), a distro package,
    Flatpak, or a source checkout."""
    if env.get("FLATPAK_ID") or Path("/.flatpak-info").exists():
        return "Flatpak"
    parent = package_dir.parent
    lib = config._xdg("XDG_DATA_HOME", ".local/share") / "momento"
    if parent == lib or parent == lib.resolve():
        version = _read(parent, "VERSION").strip()
        how = f"install.sh ({version})" if version else "install.sh"
        if Path("/run/.containerenv").exists():
            how += ", inside a container (distrobox)"
        return how
    if str(parent).startswith(("/usr/", "/opt/")):
        return f"distro package ({parent})"
    if (parent / ".git").exists():
        return f"from source ({parent})"
    return f"unknown ({parent})"


def os_release(root: Path) -> str:
    fields = {}
    for line in (_read(root, "etc/os-release") or _read(root, "usr/lib/os-release")).splitlines():
        key, sep, value = line.partition("=")
        if sep:
            fields[key.strip()] = value.strip().strip('"')
    name = fields.get("PRETTY_NAME") or fields.get("NAME") or "unknown"
    version = fields.get("VERSION") or fields.get("VERSION_ID")
    if version and version not in name:
        name += f", {version}"
    if fields.get("VARIANT") and fields["VARIANT"] not in name:
        name += f" ({fields['VARIANT']})"
    return name


def desktop(env=os.environ, run=run) -> str:
    de = env.get("XDG_CURRENT_DESKTOP") or env.get("XDG_SESSION_DESKTOP") or env.get("DESKTOP_SESSION") or "unknown"
    session = env.get("XDG_SESSION_TYPE") or ("wayland" if env.get("WAYLAND_DISPLAY") else
                                              "x11" if env.get("DISPLAY") else "unknown")
    parts = [de, session]
    if env.get("GAMESCOPE_WAYLAND_DISPLAY"):
        parts.append("gamescope")
    version = None
    if "KDE" in de.upper():
        version = _first_line(run(["plasmashell", "--version"], timeout=5))
    elif "GNOME" in de.upper():
        version = _first_line(run(["gnome-shell", "--version"], timeout=5))
    if version:
        parts.append(version)
    return ", ".join(parts)


def _first_line(r) -> str | None:
    if not r or r[0] != 0:
        return None
    lines = r[1].strip().splitlines()
    return lines[0].strip() if lines else None


def cpu_model(root: Path) -> str:
    for line in _read(root, "proc/cpuinfo").splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() in ("model name", "Model", "Hardware"):
            return value.strip()
    return platform.processor() or "unknown"


def ram_total(root: Path) -> str:
    for line in _read(root, "proc/meminfo").splitlines():
        if line.startswith("MemTotal:"):
            try:
                return f"{int(line.split()[1]) / 1024 / 1024:.1f} GB"
            except (ValueError, IndexError):
                break
    return "unknown"


VENDOR_NAMES = {"amd": "AMD", "intel": "Intel", "nvidia": "NVIDIA"}


def gpu_lines(root: Path, run=run, detection: dict | None = None) -> list[str]:
    """One line per GPU (vendor, PCI id, kernel driver, name when lspci knows it), then
    the driver version: the video format test's key, else libva asked now."""
    gpu_list = codecs.gpus(root / "sys/class/drm")
    out = []
    for gpu in gpu_list:
        vendor = VENDOR_NAMES.get(codecs.PCI_VENDORS.get(gpu.get("vendor", "")), gpu.get("vendor") or "?")
        ids = f"{gpu.get('vendor', '')[2:]}:{gpu.get('device', '')[2:]}"
        line = f"{vendor} [{ids}], driver {gpu.get('driver') or '?'}"
        name = _first_line(run(["lspci", "-mm", "-d", ids], timeout=5))
        if name:
            fields = re.findall(r'"([^"]*)"', name)
            if len(fields) >= 3:
                line += f": {fields[1]} {fields[2]}"
        out.append(line)
    if not out:
        out.append("no GPU found in /sys/class/drm")
    key = (detection or {}).get("key") or {}
    driver = key.get("driver")
    if driver is None and gpu_list and root == Path("/"):
        try:
            driver = codecs.driver_version(gpu_list)
        except Exception:  # noqa: BLE001 - libva missing or broken: just unknown
            driver = ""
    out.append(f"driver version: {driver or 'unknown'}")
    if key.get("gst"):
        out.append(f"GStreamer: {key['gst']}")
    return out


def screen_line(root: Path) -> str:
    from .overlay import drm_screen_size   # the size the bar caps Resolution by (no Qt import)

    size = drm_screen_size(root / "sys/class/drm")
    return f"{size[0]}x{size[1]} (largest connected display)" if size else "unknown"


def system_section(root: Path = Path("/"), run=run, env=os.environ, detection: dict | None = None) -> list[str]:
    rows = [
        ("Momento", f"{__version__}, install: {install_method(env=env)}"),
        ("Linux", os_release(root)),
        ("Desktop", desktop(env, run)),
        ("Kernel", platform.release()),
        ("Processor", cpu_model(root)),
        ("Memory", ram_total(root)),
        ("Screen", screen_line(root)),
    ]
    out = [f"{k + ':':<11}{v}" for k, v in rows]
    gpus = gpu_lines(root, run, detection)
    out.append(f"{'Graphics:':<11}{gpus[0]}")
    out += [f"{'':<11}{line}" for line in gpus[1:]]
    return out


# --- Momento itself ------------------------------------------------------------------

def momento_output(args: list[str], config_path=None, run=run) -> list[str]:
    """What ``momento <args>`` prints (run as its own process, as a user would)."""
    cmd = [sys.executable, "-m", "momento"]
    if config_path:
        cmd += ["--config", str(config_path)]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(PACKAGE_DIR.parent), env.get("PYTHONPATH")) if p)
    r = run(cmd + args, timeout=30, env=env)
    if r is None:
        return [f"could not run `momento {' '.join(args)}`"]
    code, out, err = r
    lines = out.rstrip("\n").splitlines() + err.rstrip("\n").splitlines()
    return lines or [f"`momento {' '.join(args)}` printed nothing (exit code {code})"]


def load_detection(path: Path | None = None) -> dict | None:
    try:
        data = json.loads(Path(path or codecs.cache_path()).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def formats_section(data: dict | None) -> list[str]:
    """The cached video format test (formats.json), in words."""
    if not data:
        return ["Not tested yet (Momento tests the video formats when it starts)."]
    try:
        det = codecs.Detection.from_json(data)
    except (TypeError, ValueError, AttributeError):
        return ["The saved test result can't be read."]
    out = []
    if isinstance(data.get("checked"), (int, float)):
        out.append(f"Tested:    {datetime.fromtimestamp(data['checked']):%Y-%m-%d %H:%M}")
    out.append(f"Graphics:  {VENDOR_NAMES.get(det.vendor, det.vendor or 'unknown')}")
    for fmt in codecs.FORMATS:
        hw = det.encoders(fmt)
        if hw:
            how = f"yes ({', '.join(hw)})"
        elif det.available(fmt):
            how = "yes (software)"
        else:
            how = "no"
        out.append(f"{codecs.label(fmt) + ':':<11}{how}")
    out.append(f"Auto:      {codecs.label(det.auto())}")
    tested = ", ".join(f"{n} {'ok' if ok else 'failed'}" for n, ok in sorted(det.works.items()))
    out.append(f"Encoders:  {tested or 'none tested'}")
    return out


# --- logs --------------------------------------------------------------------------

def journal_section(run=run) -> list[str]:
    """The last JOURNAL_LINES lines of momento.service from this boot and the one before."""
    base = logs.journal_command(boot=None, output="short-iso")
    cur = run(base + ["-b", "0", "-n", str(JOURNAL_LINES)])
    if cur is None:
        return ["journalctl isn't available on this system (see the log files below)."]
    if cur[0] != 0:
        return [f"The system log can't be read: {cur[2].strip() or 'journalctl failed'}"]
    now = cur[1].rstrip("\n").splitlines()[-JOURNAL_LINES:]
    before = []
    room = JOURNAL_LINES - len(now)
    if room > 0:
        prev = run(base + ["-b", "-1", "-n", str(room)])
        if prev is not None and prev[0] == 0:
            before = prev[1].rstrip("\n").splitlines()[-room:]
    out = []
    if before:
        out += ["-- previous boot --"] + before
    out += ["-- this boot --"] + (now or ["(nothing from Momento in this boot)"])
    return out


def files_section(directory: Path | None = None) -> list[str]:
    paths = logs.file_paths(directory)
    if not paths:
        return [f"No log files in {logs.shown_path(directory or logs.log_dir())} yet."]
    out = []
    for comp, name in logs.FILES.items():
        mine = logs.file_paths(directory, (comp,))
        if not mine:
            continue
        lines = logs.select(logs.read_entries(mine), None, FILE_LINES)
        out += [f"-- {name} (last {len(lines)} line{'' if len(lines) == 1 else 's'}) --"] + lines
    return out


# Graphics driver trouble in the kernel log: GPU resets and hangs, ring timeouts
# (a stuck encoder shows as "ring vcn_unified_0 timeout"), page faults.
_GPU = r"amdgpu|i915|\bxe\b|nvidia|nouveau|\[drm\]|\bdrm:"
_BAD = r"error|fail|timeout|timed out|reset|fault|hang|\bvcn"
KERNEL_PATTERN = re.compile(rf"ring \S+ timeout|gpu fault|gpu reset|\bVCN\b|(?:{_GPU}).*(?:{_BAD})", re.I)


def kernel_section(run=run) -> list[str]:
    """This boot's kernel lines about the graphics driver; skipped when only root can read them."""
    text = None
    r = run(["journalctl", "-k", "-b", "0", "--no-pager", "-q", "--no-hostname", "-o", "short-iso"])
    if r is not None and r[0] == 0 and r[1].strip():
        text = r[1]
    else:
        d = run(["dmesg", "--time-format", "iso"])
        if d is not None and d[0] == 0 and d[1].strip():
            text = d[1]
    if text is None:
        return ["The kernel log can only be read with admin rights here, so it was skipped."]
    hits = [line for line in text.splitlines() if KERNEL_PATTERN.search(line)]
    return hits[-KERNEL_LINES:] or ["No graphics driver errors in this boot's kernel log."]


def crashes_section(run=run) -> list[str]:
    """Crashes of Momento's processes (the list only, never the dumps)."""
    r = run(["coredumpctl", "list", "--no-pager", f"COREDUMP_USER_UNIT={logs.UNIT}"])
    if r is None:
        return ["coredumpctl isn't available on this system, so crashes can't be listed."]
    lines = [line for line in r[1].rstrip("\n").splitlines() if line.strip()]
    if r[0] != 0 or len(lines) < 2:
        return ["No crashes recorded."]
    return [lines[0]] + lines[1:][-CRASH_LINES:]


# --- privacy -------------------------------------------------------------------------

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?![A-Za-z0-9-])"
                    r"(?<!\.service)(?<!\.socket)(?<!\.target)(?<!\.scope)(?<!\.slice)(?<!\.mount)(?<!\.timer)")
_IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.]*\d)")
_IPV6 = re.compile(r"(?<![\w:])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![\w:])")
_MAC = re.compile(r"(?<![\w:])(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}(?![\w:])")
# Window titles, wherever an older Momento (or `momento status`) wrote one in full.
_TITLES = (
    re.compile(r"^(?P<pre>\s*record: window): (?P<title>.+)$", re.M),                # momento status
    re.compile(r"(?P<pre>recording window): (?P<title>.+)$", re.M),                  # daemon, before 0.1.1
    re.compile(r"(?P<pre>window title from \S+ \S+ restore data): (?P<title>'.*'|\".*\")$", re.M),
)


def _ip(m: re.Match) -> str:
    try:
        ipaddress.ip_address(m.group(0))
    except ValueError:
        return m.group(0)                  # a version number, a clock time: not an address
    return "<ip>"


class Redactor:
    """Replaces what could identify the person: home folder -> ~, user name -> <user>,
    computer name -> <host>, email / IP / MAC addresses, and window titles -> their length."""

    def __init__(self, home: str | None = None, user: str | None = None, host: str | None = None):
        home = home if home is not None else str(Path.home())
        homes = {home.rstrip("/")} if home.rstrip("/") else set()
        for h in list(homes):
            homes.add(os.path.realpath(h))
            if h.startswith("/var/home/"):
                homes.add(h[len("/var"):])            # Fedora Atomic: /home -> /var/home
            elif h.startswith("/home/"):
                homes.add("/var" + h)
        self.homes = sorted((h for h in homes if len(h) > 1), key=len, reverse=True)
        self.user = user if user is not None else _user()
        host = host if host is not None else socket.gethostname()
        self.hosts = sorted({h for h in (host, host.split(".")[0]) if h}, key=len, reverse=True)

    @staticmethod
    def _word(word: str) -> re.Pattern:
        # the name as a whole word: "bazzite" in "bazzite@pc", not in "bazzite-deck" or "Bazzite"
        return re.compile(rf"(?<![\w.-]){re.escape(word)}(?![\w-])")

    def __call__(self, text: str) -> str:
        for pat in _TITLES:
            text = pat.sub(lambda m: f"{m['pre']} (window title: "
                                     f"{logs.hidden(m['title'].strip().strip(chr(39) + chr(34)))})", text)
        for h in self.homes:
            text = re.sub(rf"{re.escape(h)}(?=/|\b|$)", "~", text)
        text = _EMAIL.sub("<email>", text)
        text = _MAC.sub("<mac>", text)
        text = _IPV6.sub(_ip, text)
        text = _IPV4.sub(_ip, text)
        for h in self.hosts:
            if h not in ("localhost", "localhost.localdomain"):
                text = self._word(h).sub("<host>", text)
        if self.user and len(self.user) >= 3:        # a 1-2 letter name would hit ordinary words
            text = self._word(self.user).sub("<user>", text)
        return text


def _user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return os.environ.get("USER", "")


# --- the file ------------------------------------------------------------------------

def default_path(now: datetime | None = None, directory: Path | None = None) -> Path:
    now = now or datetime.now()
    return Path(directory or Path.home()) / f"Momento-report-{now:%Y-%m-%d_%H-%M}.txt"


def build(config_path=None, run=run, now: datetime | None = None, root: Path = Path("/"),
          env=os.environ, log_dir: Path | None = None, redactor: Redactor | None = None,
          detection: dict | None = None) -> str:
    """The whole report, redacted."""
    now = now or datetime.now()
    if detection is None:
        detection = load_detection()
    sections = [
        ("About this PC", lambda: system_section(root, run, env, detection)),
        ("Momento status (momento status)", lambda: momento_output(["status"], config_path, run)),
        ("Momento settings (momento settings)", lambda: momento_output(["settings"], config_path, run)),
        ("Video formats (the test Momento runs on this PC)", lambda: formats_section(detection)),
        ("Momento's log (system journal, this boot and the one before)", lambda: journal_section(run)),
        ("Momento's log files", lambda: files_section(log_dir)),
        ("Graphics driver messages (kernel log, this boot)", lambda: kernel_section(run)),
        ("Momento crashes (coredumpctl)", lambda: crashes_section(run)),
    ]
    parts = [HEADER.format(when=f"{now:%Y-%m-%d %H:%M}", url=ISSUE_URL)]
    for title, make in sections:
        try:
            lines = make()
        except Exception as e:  # noqa: BLE001 - one broken section never costs the report
            lines = [f"(this part failed: {e.__class__.__name__}: {e})"]
        parts.append(f"== {title} ==\n" + "\n".join(lines) + "\n")
    return (redactor or Redactor())("\n".join(parts))


def write(path: Path | None = None, config_path=None, **kw) -> Path:
    """Build the report and save it (home folder by default); its path."""
    now = kw.pop("now", None) or datetime.now()
    path = Path(path) if path else default_path(now)
    text = build(config_path, now=now, **kw)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return path
