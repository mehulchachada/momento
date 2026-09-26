# Momento: developer notes

This page covers how Momento works under the hood, how to hack on it, and how to run the tests. The user-facing docs are in [README.md](../README.md).

## Architecture

```
                  ┌──────────────────────── momento daemon ──────────────────────────┐
 capture source   │                                                                     │
 ─────────────►   │  GStreamer pipeline                                                 │
  portal (PW)     │  src → vapostproc|videoconvert → H.264 enc → h264parse ┐            │
  gamescope (PW)  │                                                        ├→ splitmux  │ ──► ~/.cache/momento/buffer/
  ximagesrc       │  pulsesrc(@DEFAULT_MONITOR@) [+ mic] → AAC enc ────────┘  (mpegts)  │     seg_000123.ts  (10 s each)
  videotestsrc    │                                                                     │
                  │  ring-buffer janitor: keeps the newest max_seconds of footage       │
                  │  GlobalShortcuts portal (Super+Shift+G) ──► toggles the resident bar    │
                  │  child: `momento overlay --resident` (hidden bar, overlay.sock)   │
                  │  IPC server: $XDG_RUNTIME_DIR/momento.sock (JSON lines)           │
                  └─────────────────────────────────────────────────────────────────────┘
                           ▲                                   │ save 5m
   momento overlay ──────┤  (PySide6, layer-shell on KDE/    ▼
   momento save 5m ──────┘   wlroots; frameless topmost    ffmpeg -c copy  ──► ~/Videos/Momento/Momento_…_5m.mp4
   momento status/quit        window elsewhere)
```

### The ring buffer

The daemon records continuously into **10-second MPEG-TS segments** (`buffer.segment_seconds`) through `splitmuxsink` + `mpegtsmux`. The encoder GOP is one second (`key-int-max = fps`, no B-frames), and splitmuxsink requests a keyframe at every segment boundary, so every segment can be decoded on its own and a clip can start on any whole second.

Retention counts **footage, not wall-clock time**: as each segment closes, the oldest segments are deleted once the footage newer than them exceeds `buffer.max_seconds` (at most 3600) plus a 30 s margin, so disk use is bounded at about `(video + audio bitrate) × max_seconds` and an hour of footage stays saveable however long ago it was recorded. `status.buffered` is the summed duration of the segments on disk (capped at `max_seconds`). Segments that closed 30 s or more before the newest one are dropped from the page cache (`posix_fadvise(DONTNEED)`, no fsync), as are the segments an export has just read.

**The buffer persists.** Every closed segment is appended to `<buffer dir>/index.jsonl`:

```
{"file":"seg00000123.ts","start":1790431294.6,"end":1790431304.6,"session":"1790431294-18ab2509","width":1920,"height":1080,"fps":60,"codec":"h264","audio":true}
```

`session` identifies one capture run (a new id on every pipeline start: resume, setting change, error retry, service restart). `width`/`height`/`fps` come from the negotiated caps on `h264parse`'s src pad (the encoder's sink caps as a fallback). Pruning rewrites the index atomically. On daemon and recorder start, `RingBuffer.recover()` reloads the index, drops entries whose file is missing or empty, deletes any `*.ts` the index doesn't know (for example the unfinished segment of a crash), and numbering continues after the highest remaining segment (`splitmuxsink start-index`). So the buffer survives pause/resume, `configure`/`reload`, `systemctl --user restart`, SIGTERM, a crash and a reboot. Only the explicit stop (`{"cmd": "quit"}`, `momento stop|quit`, the bar's Stop button) deletes it, unless it's sent with `"keep_buffer": true` (`momento stop --keep-buffer`).

```
 time ──────────────────────────────────────────────────────────────────────►
        ┌──────┬──────┬──────┬──────┬──────┬──────┬──────┬──────┬────┐
 disk   │ s120 │ s121 │ s122 │ s123 │ s124 │ s125 │ s126 │ s127 │s128│ ← being written
        └──────┴──────┴──────┴──────┴──────┴──────┴──────┴──────┴────┘
          ▲ oldest (deleted beyond max_seconds of footage)          ▲ now
                                         │◄──────── X = 30 s ───────►│
                                    now − X
```

Saving `X` seconds:

1. **Flush.** The daemon forces a keyframe and emits splitmuxsink's `split-now`, so the segment being written is closed and "now" really is now (`Recorder.flush()` in `pipeline.py`).
2. **Select.** `RingBuffer.select_last(X, until=now)` walks the closed segments newest → oldest, adding up their durations until it has `X` seconds, and skips the gaps between sessions (a pause, a restart). The oldest chosen segment gets a start offset. If the sessions it reaches used different stream parameters (`width`, `height`, `fps`, `codec`, `audio`), it stops there and keeps only the newest compatible tail. The save reply then has `"partial": true` and a `"reason"` such as `"earlier footage used a different resolution"`.
3. **Cut.** The selection is split into runs of the same session. Within a run, `exporter.py` byte-concatenates the segments with ffmpeg's `concatf:` protocol. Their TS timestamps are already continuous, and the `concat` demuxer would re-derive them and add audio pre-roll and edit lists. The exporter then snaps the start offset to the nearest keyframe in the first segment (or the first keyframe of the next one) and applies it as an *output* `-ss` with `-c copy -avoid_negative_ts make_zero`. A single run goes straight to the MP4. With several runs, each run is cut this way into a temporary MP4 piece that starts at 0, and the pieces are joined with the `concat` demuxer (`-c copy`), which lays them back to back: the gap between sessions disappears, and audio and video of each piece are shifted together, so they stay in sync. Stream copy means no re-encode, so even 60 minutes saves in seconds. Clips are accurate to about 1 s.

MPEG-TS is used for the buffer because a segment that was cut off (crash, power loss, SIGKILL) is still playable. MP4 is used for output because that's what players and upload sites expect.

### Capture sources (`capture.source`)

| Source | When `auto` picks it | How |
|---|---|---|
| `gamescope` | Session is gamescope (`XDG_CURRENT_DESKTOP`/`GAMESCOPE_WAYLAND_DISPLAY`) and a PipeWire node named `gamescope` exists | `pipewiresrc target-object=gamescope`; no portal needed |
| `portal` | `WAYLAND_DISPLAY` set | `org.freedesktop.portal.ScreenCast` → PipeWire fd + node id → `pipewiresrc`. `persist_mode=2`; the restore token is saved to `~/.local/state/momento/portal_token` |
| `x11` | only `DISPLAY` set | `ximagesrc` |
| `test` | never (explicit only) | `videotestsrc` + `audiotestsrc`, for development/CI |

### Encoders (`capture.encoder = "auto"`)

The first one that GStreamer can instantiate and preroll wins (`ENCODER_ORDER` in `pipeline.py`): `vah264enc` → `vah264lpenc` (VA-API, modern `va` plugin from gst-plugins-bad; fed VAMemory NV12 from `vapostproc` for zero-copy colour conversion) → `vaapih264enc` (legacy gstreamer-vaapi) → `nvh264enc` (NVENC) → `qsvh264enc` → `x264enc` → `openh264enc`. AAC: `avenc_aac` (gst-libav) → `fdkaacenc`. If neither exists, the daemon records without audio and logs a warning.

Distro gotchas:
- **Fedora** builds Mesa with H.264/HEVC encode disabled. `vah264enc` is present but fails to open on AMD unless `mesa-va-drivers-freeworld` (RPM Fusion) is installed. Bazzite ships negativo17 Mesa with H.264 enabled.
- **openSUSE** needs Packman for H.264 VA-API and for gst-libav AAC.
- `ffmpeg-free` (Fedora) is enough for `-c copy` remuxing of H.264/AAC. It doesn't need decoders.

### Hotkey

The daemon registers a `save-replay` shortcut (preferred trigger from `hotkey.trigger`, default `LOGO+SHIFT+g`) through `org.freedesktop.portal.GlobalShortcuts`. For host (non-Flatpak) apps, the portal identifies the app by its **desktop file id**, so `io.github.mehulchachada.Momento.desktop` must be installed under that exact name. Don't rename it. When the shortcut fires, the daemon sends `toggle` to the resident clip bar (see [Overlay](#overlay)); if none answers, or `[ui] keep_bar_loaded` is `false`, it launches `momento overlay` instead. Running `momento overlay` while a bar is open closes it, so the key toggles either way.

### Overlay

A slim PySide6 bar that slides in at the bottom of the screen with the eight clip lengths in a row. On KDE Plasma and wlroots compositors it's a wlr-layer-shell surface on the Overlay layer, created through **LayerShellQt** (driven with ctypes, because there are no Python bindings), so it appears above fullscreen games. Anywhere that fails (GNOME, X11, ...) it falls back to a frameless, always-on-top window, which can't cover *exclusive* fullscreen games. In Steam Gaming Mode, gamescope only composites its own focus window, so the overlay doesn't show there (roadmap).

**How it opens.** By default (`[ui] keep_bar_loaded = true`) the daemon starts `momento overlay --resident` once at startup, as a child process in the service's cgroup. That process builds the bar, configures the layer surface and keeps it hidden. It listens on `$XDG_RUNTIME_DIR/overlay.sock` (0600, the same JSON-lines framing as the daemon's socket) for `toggle`, `show`, `hide`, `quit` and `ping`, each answered with `{"ok": true, "visible": …}`. A show paints the last status it has (with the buffer grown by the time since, if it was recording) and fetches a fresh one in the background, so it never waits on IPC. Offscreen, the first paint lands about 4 ms after `show` is sent, compared with about 180 ms from process start to first paint for a one-shot bar. Every show starts from the state a new bar would have: the clip view, focus on the last length, the keyboard focus ring, a new idle timer, and no leftover "Saved", settings or stop question. Replies from the previous open are dropped (every worker result carries `Bar.gen`). Esc, the idle timeout and the pause after "Saved" hide the bar instead of quitting. Hiding the QWindow destroys the layer surface, and the keyboard interactivity is also set to None while hidden, so a hidden bar never holds the keyboard. If the bar exits, the daemon restarts it after 1, 2, 4 … 60 s (the delay resets once a bar has run for 30 s), but never while the daemon is stopping. A `reload`/`configure` starts or stops it to match `keep_bar_loaded`. The trade-off is memory: offscreen, the hidden bar is about 66 MB RSS (38 MB PSS), rising to about 74 MB after its first show and staying flat over hundreds of show/hide cycles. With `keep_bar_loaded = false`, or while no resident bar answers, each press starts a one-shot `momento overlay` process as before. That takes about 0.3-0.5 s on a real desktop, and it quits when closed. `momento overlay` from a terminal or the app menu toggles the resident bar when there is one.

The gear, pause/play and stop buttons are painted with QPainter (no icon font). Settings open inside the same bar: it grows upward by resizing the window, and LayerShellQt passes the new size on to the bottom-anchored surface. While paused, a one-line hint also grows the bar upward. The overlay talks to the daemon with `settings`/`configure`/`pause`/`resume`/`quit`. When the daemon is off, it reads and writes the config file directly through `momento.settings`, and **Start** runs `systemctl --user start momento.service` (or spawns `momento daemon` when the unit isn't installed).

### IPC

The socket is `$XDG_RUNTIME_DIR/momento.sock`. It carries one JSON object per line, and the daemon sends one JSON reply per line.

```
→ {"cmd": "status"}          ← {"ok": true, "state": "recording", "buffered": 1234.5, "max_seconds": 3600, "source": "portal", "encoder": "vah264enc", "output_dir": "…"}
→ {"cmd": "save", "seconds": 300}   ← {"ok": true, "path": "/home/…/Momento_…_5m.mp4", …}
→ {"cmd": "pause"}           ← {"ok": true, "state": "paused"}
→ {"cmd": "resume"}          ← {"ok": true, "state": "starting"}
→ {"cmd": "reload"}          ← {"ok": true, "restarted": true, "paused": false}
→ {"cmd": "settings"}        ← {"ok": true, "values": {"resolution": "1080p", "quality": "high", "bitrate": 0,
                                  "audio_source": "default", "mic": "off", "mic_device": "default"},
                                "choices": {"resolution": ["720p", …, "native"], "quality": ["standard", "high", "ultra"]},
                                "devices": {"outputs": [{"name": "alsa_output.….monitor", "label": "…", "default": true}],
                                            "inputs": [{"name": "alsa_input.…", "label": "…", "default": true}]},
                                "fps": 60, "max_seconds": 3600, "config": "/home/…/config.toml"}
→ {"cmd": "configure", "changes": {"resolution": "1440p", "mic": "on"}}
                             ← {"ok": true, "changed": {…}, "restarted": true, "paused": false}
                             ← {"ok": false, "error": "resolution: choose one of: …"}
→ {"cmd": "quit"}            ← {"ok": true, "buffer_cleared": true}
→ {"cmd": "quit", "keep_buffer": true}   ← {"ok": true, "buffer_cleared": false}
```

- `pause` stops capture but keeps the ring buffer, so `save` keeps working on what was recorded. `status` then reports `"state": "paused"` and `"recording": false`. `resume` starts a new capture session and keeps the footage from before the pause, so a save can span the pause (the paused time is skipped).
- `quit` stops the daemon and deletes the replay buffer. With `"keep_buffer": true` the footage stays on disk and is saveable after the next start. SIGTERM (`systemctl --user stop/restart`, logout, reboot) always keeps it.
- `settings` and `configure` use the user-facing keys from `momento/settings.py`, which is shared by the CLI (`momento set`), the daemon and the overlay. `audio_source` is `default` (`@DEFAULT_MONITOR@`), `off` (`audio.desktop = false`) or a sink monitor source name. `mic` is `on`/`off`. `mic_device` is `default` (`@DEFAULT_SOURCE@`) or a source name. Devices come from `pactl -f json list sinks/sources`, and the list is empty without `pactl`.
- `configure` validates every value before writing any (`config.set_value`, so comments are kept), then reloads the recorder only if something changed. While paused, the new settings load but capture stays off until `resume`.

See `momento/ipc.py` and `momento/cli.py` for the authoritative protocol.

### Disk space (`momento/storage.py`)

Momento never starts capture unless a full buffer fits. All sizes are bytes; the UI formats them with decimal units (`storage.human`: `7.2 GB`).

- **Full buffer** = (video kbps from `quality.bitrate_kbps` + audio kbps) × 1000/8 × `buffer.max_seconds` × 1.05 mux overhead. Audio (`audio.bitrate_kbps`, 160 by default) counts only when desktop sound or the mic is on; both are mixed into one stream. 1080p High 60 fps for 60 min is 6.75 GB of video, 7.2 GB in all.
- **Required** = full buffer + 1 GiB reserve for the system.
- **Free** = `statvfs(f_bavail × f_frsize)` of the buffer dir (or its nearest existing parent). **Reclaimable** = the size of our own buffer segments, which count as free because the ring's total size is capped by `max_seconds`. A start is allowed when `free + reclaimable >= required`.
- Every recorder start (daemon startup, `resume`, `reload`, `configure`) is checked. If there isn't room, the recorder isn't started. The state becomes `no_storage`, the error says `Not enough free space: needs 8.2 GB, 3.1 GB free`, and one desktop notification is sent. Every 30 s the daemon checks again and starts capture on its own once the space is there, so freeing space is enough.
- While recording, the daemon stops capture when free space drops below 512 MiB. The buffered segments are kept, so saves still work, and the state becomes `no_storage` with the error `Disk almost full: … free`. In this case auto-restart waits for real free space (reclaimable is not counted), because restarting would drop the kept footage. A manual `resume` does count it.
- `save` needs the selected segments' total size + 256 MiB free in the output dir. Otherwise it replies `{"ok": false, "code": "no_storage", "error": "Not enough space to save this clip: needs X, Y free"}`.
- While paused, none of this runs. `status` shows `paused`, and the check happens on `resume`.

Protocol additions (`code: "no_storage"` marks every storage refusal):

```
status    ← {…, "state": "no_storage", "error": "Not enough free space: …",
             "storage": {"ok": false, "free": 3100000000, "required": 8237000000, "reclaimable": 0, "path": "…/buffer"}}
             ("storage" is always present; "ok" answers "would a (re)start fit right now")
settings  ← {…, "storage": {"required": {"1080p/high/60": 8237…, "<resolution>/<quality>/<fps>": …},
                            "current": "1080p/high/60", "free": …, "reclaimable": …, "reserve": 1073741824, "path": "…"}}
             (an option fits when free + reclaimable >= required[key]; audio, buffer length and an explicit bitrate come from the saved config)
configure → {"changes": {…}, "force": false}
          ← {"ok": false, "code": "no_storage", "error": "1440p Ultra needs 20.0 GB free, 9.4 GB available", "storage": {…check…}}
             (only when the change raises the requirement and it doesn't fit; nothing is written. A change that doesn't raise it,
              or "force": true, is saved. If it still doesn't fit, the reply is ok with "restarted": false, "state": "no_storage", "warning": "…")
          ← {"ok": true, "changed": {…}, "restarted": …, "paused": …, "state": "…", "storage": {…}}
reload    ← {"ok": true, "restarted": false, "paused": false, "state": "no_storage", "warning": "Not enough free space: …", "storage": {…}}
resume    ← {"ok": false, "code": "no_storage", "error": "Not enough free space: …", "state": "no_storage", "storage": {…}}
             (resume also retries a start when the state is no_storage and not paused. A refused resume leaves the daemon
              unpaused in no_storage, so it starts by itself once there is room)
save      ← {"ok": false, "code": "no_storage", "error": "Not enough space to save this clip: …"}
```

The CLI shows the same thing. `momento status` and `momento settings` print a `storage:` line (`3.1 GB free, needs 8.2 GB — not enough`). `momento set` goes through `configure` when the daemon is running, so a refusal is fatal. With the daemon off it writes the file and only warns.

### Files

| Path | What |
|---|---|
| `~/.config/momento/config.toml` | user config (see `data/config.example.toml`; defaults in `momento/config.py`) |
| `~/.cache/momento/buffer/` | ring buffer segments + `index.jsonl` (kept across restarts; cleared by `momento stop`) |
| `~/.local/state/momento/portal_token` | ScreenCast restore token |
| `$XDG_RUNTIME_DIR/momento.sock` | IPC socket |
| `~/Videos/Momento/` | saved clips |

## Dev setup

You need the same system packages as a user (see the README install section). Nothing needs pip, and a virtualenv would hide the distro's PyGObject/PySide6. If you use one anyway, create it with `--system-site-packages`.

```bash
git clone https://github.com/mehulchachada/momento.git && cd momento
./install.sh --check                       # what's missing on this machine
```

Run straight from the source tree, without installing anything, using the synthetic test source:

```bash
mkdir -p /tmp/rb-dev && printf '[capture]\nsource = "test"\n[buffer]\ndir = "/tmp/rb-dev/buffer"\n[output]\ndir = "/tmp/rb-dev/clips"\n[hotkey]\nenabled = false\n' > /tmp/rb-dev/config.toml
python3 -m momento --config /tmp/rb-dev/config.toml -v daemon      # terminal 1 (-vv for debug)
python3 -m momento status                                          # terminal 2
python3 -m momento save 15s
python3 -m momento overlay
```

Global options (`-v`, `--config`) go **before** the subcommand.

If the installed service is running, stop it first (`systemctl --user stop momento.service`), since both would use the same socket.

`make` shortcuts: `make install`, `make deps`, `make check`, `make update`, `make uninstall`, `make purge`, `make run`, `make test`, `make lint`, `make clean`.

## Tests

```bash
python3 -m unittest discover -s tests       # or: make test
```

- `tests/test_core.py`: durations, ring-buffer selection, index recovery after a crash, footage-based retention, selection across session gaps, multi-session export (same parameters joined; a resolution change keeps the newest tail), output naming, the CLI, the IPC round trip and the daemon's supervision of the resident bar (both need PyGObject) and the ffmpeg exporter against synthetic TS segments (needs ffmpeg/ffprobe). Tests whose dependencies are missing are skipped.
- `tests/test_overlay_offscreen.py`: renders the overlay against a fake daemon, and drives a resident bar over its control socket (toggle/show/hide, state reset between opens, no focus while hidden, the fallback without a resident bar). Run it with `QT_QPA_PLATFORM=offscreen python3 -m unittest tests.test_overlay_offscreen`. It saves screenshots to `$MOMENTO_SHOT_DIR`.
- `tests/test_pipeline_live.py`: records a few seconds from the `test` source and cuts a clip. It's skipped without GStreamer. Run it with `python3 tests/test_pipeline_live.py` or pytest.

## Installer

`install.sh` is idempotent and does four things: checks which dependencies are missing, installs the missing distro packages (after showing the exact command and asking; `sudo` only for that step, never for anything else), installs Momento for the user, and enables the systemd user unit. The user part copies `momento/` to `~/.local/share/momento/` and writes a launcher to `~/.local/bin/momento`. That launcher pins the **system** Python (`/usr/bin/python3`), because Homebrew/pyenv Pythons can't import the distro's `gi`/`PySide6`; override it with `MOMENTO_PYTHON=`. Where the distro has no PySide6 package (Ubuntu 24.04), PySide6-Essentials from PyPI goes into `~/.local/share/momento/venv` (created with `--system-site-packages`, so it still sees the distro's `gi` and `dbus`) and the launcher uses that. Inside a distrobox, the launcher re-enters the box when it's started from the host.

Run through `curl | bash`, or with `--update`, it downloads the source tarball of `main` (or `MOMENTO_REF`) to a temp dir and re-executes the `install.sh` inside it. A copy of the installer is kept at `~/.local/share/momento/install.sh` for later `--update`/`--uninstall`.

Test it without touching your real home directory or your running recorder:

```bash
H=$(mktemp -d); env HOME=$H XDG_DATA_HOME= XDG_CONFIG_HOME= XDG_CACHE_HOME= XDG_STATE_HOME= MOMENTO_NO_SYSTEMD=1 ./install.sh --no-deps
```

Test hooks: `MOMENTO_OS_RELEASE=<file>` fakes the distro, `MOMENTO_TARBALL_URL=file:///...` feeds the bootstrap a local tarball.

The package step runs as root in a throwaway container, which is how the lists below were verified (Fedora 44, Ubuntu 24.04, Debian 13, Arch, openSUSE Tumbleweed; 2026-09):

```bash
podman run --rm -v "$PWD":/src:ro registry.fedoraproject.org/fedora:44 bash -c 'cp -r /src /tmp/m && /tmp/m/install.sh --deps-only --yes'
```

### Package names

Required ones are in the first block of the installer's command, optional ones (clip bar over fullscreen games, the sound device menu, GPU encoder drivers, software H.264 fallback) in a second one that is allowed to fail.

| Need | Fedora | Arch | Debian 13 / Ubuntu | openSUSE Tumbleweed |
|---|---|---|---|---|
| PyGObject + Gst/GstVideo typelibs | python3-gobject, gstreamer1, gstreamer1-plugins-base | python-gobject, gstreamer, gst-plugins-base-libs | python3-gi, gir1.2-gstreamer-1.0, gir1.2-gst-plugins-base-1.0, gstreamer1.0-tools | python3-gobject, typelib-1_0-Gst-1_0, typelib-1_0-GstVideo-1_0, gstreamer-utils |
| dbus-python | python3-dbus | python-dbus | python3-dbus | python3-dbus-python |
| PySide6 | python3-pyside6 | pyside6 | python3-pyside6.qt{core,gui,widgets} (Ubuntu 24.04: none, PyPI venv + python3-venv) | python3-pyside6 |
| pipewiresrc | pipewire-gstreamer | gst-plugin-pipewire | gstreamer1.0-pipewire | gstreamer-plugin-pipewire |
| splitmuxsink, pulsesrc | gstreamer1-plugins-good | gst-plugins-good | gstreamer1.0-plugins-good, gstreamer1.0-pulseaudio | gstreamer-plugins-good |
| mpegtsmux, h264parse, va, nvcodec | gstreamer1-plugins-bad-free | gst-plugins-bad, gst-plugin-va | gstreamer1.0-plugins-bad | gstreamer-plugins-bad |
| AAC (avenc_aac) | gstreamer1-plugin-libav | gst-libav | gstreamer1.0-libav | gstreamer-plugins-libav |
| ffmpeg + ffprobe | ffmpeg-free | ffmpeg | ffmpeg | ffmpeg |
| software H.264 (optional) | gstreamer1-plugin-openh264 | gst-plugins-ugly (x264) | gstreamer1.0-plugins-ugly (x264) | openh264 via the default Open H.264 repo |
| VA-API driver, AMD (optional) | mesa-va-drivers-freeworld (RPM Fusion) | part of mesa | mesa-va-drivers | Mesa-libva (H.264 needs Packman) |
| VA-API driver, Intel (optional) | libva-intel-media-driver | intel-media-driver | intel-media-va-driver | intel-media-driver |
| layer-shell (optional) | layer-shell-qt | layer-shell-qt | layer-shell-qt (Debian 13; Ubuntu 24.04 only has the Qt 5 build) | layer-shell-qt6 |
| pactl (optional) | pulseaudio-utils | libpulse | pulseaudio-utils | pulseaudio-utils |
