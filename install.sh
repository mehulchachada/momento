#!/usr/bin/env bash
# Momento installer — user-level, never needs root.
#
#   ./install.sh               install / update for the current user
#   ./install.sh --enable      ...and start the recorder now + at every login
#   ./install.sh --check       only run the dependency check
#   ./install.sh --uninstall   remove Momento (keeps config, clips and buffer)
#   ./install.sh --purge       remove Momento, its config and its buffer
#                              (saved clips are never touched)
#
# What gets written (and nothing else):
#   ~/.local/share/momento/                       the Python package
#   ~/.local/bin/momento                          launcher script
#   ~/.local/share/applications/io.github.mehulchachada.Momento.desktop
#   ~/.config/systemd/user/momento.service
#   ~/.config/momento/config.toml                 only if it doesn't exist
#
# Environment overrides: MOMENTO_PYTHON=/path/to/python3 (interpreter used by
# the launcher; must see the distro's PyGObject/PySide6), XDG_DATA_HOME,
# XDG_CONFIG_HOME, MOMENTO_NO_SYSTEMD=1 (never call systemctl; for testing).
set -euo pipefail

APP_ID="io.github.mehulchachada.Momento"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

: "${HOME:?HOME is not set}"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
CACHE_HOME="${XDG_CACHE_HOME:-$HOME/.cache}"
BIN_DIR="$HOME/.local/bin"            # the systemd unit hard-codes %h/.local/bin
LIB_DIR="$DATA_HOME/momento"
APPS_DIR="$DATA_HOME/applications"
UNIT_DIR="$CONFIG_HOME/systemd/user"
CONF_DIR="$CONFIG_HOME/momento"
LAUNCHER="$BIN_DIR/momento"
DESKTOP_FILE="$APPS_DIR/$APP_ID.desktop"
UNIT_FILE="$UNIT_DIR/momento.service"
ICON_FILE="$DATA_HOME/icons/hicolor/scalable/apps/$APP_ID.svg"

if [ -t 1 ]; then
    B=$'\e[1m'; R=$'\e[31m'; G=$'\e[32m'; Y=$'\e[33m'; N=$'\e[0m'
else
    B=""; R=""; G=""; Y=""; N=""
fi
say()  { printf '%s==>%s %s\n' "$B" "$N" "$*"; }
ok()   { printf '  %s✓%s %s\n' "$G" "$N" "$*"; }
warn() { printf '  %s!%s %s\n' "$Y" "$N" "$*"; }
bad()  { printf '  %s✗%s %s\n' "$R" "$N" "$*"; }
die()  { printf '%serror:%s %s\n' "$R" "$N" "$*" >&2; exit 1; }

usage() { sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

# ---------------------------------------------------------------- arguments --
MODE=install
ENABLE=0
CHECK=1
while [ $# -gt 0 ]; do
    case "$1" in
        --enable)      ENABLE=1 ;;
        --no-check)    CHECK=0 ;;
        --check)       MODE=check ;;
        --uninstall)   MODE=uninstall ;;
        --purge)       MODE=purge ;;
        -h|--help)     usage; exit 0 ;;
        *)             usage >&2; die "unknown option: $1" ;;
    esac
    shift
done

[ "$(id -u)" -ne 0 ] || die "run this as your normal user, not root — Momento installs per user."

have() { command -v "$1" >/dev/null 2>&1; }

# systemctl --user only works inside a login session with a user bus.
user_systemd() {
    [ -z "${MOMENTO_NO_SYSTEMD:-}" ] || return 1
    have systemctl && systemctl --user show-environment >/dev/null 2>&1
}

# ----------------------------------------------------------- distro family --
OS_ID=""; OS_LIKE=""; OS_NAME="Linux"
if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    IFS=$'\x1f' read -r OS_ID OS_LIKE OS_NAME < <(
        . /etc/os-release
        printf '%s\x1f%s\x1f%s\n' "${ID:-}" "${ID_LIKE:-}" "${PRETTY_NAME:-${NAME:-Linux}}"
    ) || true
fi

FAMILY=unknown
case " $OS_ID $OS_LIKE " in
    *" steamos "*)                                  FAMILY=steamos ;;
    *" fedora "*|*" rhel "*|*" centos "*)          FAMILY=fedora ;;
    *" arch "*|*" cachyos "*|*" manjaro "*|*" endeavouros "*) FAMILY=arch ;;
    *" debian "*|*" ubuntu "*)                      FAMILY=debian ;;
    *" suse "*|*" opensuse "*|*opensuse*)           FAMILY=suse ;;
esac
ATOMIC=0
if [ -e /run/ostree-booted ]; then ATOMIC=1; fi
case "$OS_ID" in bazzite|bluefin|aurora) FAMILY=fedora; ATOMIC=1 ;; esac

# hint KEY -> distro packages that provide it
hint() {
    local key="$1"
    case "$FAMILY:$key" in
        fedora:python)   echo "python3 (>= 3.11)" ;;
        fedora:gi)       echo "python3-gobject gstreamer1" ;;
        fedora:dbus)     echo "python3-dbus" ;;
        fedora:pyside)   echo "python3-pyside6" ;;
        fedora:pipewire) echo "pipewire-gstreamer" ;;
        fedora:good)     echo "gstreamer1-plugins-good" ;;
        fedora:bad)      echo "gstreamer1-plugins-bad-free" ;;
        fedora:h264)     echo "gstreamer1-plugins-bad-free (vah264enc) + mesa-va-drivers-freeworld from RPM Fusion (AMD: Fedora's mesa ships with H.264 encode disabled) | intel-media-driver (Intel, RPM Fusion) | gstreamer1-plugin-openh264 (fedora-cisco-openh264 repo, software)" ;;
        fedora:aac)      echo "gstreamer1-plugin-libav (avenc_aac) or gstreamer1-plugins-bad-free (fdkaacenc)" ;;
        fedora:ffmpeg)   echo "ffmpeg-free (Fedora) or ffmpeg (RPM Fusion)" ;;
        fedora:layer)    echo "layer-shell-qt" ;;

        arch:python)     echo "python" ;;
        arch:gi)         echo "python-gobject gstreamer" ;;
        arch:dbus)       echo "python-dbus" ;;
        arch:pyside)     echo "pyside6" ;;
        arch:pipewire)   echo "gst-plugin-pipewire" ;;
        arch:good)       echo "gst-plugins-good" ;;
        arch:bad)        echo "gst-plugins-bad" ;;
        arch:h264)       echo "gst-plugin-va + libva-mesa-driver (AMD) / intel-media-driver (Intel) | gst-plugins-bad (nvh264enc, NVIDIA) | gst-plugins-ugly (x264enc, software)" ;;
        arch:aac)        echo "gst-libav" ;;
        arch:ffmpeg)     echo "ffmpeg" ;;
        arch:layer)      echo "layer-shell-qt" ;;

        debian:python)   echo "python3 (>= 3.11: Debian 12+, Ubuntu 24.04+)" ;;
        debian:gi)       echo "python3-gi gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0" ;;
        debian:dbus)     echo "python3-dbus" ;;
        debian:pyside)   echo "python3-pyside6.qtwidgets python3-pyside6.qtgui python3-pyside6.qtcore (Debian 13+/Ubuntu 24.04+; older: pip install --user PySide6)" ;;
        debian:pipewire) echo "gstreamer1.0-pipewire" ;;
        debian:good)     echo "gstreamer1.0-plugins-good gstreamer1.0-pulseaudio" ;;
        debian:bad)      echo "gstreamer1.0-plugins-bad" ;;
        debian:h264)     echo "gstreamer1.0-plugins-bad + mesa-va-drivers (AMD) / intel-media-va-driver-non-free (Intel) | gstreamer1.0-vaapi | gstreamer1.0-plugins-ugly (x264enc, software)" ;;
        debian:aac)      echo "gstreamer1.0-libav" ;;
        debian:ffmpeg)   echo "ffmpeg" ;;
        debian:layer)    echo "layer-shell-qt (Debian 13+/Ubuntu 24.10+; may be named liblayershellqtinterface6)" ;;

        suse:python)     echo "python3 (>= 3.11; Leap: python311)" ;;
        suse:gi)         echo "python3-gobject typelib-1_0-Gst-1_0" ;;
        suse:dbus)       echo "python3-dbus-python" ;;
        suse:pyside)     echo "python3-pyside6" ;;
        suse:pipewire)   echo "gstreamer-plugin-pipewire" ;;
        suse:good)       echo "gstreamer-plugins-good" ;;
        suse:bad)        echo "gstreamer-plugins-bad" ;;
        suse:h264)       echo "gstreamer-plugins-bad + Mesa-libva (from Packman for H.264) | gstreamer-plugins-ugly (Packman, x264enc) | gstreamer-plugin-openh264" ;;
        suse:aac)        echo "gstreamer-plugins-libav (Packman)" ;;
        suse:ffmpeg)     echo "ffmpeg (Packman ffmpeg recommended)" ;;
        suse:layer)      echo "layer-shell-qt6" ;;

        *:python)   echo "python3 >= 3.11" ;;
        *:gi)       echo "PyGObject + GStreamer GObject-introspection data" ;;
        *:dbus)     echo "dbus-python" ;;
        *:pyside)   echo "PySide6 (Qt 6 for Python)" ;;
        *:pipewire) echo "GStreamer PipeWire plugin (pipewiresrc)" ;;
        *:good)     echo "gst-plugins-good (incl. pulseaudio plugin)" ;;
        *:bad)      echo "gst-plugins-bad" ;;
        *:h264)     echo "a GStreamer H.264 encoder: va (gst-plugins-bad), vaapi, nvcodec, x264 (ugly) or openh264" ;;
        *:aac)      echo "gst-libav (avenc_aac) or fdkaac" ;;
        *:ffmpeg)   echo "ffmpeg" ;;
        *:layer)    echo "layer-shell-qt (KDE's Qt Wayland layer-shell plugin)" ;;
    esac
}

pkg_cmd() {
    case "$FAMILY" in
        fedora)  if [ "$ATOMIC" = 1 ]; then echo "rpm-ostree install"; else echo "sudo dnf install"; fi ;;
        arch)    echo "sudo pacman -S --needed" ;;
        debian)  echo "sudo apt install" ;;
        suse)    echo "sudo zypper install" ;;
        *)       echo "" ;;
    esac
}

# ---------------------------------------------------------- python choice --
# Prefer the distro interpreter: Homebrew/pyenv/conda pythons can't see the
# distro's PyGObject and PySide6.
pick_python() {
    local c
    if [ -n "${MOMENTO_PYTHON:-}" ]; then echo "$MOMENTO_PYTHON"; return; fi
    for c in /usr/bin/python3 "$(command -v python3 2>/dev/null || true)"; do
        [ -n "$c" ] && [ -x "$c" ] || continue
        if "$c" -c 'import gi' >/dev/null 2>&1; then echo "$c"; return; fi
    done
    if [ -x /usr/bin/python3 ]; then echo /usr/bin/python3; return; fi
    command -v python3 2>/dev/null || echo python3
}
PYTHON="$(pick_python)"

# ------------------------------------------------------- dependency check --
MISSING_KEYS=()
MISSING_OPT=()

py_try() { "$PYTHON" -c "$1" >/dev/null 2>&1; }
# Prefer the python GStreamer registry when gst-inspect-1.0 isn't installed
# (it lives in a separate "tools" package on some distros).
gst_has_any() {
    local e
    for e in "$@"; do
        if have gst-inspect-1.0; then
            if gst-inspect-1.0 --exists "$e" 2>/dev/null; then echo "$e"; return 0; fi
        elif "$PYTHON" - "$e" >/dev/null 2>&1 <<'PY'
import sys, gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst
Gst.init(None)
sys.exit(0 if Gst.ElementFactory.find(sys.argv[1]) else 1)
PY
        then echo "$e"; return 0
        fi
    done
    return 1
}

need() {  # need KEY LABEL  (called after a failed test)
    bad "$2  ->  $(hint "$1")"
    MISSING_KEYS+=("$1")
}

check_deps() {
    say "Checking dependencies ($OS_NAME, python: $PYTHON)"
    local found
    case "$PYTHON" in
        /usr/bin/*|/bin/*) ;;
        *) warn "using a non-system Python ($PYTHON); it usually can't see distro PyGObject/PySide6 — try MOMENTO_PYTHON=/usr/bin/python3" ;;
    esac

    if py_try 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
        ok "Python $("$PYTHON" -c 'import platform; print(platform.python_version())')"
    else
        need python "Python >= 3.11"
    fi

    if py_try 'import gi; gi.require_version("Gst", "1.0"); from gi.repository import Gst; Gst.init(None)'; then
        ok "PyGObject + GStreamer $("$PYTHON" -c 'import gi; gi.require_version("Gst","1.0"); from gi.repository import Gst; Gst.init(None); print(Gst.version_string().split()[-1])' 2>/dev/null)"
    else
        need gi "PyGObject / GStreamer introspection"
    fi

    if py_try 'import dbus, dbus.mainloop.glib'; then ok "dbus-python"; else need dbus "dbus-python"; fi
    if py_try 'from PySide6 import QtCore, QtGui, QtWidgets'; then ok "PySide6"; else need pyside "PySide6"; fi

    if ! have gst-inspect-1.0; then
        warn "gst-inspect-1.0 not found; checking GStreamer elements through Python instead"
    fi
    if found="$(gst_has_any pipewiresrc)"; then ok "GStreamer: $found"; else need pipewire "GStreamer: pipewiresrc"; fi
    if found="$(gst_has_any splitmuxsink)"; then ok "GStreamer: $found"; else need good "GStreamer: splitmuxsink"; fi
    if found="$(gst_has_any pulsesrc)"; then ok "GStreamer: $found (desktop audio)"; else need good "GStreamer: pulsesrc (desktop audio)"; fi
    if found="$(gst_has_any mpegtsmux)"; then ok "GStreamer: $found"; else need bad "GStreamer: mpegtsmux"; fi
    if found="$(gst_has_any h264parse)"; then ok "GStreamer: $found"; else need bad "GStreamer: h264parse"; fi
    if found="$(gst_has_any vah264enc vah264lpenc vaapih264enc nvh264enc qsvh264enc x264enc openh264enc)"; then
        ok "GStreamer H.264 encoder: $found"
        case "$found" in x264enc|openh264enc)
            warn "only a software encoder was found — expect noticeable CPU load; see hint: $(hint h264)" ;;
        esac
    else
        need h264 "GStreamer H.264 encoder (none of vah264enc/vaapih264enc/nvh264enc/qsvh264enc/x264enc/openh264enc)"
    fi
    if found="$(gst_has_any avenc_aac fdkaacenc)"; then ok "GStreamer AAC encoder: $found"; else need aac "GStreamer AAC encoder (avenc_aac/fdkaacenc) — clips would have no sound"; fi

    if have ffmpeg; then ok "ffmpeg $(ffmpeg -hide_banner -version 2>/dev/null | awk 'NR==1{print $3}')"; else need ffmpeg "ffmpeg"; fi

    # Optional: layer-shell-qt lets the overlay sit above fullscreen games on
    # KDE Plasma and wlroots compositors (Hyprland, Sway...).
    local plugdir layer=0 d
    plugdir="$("$PYTHON" -c 'from PySide6.QtCore import QLibraryInfo as Q; print(Q.path(Q.LibraryPath.PluginsPath))' 2>/dev/null || true)"
    for d in "$plugdir" /usr/lib64/qt6/plugins /usr/lib/qt6/plugins /usr/lib/*/qt6/plugins; do
        [ -n "$d" ] || continue
        if [ -e "$d/wayland-shell-integration/liblayer-shell.so" ]; then layer=1; break; fi
    done
    if [ "$layer" = 1 ]; then
        ok "layer-shell-qt (overlay above fullscreen games on KDE/wlroots)"
    else
        warn "optional: layer-shell-qt not found — overlay falls back to a normal topmost window  ->  $(hint layer)"
        MISSING_OPT+=(layer)
    fi

    echo
    if [ ${#MISSING_KEYS[@]} -eq 0 ]; then
        say "${G}All required dependencies found.${N}"
        return 0
    fi

    say "${Y}${#MISSING_KEYS[@]} required dependency check(s) failed.${N} Momento is installed but won't record until these are fixed."
    local pkgs="" k cmd
    for k in "${MISSING_KEYS[@]}"; do
        case "$k" in h264|aac|ffmpeg|python) continue ;; esac  # need a choice / extra repo
        pkgs="$pkgs $(hint "$k" | sed 's/ (.*//')"
    done
    cmd="$(pkg_cmd)"
    case "$FAMILY:$ATOMIC" in
        fedora:1)
            echo "  Fedora Atomic: Bazzite/Bluefin/Aurora already ship all of this in the image —"
            echo "  if something is missing there, update the system (ujust update) first."
            echo "  On Silverblue/Kinoite, layering is the only host option (last resort; slows updates):"
            [ -n "$pkgs" ] && echo "    $cmd$pkgs"
            ;;
        steamos:*)
            echo "  SteamOS' root filesystem is read-only and pacman changes are lost on updates."
            echo "  If PySide6/PyGObject/GStreamer plugins are missing there, run Momento from an"
            echo "  Arch distrobox for now, or wait for the Flatpak (see README)."
            ;;
        *)
            if [ -n "$cmd" ] && [ -n "$pkgs" ]; then
                echo "  Suggested:"
                echo "    $cmd$pkgs"
            fi
            ;;
    esac
    echo "  (Encoder/AAC/ffmpeg lines above list alternatives — pick the one for your GPU/repos.)"
    echo "  Re-run the check any time with: $0 --check"
    return 1
}

# --------------------------------------------------------------- install --
do_install() {
    [ -f "$SRC_DIR/momento/__init__.py" ] || die "run install.sh from the Momento source tree ($SRC_DIR has no momento/ package)"
    local version
    version="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$SRC_DIR/momento/__init__.py")"
    say "Installing Momento ${version:-?} for $(id -un)"

    mkdir -p "$LIB_DIR" "$BIN_DIR" "$APPS_DIR" "$UNIT_DIR" "$CONF_DIR"

    # 1. Package: replace atomically-ish so a running daemon keeps its old
    #    files until restart and a failed copy never leaves a half tree.
    local tmp="$LIB_DIR/.momento.new"
    rm -rf "${tmp:?}"
    cp -R "$SRC_DIR/momento" "$tmp"
    find "$tmp" -name '__pycache__' -type d -prune -exec rm -rf {} +
    rm -rf "${LIB_DIR:?}/momento"
    mv "$tmp" "$LIB_DIR/momento"
    cp "$SRC_DIR/data/config.example.toml" "$LIB_DIR/config.example.toml"
    printf '%s\n' "${version:-unknown}" > "$LIB_DIR/VERSION"
    ok "package        -> $LIB_DIR/momento"

    # 2. Launcher
    cat > "$LAUNCHER.new" <<EOF
#!/bin/sh
# Momento launcher — generated by install.sh; re-run install.sh to update.
PYTHONPATH="$LIB_DIR\${PYTHONPATH:+:\$PYTHONPATH}"
export PYTHONPATH
exec "$PYTHON" -m momento "\$@"
EOF
    chmod 755 "$LAUNCHER.new"
    mv -f "$LAUNCHER.new" "$LAUNCHER"
    ok "launcher       -> $LAUNCHER"

    # 3. Desktop entry. Its file name must equal APP_ID (GlobalShortcuts portal
    #    matches on it). Exec uses the absolute launcher path because
    #    ~/.local/bin is not on PATH in every graphical session.
    sed -e "s|^Exec=momento |Exec=\"$LAUNCHER\" |" \
        -e "s|^TryExec=momento\$|TryExec=$LAUNCHER|" \
        "$SRC_DIR/data/$APP_ID.desktop" > "$DESKTOP_FILE.new"
    mv -f "$DESKTOP_FILE.new" "$DESKTOP_FILE"
    if have update-desktop-database; then update-desktop-database -q "$APPS_DIR" 2>/dev/null || true; fi
    ok "desktop entry  -> $DESKTOP_FILE"
    mkdir -p "$(dirname "$ICON_FILE")"
    cp -f "$SRC_DIR/assets/logo.svg" "$ICON_FILE"
    ok "icon           -> $ICON_FILE"

    # 4. systemd user unit
    cp "$SRC_DIR/data/momento.service" "$UNIT_FILE.new"
    mv -f "$UNIT_FILE.new" "$UNIT_FILE"
    ok "systemd unit   -> $UNIT_FILE"

    # 5. Config (never overwrite the user's)
    if [ -e "$CONF_DIR/config.toml" ]; then
        ok "config         -> $CONF_DIR/config.toml (kept existing)"
    else
        cp "$SRC_DIR/data/config.example.toml" "$CONF_DIR/config.toml"
        ok "config         -> $CONF_DIR/config.toml (new, from example)"
    fi

    case ":$PATH:" in
        *":$BIN_DIR:"*) ;;
        *) warn "$BIN_DIR is not on your PATH — add it, or call $LAUNCHER directly" ;;
    esac

    if user_systemd; then
        systemctl --user daemon-reload || true
        if systemctl --user is-active --quiet momento.service; then
            systemctl --user restart momento.service && ok "restarted running momento.service with the new version"
        fi
    fi

    echo
    if [ "$CHECK" = 1 ]; then check_deps || true; echo; fi

    if [ "$ENABLE" = 1 ]; then
        do_enable
    else
        say "Done. Start recording now and at every login with:"
        echo "    systemctl --user enable --now momento.service     (or: $0 --enable)"
        echo "  Then press Super+G (or run: momento overlay) to save a clip."
    fi
}

do_enable() {
    user_systemd || die "no systemd user session available (are you in a graphical login session?)"
    systemctl --user daemon-reload
    systemctl --user enable --now momento.service
    say "momento.service enabled and started."
    echo "  Logs: journalctl --user -u momento.service -f"
    echo "  The first start may show a 'share your screen' dialog — pick your screen and allow it."
}

# -------------------------------------------------------------- uninstall --
do_uninstall() {
    local purge="$1"
    say "Removing Momento"
    if user_systemd && systemctl --user cat momento.service >/dev/null 2>&1; then
        systemctl --user disable --now momento.service >/dev/null 2>&1 || true
        ok "stopped and disabled momento.service"
    fi
    local f
    for f in "$UNIT_FILE" "$DESKTOP_FILE" "$ICON_FILE" "$LAUNCHER"; do
        if [ -e "$f" ]; then rm -f "$f"; ok "removed $f"; fi
    done
    if [ -d "$LIB_DIR" ]; then rm -rf "${LIB_DIR:?}"; ok "removed $LIB_DIR"; fi
    if user_systemd; then systemctl --user daemon-reload || true; fi
    if have update-desktop-database; then update-desktop-database -q "$APPS_DIR" 2>/dev/null || true; fi

    if [ "$purge" = 1 ]; then
        if [ -d "$CONF_DIR" ]; then rm -rf "${CONF_DIR:?}"; ok "removed $CONF_DIR"; fi
        if [ -d "$CACHE_HOME/momento" ]; then rm -rf "${CACHE_HOME:?}/momento"; ok "removed $CACHE_HOME/momento (replay buffer)"; fi
        if [ -d "${XDG_STATE_HOME:-$HOME/.local/state}/momento" ]; then
            rm -rf "${XDG_STATE_HOME:-$HOME/.local/state}/momento"; ok "removed ${XDG_STATE_HOME:-$HOME/.local/state}/momento"
        fi
        say "Purged. Your saved clips were not touched."
    else
        say "Removed. Kept: $CONF_DIR (settings), $CACHE_HOME/momento (replay buffer — can be several GB), and your saved clips."
        echo "  Use --purge to also delete settings and the buffer."
    fi
}

case "$MODE" in
    install)   do_install ;;
    check)     check_deps ;;
    uninstall) do_uninstall 0 ;;
    purge)     do_uninstall 1 ;;
esac
