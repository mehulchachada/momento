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
                  │  ring-buffer janitor: deletes segments older than max_seconds       │
                  │  GlobalShortcuts portal (Super+G) ──► spawns `momento overlay`    │
                  │  IPC server: $XDG_RUNTIME_DIR/momento.sock (JSON lines)           │
                  └─────────────────────────────────────────────────────────────────────┘
                           ▲                                   │ save 5m
   momento overlay ──────┤  (PySide6, layer-shell on KDE/    ▼
   momento save 5m ──────┘   wlroots; frameless topmost    ffmpeg -c copy  ──► ~/Videos/Momento/Momento_…_5m.mp4
   momento status/quit        window elsewhere)
```

### The ring buffer

The daemon records continuously into **10-second MPEG-TS segments** (`buffer.segment_seconds`) through `splitmuxsink` + `mpegtsmux`. The encoder GOP is one second (`key-int-max = fps`, no B-frames), and splitmuxsink requests a keyframe at every segment boundary, so every segment can be decoded on its own and a clip can start on any whole second. Segments older than `buffer.max_seconds` (at most 3600) are deleted as new ones close, so disk use is bounded at about `(video + audio bitrate) × max_seconds`.

```
 time ──────────────────────────────────────────────────────────────────────►
        ┌──────┬──────┬──────┬──────┬──────┬──────┬──────┬──────┬────┐
 disk   │ s120 │ s121 │ s122 │ s123 │ s124 │ s125 │ s126 │ s127 │s128│ ← being written
        └──────┴──────┴──────┴──────┴──────┴──────┴──────┴──────┴────┘
          ▲ oldest (deleted when older than max_seconds)            ▲ now
                                         │◄──────── X = 30 s ───────►│
                                    now − X
```

Saving `X` seconds:

1. **Flush.** The daemon forces a keyframe and emits splitmuxsink's `split-now`, so the segment being written is closed and "now" really is now (`Recorder.flush()` in `pipeline.py`).
2. **Select.** `ringbuffer.py` picks the closed segments that overlap `[now − X, now]`, using each segment's recorded start time.
3. **Cut.** `exporter.py` byte-concatenates them with ffmpeg's `concatf:` protocol. Their TS timestamps are already continuous, and the `concat` demuxer would re-derive them and add audio pre-roll and edit lists. The exporter then snaps the start offset to the nearest keyframe in the first segment and applies it as an *output* `-ss` with `-c copy -avoid_negative_ts make_zero`. Stream copy means no re-encode, so even 60 minutes saves in seconds. Clips are accurate to about 1 s.

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

The daemon registers a `save-replay` shortcut (preferred trigger from `hotkey.trigger`, default `LOGO+g`) through `org.freedesktop.portal.GlobalShortcuts`. For host (non-Flatpak) apps, the portal identifies the app by its **desktop file id**, so `io.github.mehulchachada.Momento.desktop` must be installed under that exact name. Don't rename it. When the shortcut fires, the daemon launches `momento overlay`. Running `momento overlay` while an overlay is open closes it, so the key toggles.

### Overlay

A slim PySide6 bar that slides in at the top of the screen with the eight clip lengths in a row. On KDE Plasma and wlroots compositors it's a wlr-layer-shell surface on the Overlay layer, created through **LayerShellQt** (driven with ctypes, because there are no Python bindings), so it appears above fullscreen games. Anywhere that fails (GNOME, X11, ...) it falls back to a frameless, always-on-top window, which can't cover *exclusive* fullscreen games. In Steam Gaming Mode, gamescope only composites its own focus window, so the overlay doesn't show there (roadmap).

### IPC

The socket is `$XDG_RUNTIME_DIR/momento.sock`. It carries one JSON object per line, and the daemon sends one JSON reply per line.

```
→ {"cmd": "status"}          ← {"ok": true, "state": "recording", "buffered": 1234.5, "max_seconds": 3600, "source": "portal", "encoder": "vah264enc", "output_dir": "…"}
→ {"cmd": "save", "seconds": 300}   ← {"ok": true, "path": "/home/…/Momento_…_5m.mp4", …}
→ {"cmd": "quit"}            ← {"ok": true}
```

See `momento/ipc.py` and `momento/cli.py` for the authoritative protocol.

### Files

| Path | What |
|---|---|
| `~/.config/momento/config.toml` | user config (see `data/config.example.toml`; defaults in `momento/config.py`) |
| `~/.cache/momento/buffer/` | ring buffer segments |
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

`make` shortcuts: `make run`, `make test`, `make lint`, `make install`, `make enable`, `make uninstall`, `make clean`.

## Tests

```bash
python3 -m unittest discover -s tests       # or: make test
```

- `tests/test_core.py`: durations, ring-buffer selection, output naming, the CLI, the IPC round trip (needs PyGObject) and the ffmpeg exporter against synthetic TS segments (needs ffmpeg/ffprobe). Tests whose dependencies are missing are skipped.
- `tests/test_overlay_offscreen.py`: renders the overlay against a fake daemon. Run it with `QT_QPA_PLATFORM=offscreen python3 -m unittest tests.test_overlay_offscreen`. It saves screenshots to `$MOMENTO_SHOT_DIR`.
- `tests/test_pipeline_live.py`: records a few seconds from the `test` source and cuts a clip. It's skipped without GStreamer. Run it with `python3 tests/test_pipeline_live.py` or pytest.

## Installer

`install.sh` is user-level only and idempotent. It copies `momento/` to `~/.local/share/momento/` and writes a launcher to `~/.local/bin/momento`. That launcher pins the **system** Python (`/usr/bin/python3`), because Homebrew/pyenv Pythons can't import the distro's `gi`/`PySide6`; override it with `MOMENTO_PYTHON=`. The installer also installs the desktop file and the systemd user unit, and seeds the config. To test it without touching your real home directory:

```bash
H=$(mktemp -d); env HOME=$H XDG_DATA_HOME= XDG_CONFIG_HOME= XDG_CACHE_HOME= MOMENTO_NO_SYSTEMD=1 ./install.sh
```

### Package names

These are best-effort per distro. The ones marked (?) are unverified, so corrections are welcome:

| Need | Fedora | Arch | Debian/Ubuntu | openSUSE |
|---|---|---|---|---|
| PyGObject + Gst typelib | python3-gobject, gstreamer1 | python-gobject, gstreamer | python3-gi, gir1.2-gstreamer-1.0, gir1.2-gst-plugins-base-1.0 | python3-gobject, typelib-1_0-Gst-1_0 |
| dbus-python | python3-dbus | python-dbus | python3-dbus | python3-dbus-python |
| PySide6 | python3-pyside6 | pyside6 | python3-pyside6.qt{core,gui,widgets} | python3-pyside6 |
| pipewiresrc | pipewire-gstreamer | gst-plugin-pipewire | gstreamer1.0-pipewire | gstreamer-plugin-pipewire |
| splitmuxsink, pulsesrc | gstreamer1-plugins-good | gst-plugins-good | gstreamer1.0-plugins-good, gstreamer1.0-pulseaudio | gstreamer-plugins-good |
| mpegtsmux, h264parse, va | gstreamer1-plugins-bad-free | gst-plugins-bad, gst-plugin-va | gstreamer1.0-plugins-bad | gstreamer-plugins-bad (Packman for codecs) |
| AAC | gstreamer1-plugin-libav | gst-libav | gstreamer1.0-libav | gstreamer-plugins-libav (Packman) |
| ffmpeg | ffmpeg-free / ffmpeg (RPM Fusion) | ffmpeg | ffmpeg | ffmpeg (Packman) (?) |
| layer-shell (optional) | layer-shell-qt | layer-shell-qt | layer-shell-qt (?) | layer-shell-qt6 (?) |
