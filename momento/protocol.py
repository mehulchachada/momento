"""The Momento control protocol: the committed, machine-readable contract.

This module is the source of truth for how clients (the ``momento`` CLI, the
clip bar, a future C++/Rust/QML client) talk to the recorder daemon, and for
what a replacement daemon must answer. It is pure data plus two validators, has
no dependencies, and does not touch sockets. The daemon does not import it (yet);
``tests/test_protocol.py`` holds the real daemon to it. A longer prose version
may exist locally as ``docs/PROTOCOL.md`` (not committed); where they differ,
this module wins.

Transport
---------
* Unix domain ``SOCK_STREAM`` socket at ``$XDG_RUNTIME_DIR/momento.sock``
  (``/tmp/momento-<uid>/momento.sock`` when ``XDG_RUNTIME_DIR`` is unset). The
  directory is created ``0700``; the socket file is ``0600`` from the moment it
  is bound (bind under umask ``0177``, then chmod).
* UTF-8 JSON, one object per line, ``\n``-terminated. Exactly one reply line
  per request line, in order. A connection may carry any number of requests;
  the server reads the next line only after replying (so pipelining is safe and
  replies stay ordered); separate connections are served concurrently.
* Request lines are at most ``MAX_REQUEST_BYTES`` (1 MiB). Replies have no
  fixed cap (clients should accept at least 1 MiB). The server has no read
  timeout; clients pick their own (reference: status 5-10 s,
  pause/resume/configure 30-60 s, stop/quit 10 s, save 120 s).
* Not running = the socket file is missing or ``connect()`` is refused.
* Socket ownership: on start the server probes an existing path with
  ``connect()``; if something answers it must exit without touching it,
  otherwise the stale file may be unlinked. On shutdown it unlinks the path
  only if the inode is still the one it bound. Clients never unlink it.

Messages
--------
* Request: an object with a string ``cmd``; other fields per command
  (``COMMANDS[cmd]["request"]``). Servers ignore unknown request fields.
* Reply: an object with a boolean ``ok``. ``ok: true`` carries the fields in
  ``COMMANDS[cmd]["reply"]``; clients must ignore fields they do not know.
  ``ok: false`` carries ``error`` (one human-readable line, shown as is; never
  parsed by clients), optionally ``code`` (one of ``ERROR_CODES``) and the
  command's extras in ``COMMANDS[cmd]["error"]``.
* A line that is not JSON / not UTF-8 / not an object ->
  ``{"ok": false, "error": "bad request: ..."}``; the connection stays usable.
* Unknown or missing ``cmd`` -> ``{"ok": false, "error": "unknown command '<cmd>'"}``.
* An internal failure while handling -> ``{"ok": false, "error": "<message>"}``.
* Units: durations in seconds, sizes in bytes (integers), bitrates in kbps
  (1 kbps = 1000 bit/s), paths absolute.

Versioning
----------
``PROTOCOL_VERSION`` (1). The ``status`` reply is required by the spec to carry
``"protocol": 1``; the reference daemon does not send it yet (a client that sees
no ``protocol`` assumes 1). Compatible changes keep the version: new commands,
new optional request fields, new reply fields, new codes, new states (clients
treat an unknown state like ``error`` and an unknown code like a plain error).
Removing/renaming/retyping anything, or changing units or meaning, bumps it.
A client needing a newer command gets ``unknown command`` from an older daemon
and may fall back (the clip bar sends ``quit`` when ``stop`` is unknown).

States
------
See ``STATES``. Reported with precedence ``stopped`` > ``paused`` > capture
state. Transitions: daemon start -> ``starting`` (or ``no_storage`` if a full
buffer does not fit; window mode: ``stopped`` until the first ``resume``);
``starting`` -> ``recording`` on the first segment;
``starting``/``recording`` -> ``error`` on failure (transient failures retry
back to ``starting``, fatal ones - no encoder, screen-share refused - stay until
``reload``/``configure``); ``recording`` -> ``no_storage`` when free space drops
below 512 MiB; ``no_storage`` -> ``starting`` when space appears (checked every
30 s) or on ``resume``; any -> ``paused`` (``pause``); any -> ``stopped``
(``stop``); ``paused``/``stopped`` -> ``starting`` or ``no_storage``
(``resume``); ``reload``/``configure`` restart capture unless paused/stopped.
Window mode (``status.target`` ``"window"``): when the recorded window closes,
``recording`` -> ``stopped`` with ``stop_reason: "window_closed"``, exactly as
if ``stop`` was sent (history cleared unless ``keep_history``). A dismissed
picker (or no window to restore) goes back to ``paused`` when the session
already has footage, else ``stopped``. None of it is retried automatically.
``no_window`` stays in ``STATES`` for older daemons, which reported these
cases as ``no_window``; clients treat it like ``stopped``.
``status.recording`` is the authoritative "frames are being written" flag.

Commands (fields are in ``COMMANDS``)
-------------------------------------
``status``
    What the recorder is doing; cheap, clients poll it. ``buffered`` = seconds of
    footage on disk (footage, not wall clock; 0 after ``stop`` unless
    ``keep_history``). ``source`` / ``encoder`` are null until capture has
    started once. ``error`` is present in state ``error``/``no_storage``
    (``no_window`` on older daemons). ``storage`` is a ``STORAGE_CHECK``;
    its ``low`` flag (any state) means a full ``max_seconds`` of recording at the
    current settings (plus, with ``keep_history``, the span saved to the output
    folder) doesn't fit: clients show a warning built from ``needed``,
    ``available`` and ``label``. The daemon also sends one desktop notification
    when ``low`` starts (not while capture is blocked, which has its own).
    ``target`` is what is recorded: ``"screen"`` or ``"window"`` (absent: screen);
    ``target_name`` the picked window's title (null when unknown or full screen).
    ``stop_reason`` (``STOP_REASONS`` or null) says why it is ``stopped``;
    ``keep_history`` whether a stop keeps the replay.
``save`` {seconds}
    The newest ``seconds`` (1-3600; an integer, or a string such as ``"90"``,
    ``"15s"``, ``"5m"``, ``"1h"``) of *recorded footage* ending at the request,
    skipping gaps (pause/restart/reboot), as one MP4. If capture is running the
    daemon first flushes the open segment, so the reply comes after flush +
    export (allow ~120 s). ``partial`` = more than 1.5 s shorter than
    ``requested``; ``reason`` explains a known cause (older footage with other
    width/height/fps/codec/audio is never mixed in). Works in any state while
    footage exists. Errors: ``bad duration: ...``; ``nothing recorded yet``;
    ``code: no_storage`` when the output dir lacks the clip size + 256 MiB.
``screenshot``
    Save one frame of the recording as a PNG in ``<output dir>/Images``
    (``Momento_<date>_<time>.png``, ``_2``, ``_3``... on collision; never
    overwrites). The frame is the first one *captured after the request*, as
    the encoder gets it: what is recorded (the picked window in window mode,
    the screen otherwise) at the recording's resolution. So a client that hides
    itself before asking (the clip bar) is not in the picture. Reply ``path``,
    ``width``, ``height``; the daemon also shows a desktop notification. Only
    while recording: otherwise ``code: not_recording``; ``code: no_storage``
    when the output dir has less than 256 MiB free.
``pause``
    Stop capture, keep the buffer (saves still work). Idempotent. Reply
    ``state: "paused"``.
``resume``
    Play. From ``paused`` it continues (window mode: the stored window is
    restored, or the portal asks); from ``stopped`` it starts a new session (the
    hour marks count from zero; window mode forgets the stored window, so the
    picker opens). Earlier footage stays. Also retries from
    ``no_storage``/``error``. A no-op in other states. Refusal: ``code: no_storage`` with ``state`` and
    ``storage``; the daemon is then unpaused in ``no_storage`` and starts by
    itself once there is room.
``pick_window``
    Window mode only: forget the stored window and start a new capture session
    that opens the desktop's window picker (the one request meant to open it).
    Leaves ``paused``/``stopped``/``no_window``; earlier footage stays. Reply
    ``state`` (usually ``starting``). Errors: ``Record is set to Full screen...``
    in screen mode; ``code: no_storage`` as for ``resume``.
``stop``
    End recording but keep the service and hotkey running (``resume`` starts a
    new session). Clears the replay history unless ``keep_history`` is on
    (``[buffer] keep_history``; then saves keep working). Reply
    ``state: "stopped"``, ``buffer_cleared``.
``quit`` {keep_buffer?}
    Shut the daemon down (reply first, exit ~100 ms later, status 0, so
    ``Restart=on-failure`` does not bring it back). Deletes the buffer unless
    ``keep_buffer`` is true. A signal (SIGTERM, logout, reboot) always keeps it.
``reload``
    Re-read config.toml and restart capture (footage kept). While paused/stopped
    the settings load but capture stays off. ``warning`` = loaded but not enough
    space (state ``no_storage``). Error ``config not applied: ...`` keeps the old
    settings.
``settings``
    Everything a settings UI needs, read from the *saved* file: ``values``
    (``SETTING_VALUES``), ``choices``, ``devices`` (outputs are monitor sources;
    empty without pactl), ``fps``, ``max_seconds``, ``config`` path and
    ``storage`` (``STORAGE_REQUIREMENTS``: bytes per ``"<res>/<quality>/<fps>"``).
    ``controller_available`` is false when python-evdev is missing (the
    controller values are then saved but unused). ``tabs`` groups the keys for
    a UI: ``[[tab name, [keys...]], ...]`` (``settings.TABS``).

Game controllers use no IPC of their own: the daemon watches for the
``[controller] open_chord`` and acts like the hotkey (toggles the bar); the
open bar reads the controllers itself and releases them when it hides.
``configure`` {changes, force?}
    Validate every value first (all or nothing), write the changed ones to
    config.toml keeping comments, reload if anything changed (``changed: {}``
    = nothing to do). Changes that only touch ``controller`` /
    ``controller_exclusive`` / ``controller_open`` / ``keep_history`` /
    ``hour_warning`` / ``instant_bar`` apply without a reload
    (``restarted: false``);
    ``changed`` holds the value read back (``"on"`` -> the shortcut it enables). Refused with ``code: no_storage`` (nothing written) only
    when the new settings do not fit AND raise the requirement over the saved
    ones AND ``force`` is not true; so shrinking always works. With ``force``
    the reply is ok with ``restarted: false``, ``state: "no_storage"`` and a
    ``warning``.

Storage math: full buffer = (video kbps + audio kbps, audio counted only when
desktop sound or the mic is on) * 1000 / 8 * max_seconds * 1.05; required =
full buffer + 1 GiB reserve; a start fits when free + reclaimable >= required,
where reclaimable = bytes of our own buffer segments.

Clip bar control socket
-----------------------
``$XDG_RUNTIME_DIR/overlay.sock``, owned by the resident clip-bar process (not
the daemon); same mode, framing and ownership rules, 64 KiB max line (a longer
one closes the connection). Commands in ``CLIP_BAR_COMMANDS``: ``toggle``,
``show``, ``hide``, ``quit`` (exit the bar process), ``ping``/``status`` (no-op).
Every success reply is ``{"ok": true, "visible": bool, "pid": int}``.

Files other implementations must stay compatible with
-----------------------------------------------------
* ``$XDG_CONFIG_HOME/momento/config.toml``: keys and defaults in
  ``momento/config.py`` ``DEFAULTS``; edits replace single lines, keep comments.
* Buffer dir (``buffer.dir``): segments ``seg%08d.ts`` (MPEG-TS, H.264 GOP 1 s,
  each decodable alone), numbering continues after the highest kept one;
  ``index.jsonl`` holds one object per closed segment,
  ``INDEX_RECORD`` fields, appended on close, rewritten atomically via
  ``.index.jsonl.tmp`` on prune; readers skip unparsable lines, missing/empty
  files and duplicates; unindexed ``*.ts`` are deleted on start.
* Clips: ``output.filename`` template with ``{date}`` (YYYY-MM-DD), ``{time}``
  (HH-MM-SS), ``{length}`` (``15s``/``5m``/``3m20s``); ``/`` -> ``_``, ``.mp4``
  appended if missing, ``_2``, ``_3``... on collision.
* ``$XDG_STATE_HOME/momento/portal_token``: the ScreenCast restore token for
  the full screen, one line, replaced atomically after each session;
  ``portal_token_window`` the same for window mode (deleted when that window
  closes, the picker is dismissed, or on ``pick_window``).

Schema conventions
------------------
Types are JSON type names (``"string" "integer" "number" "boolean" "null"
"object" "array"``, plus ``"any"``); ``"integer"`` excludes booleans,
``"number"`` is int or float. A type may also be a nested schema (dict of
field -> Field: an object matching it) or ``MapOf(type)`` (an object with
arbitrary string keys whose values have that type). A Field is
``(types, required)``: for a reply, *required* means "always present when
``ok`` is true"; for a request, the client must send it.
"""

from __future__ import annotations

PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 1 << 20  # daemon socket; the clip-bar socket allows 64 KiB
CLIP_BAR_MAX_REQUEST_BYTES = 1 << 16

# Every value `state` can take in a status/pause/resume/reload/configure reply.
STATES = {
    "starting": "capture is being set up (pipeline built, waiting for the first segment)",
    "recording": "capture is running and segments are being written",
    "paused": "capture stopped by `pause`; the buffer is kept and saveable",
    "stopped": ("capture stopped by `stop` or the recorded window closing (see stop_reason), or window "
                "mode before the first play; the buffer was cleared unless keep_history; the service keeps running"),
    "no_storage": "capture blocked: a full buffer does not fit on disk (or the disk ran low)",
    "no_window": ("older daemons only (window mode: the picked window closed or none is picked); "
                  "treat like `stopped`"),
    "error": "capture failed; `error` says why (retried automatically unless fatal)",
}

# Values of `stop_reason` in a status reply (null when not stopped by either).
STOP_REASONS = {
    "user": "`stop` (the bar's Stop, `momento stop`)",
    "window_closed": "window mode: the recorded window closed",
}

# Values of `code` in an error reply ({"ok": false, "code": ..., "error": ...}).
ERROR_CODES = {
    "no_storage": "not enough disk space for a full buffer (configure/resume/pick_window) or for the clip "
                  "(save, screenshot)",
    "not_recording": "screenshot: capture isn't running (paused, stopped, starting or failed)",
}


class MapOf:
    """An object with arbitrary string keys whose values all have ``value_type``."""

    def __init__(self, value_type: str):
        self.value_type = value_type

    def __repr__(self) -> str:
        return f"MapOf({self.value_type!r})"


Field = tuple  # (tuple of types, required: bool)

# --- shared objects -----------------------------------------------------------------

STORAGE_CHECK = {
    "ok": (("boolean",), True),          # would a (re)start of capture fit right now
    "free": (("integer",), True),        # bytes available on the buffer's filesystem
    "required": (("integer",), True),    # bytes a full buffer + 1 GiB reserve needs
    "reclaimable": (("integer",), True),  # bytes of our own buffer (counts as free for a restart)
    "path": (("string",), True),         # the buffer directory
    # Low-storage warning (absent on older daemons): does a full buffer.max_seconds of
    # recording at the current settings fit, with keep_history's saved span included?
    "low": (("boolean",), False),        # available < needed: warn (capture keeps running while ok)
    "needed": (("integer",), False),     # required, + one saved span when keep_history (same disk)
    "available": (("integer",), False),  # free + reclaimable (disk "output": that disk's free)
    "history": (("boolean",), False),    # keep_history's saved span is counted in needed
    "disk": (("string",), False),        # which disk needed/available describe: "buffer" | "output"
    "label": (("string",), False),       # the settings in words: "1080p High", "1080p High 120 fps"
}

STORAGE_REQUIREMENTS = {
    "required": ((MapOf("integer"),), True),  # "<resolution>/<quality>/<fps>" -> bytes
    "current": (("string",), True),            # key of the saved settings
    "free": (("integer",), True),
    "reclaimable": (("integer",), True),
    "reserve": (("integer",), True),           # bytes always left free (1 GiB)
    "path": (("string",), True),
}

SETTING_VALUES = {
    "record": (("string",), True),         # "screen" | "window"
    "resolution": (("string",), True),
    "quality": (("string",), True),
    "fps": (("integer",), True),
    "bitrate": (("integer",), True),       # video kbps, 0 = automatic
    "audio_source": (("string",), True),   # "default" | "off" | monitor source name
    "mic": (("string",), True),            # "on" | "off"
    "mic_device": (("string",), True),     # "default" | source name
    # "off" | a preset ("view_menu", "left_paddle", "right_paddle", "l3_r3") | buttons
    # joined with "+" ("select+mode"); configure also takes "on" (enable, keep the shortcut)
    "controller": (("string",), False),
    "controller_exclusive": (("string",), False),   # "on" | "off"
    # "hold" ([controller] hold_ms above 0, default 300) | "tap" (hold_ms = 0: opens on press)
    "controller_open": (("string",), False),
    "keep_history": (("string",), False),   # "off" | "on": keep the replay on stop, save each hour
    "hour_warning": (("integer",), False),  # minutes before the hour mark to warn: 3-10 (UI: 10, 5, 3)
    "instant_bar": (("string",), False),    # "on" | "off": keep the clip bar loaded ([ui] keep_bar_loaded)
}

SETTING_CHOICES = {
    "record": (("array",), True),
    "resolution": (("array",), True),
    "quality": (("array",), True),
    "fps": (("array",), True),
    "controller": (("array",), False),     # ["off", <preset keys>]
    "controller_open": (("array",), False),  # ["hold", "tap"]
    "keep_history": (("array",), False),   # ["off", "on"]
    "hour_warning": (("array",), False),   # [10, 5, 3]
    "instant_bar": (("array",), False),    # ["on", "off"]
}

AUDIO_DEVICES = {
    "outputs": (("array",), True),  # [{"name", "label", "default"}], name = monitor source
    "inputs": (("array",), True),
}

AUDIO_DEVICE = {
    "name": (("string",), True),
    "label": (("string",), True),
    "default": (("boolean",), True),
}

# Keys `configure.changes` accepts (the user-facing settings of momento/settings.py).
SETTING_KEYS = tuple(SETTING_VALUES)

# --- commands -------------------------------------------------------------------

# Fields of every error reply. Commands add their own extras under "error".
ERROR_REPLY = {
    "ok": (("boolean",), True),
    "error": (("string",), True),
    "code": (("string",), False),
}

COMMANDS: dict[str, dict] = {
    "status": {
        "request": {},
        "reply": {
            "ok": (("boolean",), True),
            "state": (("string",), True),
            "recording": (("boolean",), True),
            "buffered": (("number",), True),        # seconds of footage on disk
            "buffered_live": (("number",), False),  # display only: buffered + the piece being recorded
            "max_seconds": (("number",), True),     # buffer length, seconds
            "source": (("string", "null"), True),   # capture source in use, null before the first start
            "encoder": (("string", "null"), True),
            "output_dir": (("string",), True),
            "target": (("string",), False),         # "screen" | "window" (absent: screen)
            # The reference daemon always sends these three (absent on older daemons):
            "target_name": (("string", "null"), False),  # window mode: the picked window's title
            "stop_reason": (("string", "null"), False),  # STOP_REASONS while stopped, else null
            "keep_history": (("boolean",), False),       # a stop keeps the replay
            "resolution": (("string",), True),
            "quality": (("string",), True),
            "bitrate_kbps": (("integer",), True),   # effective video bitrate
            "fps": (("integer",), True),
            "storage": ((STORAGE_CHECK,), True),
            "error": (("string",), False),          # with state error / no_storage (/ no_window, older)
            "protocol": (("integer",), False),      # REQUIRED by the spec; see Pending implementation
        },
        "error": {},
    },
    "save": {
        "request": {
            "seconds": (("integer", "string"), True),  # 1..3600, or "15s" / "5m" / "1h"
            # Wall-clock time (Unix seconds) the clip should end at instead of now;
            # ignored unless it is within the last hour. The clip bar sends the
            # moment it opened when the desktop can't hide it from capture.
            "until": (("number",), False),
        },
        "reply": {
            "ok": (("boolean",), True),
            "path": (("string",), True),         # absolute path of the new MP4
            "seconds": (("number",), True),      # footage actually saved
            "requested": (("integer",), True),   # what was asked for, in seconds
            "partial": (("boolean",), True),     # saved noticeably less than requested
            "reason": (("string",), False),      # why it is short, when known
        },
        "error": {},
    },
    "screenshot": {
        "request": {},
        "reply": {
            "ok": (("boolean",), True),
            "path": (("string",), True),         # absolute path of the new PNG
            "width": (("integer",), True),       # picture size (the recording's resolution)
            "height": (("integer",), True),
        },
        "error": {},
    },
    "pause": {
        "request": {},
        "reply": {
            "ok": (("boolean",), True),
            "state": (("string",), True),
        },
        "error": {},
    },
    "resume": {
        "request": {},
        "reply": {
            "ok": (("boolean",), True),
            "state": (("string",), True),
        },
        "error": {
            "state": (("string",), False),
            "storage": ((STORAGE_CHECK,), False),
        },
    },
    "pick_window": {
        "request": {},
        "reply": {
            "ok": (("boolean",), True),
            "state": (("string",), True),
        },
        "error": {
            "state": (("string",), False),
            "storage": ((STORAGE_CHECK,), False),
        },
    },
    "stop": {
        "request": {},
        "reply": {
            "ok": (("boolean",), True),
            "state": (("string",), True),
            "buffer_cleared": (("boolean",), True),  # false when keep_history kept it
        },
        "error": {},
    },
    "quit": {
        "request": {
            "keep_buffer": (("boolean",), False),
        },
        "reply": {
            "ok": (("boolean",), True),
            "buffer_cleared": (("boolean",), True),
        },
        "error": {},
    },
    "reload": {
        "request": {},
        "reply": {
            "ok": (("boolean",), True),
            "restarted": (("boolean",), True),
            "paused": (("boolean",), True),
            "state": (("string",), True),
            "storage": ((STORAGE_CHECK,), True),
            "warning": (("string",), False),     # saved/loaded, but capture could not start
        },
        "error": {},
    },
    "settings": {
        "request": {},
        "reply": {
            "ok": (("boolean",), True),
            "values": ((SETTING_VALUES,), True),
            "choices": ((SETTING_CHOICES,), True),
            "devices": ((AUDIO_DEVICES,), True),
            "fps": (("integer",), True),
            "max_seconds": (("integer",), True),
            "config": (("string",), True),        # path of config.toml
            "storage": ((STORAGE_REQUIREMENTS,), True),
            "controller_available": (("boolean",), False),  # python-evdev present
            "tabs": (("array",), False),          # [[tab name, [setting keys]], ...] for a settings UI
        },
        "error": {},
    },
    "configure": {
        "request": {
            "changes": ((MapOf("any"),), True),  # non-empty; keys from SETTING_KEYS
            "force": (("boolean",), False),
        },
        "reply": {
            "ok": (("boolean",), True),
            "changed": ((MapOf("any"),), True),  # settings whose value changed ({} = nothing)
            "restarted": (("boolean",), True),
            "paused": (("boolean",), True),
            "state": (("string",), True),
            "storage": ((STORAGE_CHECK,), True),
            "warning": (("string",), False),
        },
        "error": {
            "storage": ((STORAGE_CHECK,), False),  # with code no_storage
            "changed": ((MapOf("any"),), False),   # written, but the reload failed
        },
    },
}

# One line of <buffer dir>/index.jsonl (a closed segment).
INDEX_RECORD = {
    "file": (("string",), True),              # basename, never contains "/"
    "start": (("number",), True),             # wall-clock UNIX seconds
    "end": (("number",), True),
    "session": (("string", "null"), True),    # capture run id; new on every pipeline start
    "width": (("integer", "null"), True),
    "height": (("integer", "null"), True),
    "fps": (("number", "null"), True),
    "codec": (("string", "null"), True),      # "h264"
    "audio": (("boolean", "null"), True),
}

# --- the resident clip bar's control socket (RUNTIME_DIR/overlay.sock) ---------------

CLIP_BAR_REPLY = {
    "ok": (("boolean",), True),
    "visible": (("boolean",), True),
    "pid": (("integer",), True),
}

CLIP_BAR_COMMANDS: dict[str, dict] = {
    name: {"request": {}, "reply": CLIP_BAR_REPLY, "error": {}}
    for name in ("toggle", "show", "hide", "quit", "ping", "status")
}

# --- validation -----------------------------------------------------------------

_PY = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "any": lambda v: True,
}


def _type_name(t) -> str:
    if isinstance(t, dict):
        return "object"
    if isinstance(t, MapOf):
        return f"object of {t.value_type}"
    return t


def _check_value(where: str, value, types) -> list[str]:
    """Problems with ``value`` against a tuple of alternative types."""
    for t in types:
        if isinstance(t, dict):
            if isinstance(value, dict):
                return _check_object(where + ".", value, t)
        elif isinstance(t, MapOf):
            if isinstance(value, dict):
                check = _PY[t.value_type]
                return [f"{where}[{k!r}]: expected {t.value_type}, got {type(v).__name__}"
                        for k, v in value.items() if not check(v)]
        elif _PY[t](value):
            return []
    return [f"{where}: expected {' or '.join(map(_type_name, types))}, got {type(value).__name__}"]


def _check_object(prefix: str, obj: dict, schema: dict) -> list[str]:
    problems = []
    for name, (types, required) in schema.items():
        if name not in obj:
            if required:
                problems.append(f"missing required field {prefix}{name}")
            continue
        problems += _check_value(prefix + name, obj[name], types)
    return problems


def _check_enums(obj: dict) -> list[str]:
    problems = []
    if "state" in obj and isinstance(obj["state"], str) and obj["state"] not in STATES:
        problems.append(f"unknown state {obj['state']!r}")
    if "code" in obj and isinstance(obj["code"], str) and obj["code"] not in ERROR_CODES:
        problems.append(f"unknown error code {obj['code']!r}")
    if isinstance(obj.get("stop_reason"), str) and obj["stop_reason"] not in STOP_REASONS:
        problems.append(f"unknown stop_reason {obj['stop_reason']!r}")
    return problems


def validate_reply(cmd: str, reply, commands: dict | None = None) -> list[str]:
    """Problems with ``reply`` as the answer to ``cmd`` (empty list: it conforms).

    An ``ok: false`` reply is checked against the error shape (``error`` string,
    optional known ``code``, plus the command's documented error extras); an
    ``ok: true`` reply against the command's reply fields. Any reply to a
    command not in the table must be an error.
    """
    commands = COMMANDS if commands is None else commands
    if not isinstance(reply, dict):
        return [f"reply is not an object: {type(reply).__name__}"]
    if not isinstance(reply.get("ok"), bool):
        return ["missing required field ok (boolean)"]
    spec = commands.get(cmd)
    if not reply["ok"]:
        schema = {**ERROR_REPLY, **(spec["error"] if spec else {})}
        problems = _check_object("", reply, schema)
    elif spec is None:
        return [f"unknown command {cmd!r} answered ok: true"]
    else:
        problems = _check_object("", reply, spec["reply"])
    problems += _check_enums(reply)
    if spec is commands.get("settings") and reply["ok"] and isinstance(reply.get("devices"), dict):
        for side in ("outputs", "inputs"):
            for i, dev in enumerate(reply["devices"].get(side) or []):
                problems += _check_value(f"devices.{side}[{i}]", dev, (AUDIO_DEVICE,))
    if spec is commands.get("settings") and reply["ok"] and isinstance(reply.get("tabs"), list):
        for i, tab in enumerate(reply["tabs"]):
            if not (isinstance(tab, list) and len(tab) == 2 and isinstance(tab[0], str)
                    and isinstance(tab[1], list) and all(isinstance(k, str) for k in tab[1])):
                problems.append(f"tabs[{i}]: expected [name, [keys...]]")
    return problems


def validate_request(msg, commands: dict | None = None) -> list[str]:
    """Problems with a request object (empty list: a documented, well-formed request)."""
    commands = COMMANDS if commands is None else commands
    if not isinstance(msg, dict):
        return ["request is not an object"]
    cmd = msg.get("cmd")
    if not isinstance(cmd, str):
        return ["missing required field cmd (string)"]
    if cmd not in commands:
        return [f"unknown command {cmd!r}"]
    problems = _check_object("", msg, commands[cmd]["request"])
    if cmd == "configure" and commands is COMMANDS and isinstance(msg.get("changes"), dict):
        if not msg["changes"]:
            problems.append("changes must not be empty")
        problems += [f"unknown setting {k!r}" for k in msg["changes"] if k not in SETTING_KEYS]
    return problems
