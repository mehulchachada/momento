#!/usr/bin/env bash
# Momento installer.
#
#   curl -fsSL https://raw.githubusercontent.com/mehulchachada/momento/main/install.sh | bash
#   ./install.sh               from a clone: same thing
#
# What it does, in order:
#   1. checks which system packages Momento needs are missing on this distro
#   2. shows the exact package-manager command and asks before running it
#      with sudo (Fedora, Arch, Debian/Ubuntu, openSUSE; on Fedora Atomic,
#      SteamOS and NixOS it only explains what to do)
#   3. installs Momento for your user (~/.local, no root)
#   4. enables and starts the recorder (systemd user service)
#
# Options:
#   -y, --yes        don't ask, install missing packages right away
#   --no-deps        skip the system-package step
#   --deps-only      only install missing system packages, then check (may run as root)
#   --no-enable      install, but don't start the recorder or add it to login
#   --update         download the latest Momento release and reinstall
#   --dev            download and install the newest test version (the main
#                    branch) instead of the latest release; for testers
#   --check          only report what's installed and what's missing
#   --uninstall      remove Momento (keeps settings, replay buffer and clips)
#   --purge          remove Momento, its settings and its replay buffer
#                    (saved clips are never touched)
#   -h, --help       this help
#
# Files written for your user (and nothing else):
#   ~/.local/share/momento/          the Python package (+ a private PySide6 venv
#                                    only where the distro has no PySide6 package)
#   ~/.local/bin/momento             launcher
#   ~/.local/share/applications/io.github.mehulchachada.Momento.desktop
#   ~/.config/systemd/user/momento.service
#   ~/.config/momento/config.toml    only if it doesn't exist yet
#
# Environment: MOMENTO_REF=<branch|tag|commit> (what --update / curl downloads,
# default the latest release), MOMENTO_PYTHON=/path/to/python3, XDG_DATA_HOME,
# XDG_CONFIG_HOME, MOMENTO_NO_SYSTEMD=1 (never call systemctl; for testing).
set -euo pipefail

REPO_URL="https://github.com/mehulchachada/momento"
RAW_INSTALL="https://raw.githubusercontent.com/mehulchachada/momento/main/install.sh"
APP_ID="io.github.mehulchachada.Momento"

SELF="${BASH_SOURCE[0]:-}"
SRC_DIR=""
if [ -n "$SELF" ] && [ -f "$SELF" ]; then
    SRC_DIR="$(cd "$(dirname "$SELF")" && pwd)"
fi
# How to tell the user to re-run us.
if [ -n "${MOMENTO_INVOKED_AS:-}" ]; then
    INVOKED_AS="$MOMENTO_INVOKED_AS"
elif [ -n "$SRC_DIR" ] && [ -f "$SRC_DIR/data/momento.service" ]; then
    INVOKED_AS="./install.sh"
else
    INVOKED_AS="curl -fsSL $RAW_INSTALL | bash -s --"
fi

: "${HOME:?HOME is not set}"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
CACHE_HOME="${XDG_CACHE_HOME:-$HOME/.cache}"
STATE_HOME="${XDG_STATE_HOME:-$HOME/.local/state}"
BIN_DIR="$HOME/.local/bin"            # the systemd unit hard-codes %h/.local/bin
LIB_DIR="$DATA_HOME/momento"
VENV_DIR="$LIB_DIR/venv"
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
ok()   { [ "${QUIET:-0}" = 1 ] || printf '  %sok%s  %s\n' "$G" "$N" "$*"; }
warn() { printf '  %s!!%s  %s\n' "$Y" "$N" "$*"; }
bad()  { printf '  %sxx%s  %s\n' "$R" "$N" "$*"; }
note() { printf '      %s\n' "$*"; }
die()  { printf '%serror:%s %s\n' "$R" "$N" "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

usage() {
    if [ -n "$SELF" ] && [ -f "$SELF" ]; then
        sed -n '2,38p' "$SELF" | sed 's/^# \{0,1\}//'
    else
        echo "Momento installer. Options: --yes --no-deps --deps-only --no-enable --update --dev --check --uninstall --purge"
        echo "Full help: $REPO_URL#install"
    fi
}

# Ask a yes/no question (default yes). Works under `curl | bash` by reading
# the answer from the terminal instead of stdin.
ask() {
    local ans=""
    if [ "$YES" = 1 ]; then return 0; fi
    if [ -t 0 ]; then
        read -r -p "$1 [Y/n] " ans || ans=n
    elif { : </dev/tty; } 2>/dev/null; then
        read -r -p "$1 [Y/n] " ans </dev/tty || ans=n
    else
        warn "no terminal to ask \"$1\" — answering no (use --yes to accept)"
        return 1
    fi
    case "$ans" in ""|[Yy]*) return 0 ;; *) return 1 ;; esac
}

# systemctl --user only works inside a login session with a user bus.
user_systemd() {
    [ -z "${MOMENTO_NO_SYSTEMD:-}" ] || return 1
    have systemctl && systemctl --user show-environment >/dev/null 2>&1
}

# =============================================================== distro ==
OS_ID=""; OS_LIKE=""; OS_VARIANT=""; OS_NAME="Linux"
detect_distro() {
    local osr="${MOMENTO_OS_RELEASE:-/etc/os-release}"   # override for testing
    if [ -r "$osr" ]; then
        # shellcheck disable=SC1090
        IFS=$'\x1f' read -r OS_ID OS_LIKE OS_VARIANT OS_NAME < <(
            . "$osr"
            printf '%s\x1f%s\x1f%s\x1f%s\n' "${ID:-}" "${ID_LIKE:-}" "${VARIANT_ID:-}" \
                "${PRETTY_NAME:-${NAME:-Linux}}"
        ) || true
    fi
    FAMILY=unknown
    case " $OS_ID $OS_LIKE " in
        *" nixos "*)                                    FAMILY=nixos ;;
        *" steamos "*)                                  FAMILY=steamos ;;
        *" fedora "*|*" rhel "*|*" centos "*)           FAMILY=fedora ;;
        *" arch "*|*" cachyos "*|*" manjaro "*|*" endeavouros "*) FAMILY=arch ;;
        *" debian "*|*" ubuntu "*)                      FAMILY=debian ;;
        *" suse "*|*" opensuse "*|*opensuse*)           FAMILY=suse ;;
    esac
    ATOMIC=0
    if [ "$FAMILY" = fedora ]; then
        if [ -e /run/ostree-booted ]; then ATOMIC=1; fi
        case "$OS_ID:$OS_VARIANT" in
            bazzite*|bluefin*|aurora*|*:silverblue|*:kinoite|*:sericea|*:onyx|*:cosmic-atomic|*:*-atomic) ATOMIC=1 ;;
        esac
    fi
    # Images that ship everything Momento needs.
    UBLUE=0
    case "$OS_ID" in bazzite*|bluefin*|aurora*) UBLUE=1 ;; esac
}

# GPU vendors present (for the right VA-API driver package).
GPU_AMD=0; GPU_INTEL=0; GPU_NVIDIA=0
detect_gpu() {
    local v
    for v in /sys/class/drm/card*/device/vendor; do
        [ -r "$v" ] || continue
        case "$(cat "$v" 2>/dev/null)" in
            0x1002) GPU_AMD=1 ;;
            0x8086) GPU_INTEL=1 ;;
            0x10de) GPU_NVIDIA=1 ;;
        esac
    done
}

# ------------------------------------------------ package names per need --
# Verified in containers: Fedora 44, Ubuntu 24.04, Debian 13, Arch, Tumbleweed.
# Needs: python gi dbus pyside pipewire good bad aac ffmpeg h264 layer pactl evdev media
pkgs_for() {
    case "$FAMILY:$1" in
        fedora:python)   echo "python3" ;;
        fedora:gi)       echo "python3-gobject gstreamer1 gstreamer1-plugins-base" ;;
        fedora:dbus)     echo "python3-dbus" ;;
        fedora:pyside)   echo "python3-pyside6" ;;
        fedora:pipewire) echo "pipewire-gstreamer" ;;
        fedora:good)     echo "gstreamer1-plugins-good" ;;
        fedora:bad)      echo "gstreamer1-plugins-bad-free" ;;
        fedora:aac)      echo "gstreamer1-plugin-libav" ;;
        fedora:ffmpeg)   echo "ffmpeg-free" ;;
        fedora:h264)     echo "gstreamer1-plugins-bad-free gstreamer1-plugin-openh264"
                         # Fedora's own Mesa can't encode H.264; RPM Fusion's can.
                         if [ "$GPU_AMD" = 1 ] && [ -e /etc/yum.repos.d/rpmfusion-free.repo ]; then echo "mesa-va-drivers-freeworld"; fi
                         if [ "$GPU_INTEL" = 1 ]; then echo "libva-intel-media-driver"; fi ;;
        fedora:layer)    echo "layer-shell-qt" ;;
        fedora:pactl)    echo "pulseaudio-utils" ;;
        fedora:evdev)    echo "python3-evdev" ;;
        fedora:media)    echo "python3-pyside6" ;;

        arch:python)     echo "python" ;;
        arch:gi)         echo "python-gobject gstreamer gst-plugins-base-libs" ;;
        arch:dbus)       echo "python-dbus" ;;
        arch:pyside)     echo "pyside6 qt6-multimedia" ;;
        arch:pipewire)   echo "gst-plugin-pipewire" ;;
        arch:good)       echo "gst-plugins-good" ;;
        arch:bad)        echo "gst-plugins-bad" ;;
        arch:aac)        echo "gst-libav" ;;
        arch:ffmpeg)     echo "ffmpeg" ;;
        arch:h264)       echo "gst-plugin-va gst-plugins-bad gst-plugins-ugly"
                         if [ "$GPU_INTEL" = 1 ]; then echo "intel-media-driver"; fi ;;
        arch:layer)      echo "layer-shell-qt" ;;
        arch:pactl)      echo "libpulse" ;;
        arch:evdev)      echo "python-evdev" ;;
        arch:media)      echo "qt6-multimedia" ;;

        debian:python)   echo "python3" ;;
        debian:gi)       echo "python3-gi gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 gstreamer1.0-tools" ;;
        debian:dbus)     echo "python3-dbus" ;;
        debian:pyside)   echo "python3-pyside6.qtcore python3-pyside6.qtgui python3-pyside6.qtwidgets python3-pyside6.qtmultimedia" ;;
        debian:pipewire) echo "gstreamer1.0-pipewire" ;;
        debian:good)     echo "gstreamer1.0-plugins-good gstreamer1.0-pulseaudio" ;;
        debian:bad)      echo "gstreamer1.0-plugins-bad" ;;
        debian:aac)      echo "gstreamer1.0-libav" ;;
        debian:ffmpeg)   echo "ffmpeg" ;;
        debian:h264)     echo "gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly"
                         if [ "$GPU_AMD" = 1 ]; then echo "mesa-va-drivers"; fi
                         if [ "$GPU_INTEL" = 1 ]; then echo "intel-media-va-driver"; fi ;;
        debian:layer)    echo "layer-shell-qt" ;;
        debian:pactl)    echo "pulseaudio-utils" ;;
        debian:evdev)    echo "python3-evdev" ;;
        debian:media)    echo "python3-pyside6.qtmultimedia" ;;
        debian:venv)     echo "python3-venv" ;;

        suse:python)     echo "python3" ;;
        suse:gi)         echo "python3-gobject typelib-1_0-Gst-1_0 typelib-1_0-GstVideo-1_0 gstreamer-utils" ;;
        suse:dbus)       echo "python3-dbus-python" ;;
        suse:pyside)     echo "python3-pyside6" ;;
        suse:pipewire)   echo "gstreamer-plugin-pipewire" ;;
        suse:good)       echo "gstreamer-plugins-good" ;;
        suse:bad)        echo "gstreamer-plugins-bad" ;;
        suse:aac)        echo "gstreamer-plugins-libav" ;;
        suse:ffmpeg)     echo "ffmpeg" ;;
        suse:h264)       echo "gstreamer-plugins-bad"
                         if [ "$GPU_AMD" = 1 ]; then echo "Mesa-libva"; fi
                         if [ "$GPU_INTEL" = 1 ]; then echo "intel-media-driver"; fi ;;
        suse:layer)      echo "layer-shell-qt6" ;;
        suse:pactl)      echo "pulseaudio-utils" ;;
        suse:evdev)      echo "python3-evdev" ;;
        suse:media)      echo "python3-pyside6" ;;

        *:python)   echo "python3 (3.11 or newer)" ;;
        *:gi)       echo "PyGObject + GStreamer introspection data (Gst, GstVideo)" ;;
        *:dbus)     echo "dbus-python" ;;
        *:pyside)   echo "PySide6 (Qt 6 for Python)" ;;
        *:pipewire) echo "GStreamer PipeWire plugin (pipewiresrc)" ;;
        *:good)     echo "gst-plugins-good (incl. the pulseaudio plugin)" ;;
        *:bad)      echo "gst-plugins-bad" ;;
        *:aac)      echo "gst-libav (avenc_aac) or fdkaac" ;;
        *:ffmpeg)   echo "ffmpeg" ;;
        *:h264)     echo "a GStreamer H.264 encoder: va (gst-plugins-bad) + your GPU's VA-API driver, nvcodec, x264 or openh264" ;;
        *:layer)    echo "layer-shell-qt (Qt 6)" ;;
        *:pactl)    echo "pactl (pulseaudio-utils / libpulse)" ;;
        *:evdev)    echo "python-evdev" ;;
        *:media)    echo "PySide6 QtMultimedia" ;;
        *)          echo "" ;;
    esac
}

# Where the free repos can't give hardware H.264, say so once.
h264_notes() {
    case "$FAMILY" in
        fedora)
            if [ "$GPU_AMD" = 1 ] && [ "$UBLUE" = 0 ]; then
                note "AMD on Fedora: Fedora's Mesa has H.264 encoding switched off. For GPU encoding enable"
                note "RPM Fusion (https://rpmfusion.org/Configuration) and run:"
                note "  sudo dnf install mesa-va-drivers-freeworld"
            fi
            if [ "$GPU_NVIDIA" = 1 ] && [ "$UBLUE" = 0 ]; then
                note "NVIDIA: NVENC needs the proprietary driver (RPM Fusion akmod-nvidia)."
            fi ;;
        suse)
            note "openSUSE: the default repos leave out H.264 GPU encoding. For it, add Packman"
            note "and run: sudo zypper dup --from packman --allow-vendor-change" ;;
        arch|debian)
            if [ "$GPU_NVIDIA" = 1 ]; then note "NVIDIA: NVENC (nvh264enc) needs the proprietary driver."; fi ;;
    esac
}

# Is package $1 already installed?
pkg_installed() {
    case "$FAMILY" in
        fedora|suse) rpm -q --whatprovides "$1" >/dev/null 2>&1 ;;
        arch)        pacman -Q "$1" >/dev/null 2>&1 || pacman -Qq --provides "$1" >/dev/null 2>&1 ;;
        debian)      [ "$(dpkg-query -W -f='${db:Status-Status}' "$1" 2>/dev/null)" = installed ] ;;
        *)           return 1 ;;
    esac
}

# Can package $1 be installed from the configured repos? Only answered where
# it's a cheap local lookup; elsewhere assume yes (the names are verified).
pkg_available() {
    case "$FAMILY" in
        arch)
            if ! compgen -G '/var/lib/pacman/sync/*.db' >/dev/null; then return 0; fi
            pacman -Si "$1" >/dev/null 2>&1 || pacman -Ssq "^$1\$" >/dev/null 2>&1 ;;
        debian)
            if ! compgen -G '/var/lib/apt/lists/*_Packages*' >/dev/null; then return 0; fi
            local cand
            cand="$(apt-cache policy "$1" 2>/dev/null | awk '/Candidate:/{print $2}')"
            [ -n "$cand" ] && [ "$cand" != "(none)" ] || return 1
            # Ubuntu 24.04 ships the Qt 5 layer-shell-qt, useless for PySide6.
            if [ "$1" = layer-shell-qt ]; then case "$cand" in 5*|[0-4]*) return 1 ;; esac; fi
            return 0 ;;
        *) return 0 ;;
    esac
}

# ======================================================== dependency check ==
# check_deps sets MISSING (required needs), MISSING_OPT (optional needs) and
# HW_ENC (1 if a GPU H.264 encoder is usable). QUIET=1 hides the ok lines.
MISSING=(); MISSING_OPT=(); HW_ENC=0; PYTHON=""

pick_python() {
    local c
    if [ -n "${MOMENTO_PYTHON:-}" ]; then echo "$MOMENTO_PYTHON"; return; fi
    # A private venv made because the distro had no PySide6 package.
    if [ -x "$VENV_DIR/bin/python3" ] && "$VENV_DIR/bin/python3" -c 'import gi, PySide6' >/dev/null 2>&1; then
        echo "$VENV_DIR/bin/python3"; return
    fi
    # Prefer the distro interpreter: Homebrew/pyenv/conda pythons can't see
    # the distro's PyGObject and PySide6.
    for c in /usr/bin/python3 "$(command -v python3 2>/dev/null || true)"; do
        [ -n "$c" ] && [ -x "$c" ] || continue
        if "$c" -c 'import gi' >/dev/null 2>&1; then echo "$c"; return; fi
    done
    if [ -x /usr/bin/python3 ]; then echo /usr/bin/python3; return; fi
    command -v python3 2>/dev/null || echo python3
}

py_try() { "$PYTHON" -c "$1" >/dev/null 2>&1; }

gst_has_any() {  # prints the first element that exists
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

need()    { bad "$2   ->  $(pkgs_for "$1" | tr '\n' ' ')"; MISSING+=("$1"); }
optneed() { warn "optional: $2   ->  $(pkgs_for "$1" | tr '\n' ' ')"; MISSING_OPT+=("$1"); }

check_deps() {
    local found gst_ok=0
    MISSING=(); MISSING_OPT=(); HW_ENC=0
    PYTHON="$(pick_python)"
    [ "${QUIET:-0}" = 1 ] || say "Checking dependencies ($OS_NAME, python: $PYTHON)"
    if ! have "$PYTHON"; then
        : # no Python at all; reported below
    else case "$PYTHON" in
        /usr/bin/*|/bin/*|"$VENV_DIR"/*) ;;
        *) warn "using a non-system Python ($PYTHON); it usually can't see distro PyGObject/PySide6 — try MOMENTO_PYTHON=/usr/bin/python3" ;;
    esac; fi

    if py_try 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
        ok "Python $("$PYTHON" -c 'import platform; print(platform.python_version())')"
    else
        need python "Python 3.11 or newer"
    fi
    if py_try 'import gi; gi.require_version("Gst", "1.0"); gi.require_version("GstVideo", "1.0"); from gi.repository import Gst, GstVideo; Gst.init(None)'; then
        gst_ok=1
        ok "PyGObject + GStreamer $("$PYTHON" -c 'import gi; gi.require_version("Gst","1.0"); from gi.repository import Gst; Gst.init(None); print(Gst.version_string().split()[-1])' 2>/dev/null)"
    else
        need gi "PyGObject + GStreamer introspection"
    fi
    if py_try 'import dbus, dbus.mainloop.glib'; then ok "dbus-python"; else need dbus "dbus-python"; fi
    if py_try 'from PySide6 import QtCore, QtGui, QtWidgets'; then
        ok "PySide6"
        if py_try 'from PySide6 import QtMultimedia'; then ok "PySide6 QtMultimedia (plays clips in the gallery)"
        elif [[ "$PYTHON" == "$VENV_DIR"/* ]]; then
            # PySide6 came from PyPI (e.g. Ubuntu 24.04): nothing to install.
            warn "Everything works, except playing clips in the gallery. That needs a newer system (Ubuntu 25.04 or newer)."
        else optneed media "PySide6 QtMultimedia (plays clips in the gallery)"; fi
    else
        need pyside "PySide6"
    fi

    if have gst-inspect-1.0 || [ "$gst_ok" = 1 ]; then
        if found="$(gst_has_any pipewiresrc)"; then ok "GStreamer: $found"; else need pipewire "GStreamer: pipewiresrc"; fi
        if found="$(gst_has_any splitmuxsink)"; then ok "GStreamer: $found"; else need good "GStreamer: splitmuxsink"; fi
        if found="$(gst_has_any pulsesrc)"; then ok "GStreamer: $found (desktop audio)"; else need good "GStreamer: pulsesrc (desktop audio)"; fi
        if found="$(gst_has_any mpegtsmux)"; then ok "GStreamer: $found"; else need bad "GStreamer: mpegtsmux"; fi
        if found="$(gst_has_any h264parse)"; then ok "GStreamer: $found"; else need bad "GStreamer: h264parse"; fi
        if found="$(gst_has_any vah264enc vah264lpenc vaapih264enc nvh264enc qsvh264enc)"; then
            HW_ENC=1; ok "GStreamer H.264 encoder: $found (GPU)"
        elif found="$(gst_has_any x264enc openh264enc)"; then
            ok "GStreamer H.264 encoder: $found (software)"
            warn "no GPU H.264 encoder usable — recording works but costs CPU"
            [ "${QUIET:-0}" = 1 ] || h264_notes
        else
            need h264 "GStreamer H.264 encoder"
            [ "${QUIET:-0}" = 1 ] || h264_notes
        fi
        if found="$(gst_has_any avenc_aac fdkaacenc)"; then ok "GStreamer AAC encoder: $found"; else need aac "GStreamer AAC encoder (clips would have no sound)"; fi
    else
        # Can't look inside GStreamer yet: assume every plugin set is needed
        # (already-installed packages are filtered out later anyway).
        bad "GStreamer plugins: can't check until PyGObject + GStreamer are installed"
        MISSING+=(pipewire good bad aac h264)
    fi
    if have ffmpeg && have ffprobe; then
        local _a _b ffv=""
        read -r _a _b ffv _ < <(ffmpeg -hide_banner -version 2>/dev/null) || true
        ok "ffmpeg $ffv"
    else
        need ffmpeg "ffmpeg + ffprobe"
    fi

    # Optional: layer-shell-qt lets the overlay sit above fullscreen games on
    # KDE Plasma and wlroots compositors (Hyprland, Sway...).
    local plugdir layer=0 d
    plugdir="$("$PYTHON" -c 'from PySide6.QtCore import QLibraryInfo as Q; print(Q.path(Q.LibraryPath.PluginsPath))' 2>/dev/null || true)"
    for d in "$plugdir" /usr/lib64/qt6/plugins /usr/lib/qt6/plugins /usr/lib/*/qt6/plugins; do
        [ -n "$d" ] || continue
        if [ -e "$d/wayland-shell-integration/liblayer-shell.so" ]; then layer=1; break; fi
    done
    if [ "$layer" = 1 ]; then ok "layer-shell-qt (clip bar above fullscreen games on KDE/wlroots)"
    else optneed layer "layer-shell-qt (clip bar above fullscreen games on KDE/wlroots)"; fi
    if have pactl; then ok "pactl (sound device menu)"; else optneed pactl "pactl (sound device menu)"; fi
    if py_try 'import evdev'; then ok "python-evdev (game controllers)"
    else optneed evdev "python-evdev (open and use the clip bar with a game controller)"; fi

    [ ${#MISSING[@]} -eq 0 ]
}

# ================================================== system package step ==
INSTALLED_PKGS=""

# Packages that would fix the current MISSING/MISSING_OPT, minus what's
# already installed or not offered by the repos. Sets WANT (required),
# EXTRA (optional + GPU encoder packages; installed separately so one odd
# package can't block the rest) and UNAVAILABLE.
WANT=""; EXTRA=""; UNAVAILABLE=""; PYSIDE_FROM_PYPI=0
plan_packages() {
    local keys=("${MISSING[@]}" "${MISSING_OPT[@]}") k p v seen=" "
    WANT=""; EXTRA=""; UNAVAILABLE=""; PYSIDE_FROM_PYPI=0
    # No GPU encoder: also pull the VA-API plugin/driver for this GPU. If the
    # packages are already there, the GPU/driver simply can't do it.
    if [ "$HW_ENC" = 0 ] && [[ " ${keys[*]} " != *" h264 "* ]]; then keys+=(h264); fi
    for k in "${keys[@]}"; do
        for p in $(pkgs_for "$k"); do
            case "$seen" in *" $p "*) continue ;; esac
            seen="$seen$p "
            if pkg_installed "$p"; then continue; fi
            if ! pkg_available "$p"; then
                if [ "$k" = pyside ]; then
                    PYSIDE_FROM_PYPI=1
                    for v in $(pkgs_for venv); do pkg_installed "$v" || WANT="$WANT $v"; done
                    break
                fi
                UNAVAILABLE="$UNAVAILABLE $p"; continue
            fi
            case "$k" in
                layer|pactl|h264|media) EXTRA="$EXTRA $p" ;;
                *)                WANT="$WANT $p" ;;
            esac
        done
    done
    WANT="${WANT# }"; EXTRA="${EXTRA# }"; UNAVAILABLE="${UNAVAILABLE# }"
}

# The install command for this package manager, for the packages in $1.
pm_install() {
    case "$FAMILY" in
        fedora) echo "dnf install -y $1" ;;
        arch)   echo "pacman -S --needed --noconfirm $1" ;;
        debian) echo "env DEBIAN_FRONTEND=noninteractive apt-get install -y $1" ;;
        suse)   echo "zypper --non-interactive install $1" ;;
    esac
}

install_system_deps() {
    say "Checking system packages ($OS_NAME)"
    detect_gpu
    if QUIET=1 check_deps && [ ${#MISSING_OPT[@]} -eq 0 ] && [ "$HW_ENC" = 1 ]; then
        ok "everything Momento needs is already installed"
        return 0
    fi

    case "$FAMILY:$ATOMIC" in
        fedora:1) deps_atomic; return 0 ;;
        steamos:*) deps_steamos; return 0 ;;
        nixos:*) deps_nixos; return 0 ;;
        unknown:*)
            warn "unrecognised distro ($OS_NAME): install the packages listed above yourself."
            return 0 ;;
    esac

    plan_packages
    if [ -n "$UNAVAILABLE" ]; then warn "Not available on $OS_NAME, so skipped: $UNAVAILABLE"; fi
    if [ "$PYSIDE_FROM_PYPI" = 1 ]; then
        warn "$OS_NAME has no PySide6 package (the toolkit for Momento's bar). It will be downloaded into Momento's own folder instead."
    fi
    if [ -z "$WANT$EXTRA" ]; then
        ok "nothing to install from the repos"
        return 0
    fi

    local pre=""
    if [ "$(id -u)" -ne 0 ]; then
        if have sudo; then pre="sudo "
        elif have doas; then pre="doas "
        else
            warn "neither sudo nor doas found — install these as root:  $WANT $EXTRA"
            return 0
        fi
    fi
    local refresh="" main="" extra=""
    case "$FAMILY" in
        arch)   compgen -G '/var/lib/pacman/sync/*.db' >/dev/null || refresh="${pre}pacman -Sy" ;;
        debian) refresh="${pre}apt-get update" ;;
    esac
    [ -z "$WANT" ]  || main="$pre$(pm_install "$WANT")"
    [ -z "$EXTRA" ] || extra="$pre$(pm_install "$EXTRA")"

    echo
    echo "  Missing packages will be installed with:"
    local c
    for c in "$refresh" "$main" "$extra"; do [ -z "$c" ] || echo "    $c"; done
    echo
    if ! ask "Install these packages?"; then
        warn "skipped. Momento won't record until they're installed."
        return 0
    fi
    # Word splitting is intended below: the commands are built from fixed words.
    # shellcheck disable=SC2086
    if [ -n "$refresh" ] && ! $refresh; then bad "failed: $refresh"; fi
    # shellcheck disable=SC2086
    if [ -n "$main" ]; then
        if $main; then INSTALLED_PKGS="$WANT"; ok "installed: $WANT"
        else bad "package install failed: $main"; fi
    fi
    # shellcheck disable=SC2086
    if [ -n "$extra" ]; then
        if $extra; then INSTALLED_PKGS="${INSTALLED_PKGS:+$INSTALLED_PKGS }$EXTRA"; ok "installed: $EXTRA"
        else warn "optional packages failed to install (Momento still works): $EXTRA"; fi
    fi
}

deps_atomic() {
    plan_packages
    if [ "$UBLUE" = 1 ]; then
        warn "$OS_NAME normally ships everything Momento needs. Update the image first:  ujust update"
    else
        warn "Fedora Atomic ($OS_NAME): the system is read-only, so this installer doesn't layer packages."
        note "Your options:"
        note "  * layer them (works, but every future update gets slower; needs a reboot):"
        [ -z "$WANT$EXTRA" ] || note "      rpm-ostree install $WANT $EXTRA"
        note "  * or run Momento from a Fedora distrobox:  distrobox create -i fedora:latest momento"
        note "  * or switch to Bazzite/Aurora/Bluefin, which ship these, or wait for the Flatpak."
    fi
}

deps_steamos() {
    warn "SteamOS: the system is read-only and pacman changes are wiped on every update."
    note "Run Momento from an Arch distrobox for now (the Flatpak is on the way):"
    note "  distrobox create -i archlinux:latest momento && distrobox enter momento"
    note "  then run this installer again inside the box."
}

deps_nixos() {
    warn "NixOS: this installer can't add system packages. Add these to your configuration"
    note "(or a nix-shell) and point MOMENTO_PYTHON at a Python that has them:"
    note "  (python3.withPackages (p: [ p.pygobject3 p.dbus-python p.pyside6 p.evdev ])) ffmpeg"
    note "  gst_all_1.{gstreamer,gst-plugins-base,gst-plugins-good,gst-plugins-bad,gst-plugins-ugly,gst-libav}"
    note "  pipewire (its GStreamer plugin), layer-shell-qt"
}

# PySide6 isn't packaged (e.g. Ubuntu 24.04): put the PyPI wheel into a venv
# that still sees the distro's PyGObject and dbus-python.
pyside_venv() {
    PYTHON="$(pick_python)"
    if py_try 'from PySide6 import QtCore, QtGui, QtWidgets'; then return 0; fi
    py_try 'import gi, sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || return 0
    [ "$FAMILY" != fedora ] || [ "$ATOMIC" = 0 ] || return 0
    echo
    warn "Momento's bar needs PySide6, and $OS_NAME doesn't have it as a package."
    if ! ask "Download it (about 100 MB) into Momento's own folder ($VENV_DIR)?"; then return 0; fi
    local base="$PYTHON"
    case "$base" in "$VENV_DIR"/*) base=/usr/bin/python3 ;; esac
    rm -rf "${VENV_DIR:?}"
    mkdir -p "$LIB_DIR"
    if ! "$base" -m venv --system-site-packages "$VENV_DIR"; then
        bad "Couldn't set up Momento's own Python folder. Install python3-venv, then run the installer again."; rm -rf "${VENV_DIR:?}"; return 0
    fi
    if "$VENV_DIR/bin/python3" -m pip install --quiet --disable-pip-version-check PySide6-Essentials; then
        ok "PySide6 -> $VENV_DIR"
        INSTALLED_PKGS="${INSTALLED_PKGS:+$INSTALLED_PKGS }PySide6-Essentials (PyPI, private venv)"
    else
        bad "Couldn't download PySide6. Check your internet connection, then run the installer again."; rm -rf "${VENV_DIR:?}"
    fi
}

# ============================================================ bootstrap ==
# The tag of the latest published release (drafts don't count), from where
# github.com/<repo>/releases/latest redirects to. Prints nothing when there is
# no release yet; fails when GitHub can't be reached.
latest_release() {
    local where="" out
    if have curl; then
        where="$(curl -fsSLI -o /dev/null -w '%{url_effective}' "$REPO_URL/releases/latest")" || return 1
    elif have wget; then
        # GNU wget, wget2 and busybox all print the redirect target somewhere.
        out="$(wget -S --spider "$REPO_URL/releases/latest" 2>&1)" || return 1
        where="$(printf '%s\n' "$out" | tr -d '\r' | grep -o 'https://[^][ ]*/releases/tag/v[0-9][^][ ]*' | tail -n 1)" || true
    else
        return 1
    fi
    case "$where" in
        */releases/tag/v[0-9]*) echo "${where##*/releases/tag/}" ;;
    esac
}

# Running without the source tree next to us (curl | bash) or with --update:
# fetch the source tarball, then hand over to the install.sh inside it.
bootstrap() {
    local ref="${MOMENTO_REF:-}" url tmp label
    if [ -z "$ref" ] && [ -z "${MOMENTO_TARBALL_URL:-}" ]; then
        if [ "$DEV" = 1 ]; then
            ref=main
        elif ! ref="$(latest_release)"; then
            die "couldn't reach GitHub to find the latest Momento. Check your internet connection and try again."
        elif [ -z "$ref" ]; then
            warn "No Momento release is out yet, so the newest test version is installed instead."
            ref=main
        fi
    fi
    ref="${ref:-main}"
    if [ -n "${MOMENTO_TARBALL_URL:-}" ]; then url="$MOMENTO_TARBALL_URL"
    elif [ "$ref" = main ]; then url="$REPO_URL/archive/refs/heads/main.tar.gz"
    else url="$REPO_URL/archive/$ref.tar.gz"
    fi
    case "$ref" in
        main) label="the newest test version of Momento" ;;
        v[0-9]*) label="Momento ${ref#v}" ;;
        *) label="Momento ($ref)" ;;
    esac
    have tar || die "tar is required"
    tmp="$(mktemp -d "${TMPDIR:-/tmp}/momento-install.XXXXXX")"
    say "Downloading $label"
    note "$url"
    if have curl; then
        curl -fsSL "$url" | tar -xz -C "$tmp" --strip-components=1 || { rm -rf "$tmp"; die "download failed: $url"; }
    elif have wget; then
        wget -qO- "$url" | tar -xz -C "$tmp" --strip-components=1 || { rm -rf "$tmp"; die "download failed: $url"; }
    else
        rm -rf "$tmp"; die "need curl or wget to download Momento"
    fi
    [ -f "$tmp/install.sh" ] && [ -f "$tmp/momento/__init__.py" ] || { rm -rf "$tmp"; die "downloaded archive doesn't look like Momento"; }
    MOMENTO_BOOTSTRAP_DIR="$tmp" MOMENTO_INVOKED_AS="$INVOKED_AS" exec bash "$tmp/install.sh" "$@"
}

# ============================================================== install ==
do_install() {
    local version
    version="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$SRC_DIR/momento/__init__.py")"
    say "Installing Momento ${version:-?} for $(id -un)"

    mkdir -p "$LIB_DIR" "$BIN_DIR" "$APPS_DIR" "$UNIT_DIR" "$CONF_DIR"

    # 1. Package: swap in a complete copy so a running daemon keeps its old
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

    # 2. Launcher (pins the interpreter that passed the dependency check)
    PYTHON="$(pick_python)"
    # Installed inside a distrobox (e.g. on SteamOS): when the launcher is
    # run from the host (systemd unit, desktop entry), hop into the box.
    local box=""
    if [ -e /run/.containerenv ] && [ -n "${CONTAINER_ID:-}" ]; then box="$CONTAINER_ID"; fi
    {
        echo '#!/bin/sh'
        echo '# Momento launcher — generated by install.sh; re-run install.sh to update.'
        if [ -n "$box" ]; then
            echo 'if [ ! -e /run/.containerenv ] && command -v distrobox-enter >/dev/null 2>&1; then'
            echo "    exec distrobox-enter -n \"$box\" -- \"\$0\" \"\$@\""
            echo 'fi'
        fi
        echo "PYTHONPATH=\"$LIB_DIR\${PYTHONPATH:+:\$PYTHONPATH}\""
        echo 'export PYTHONPATH'
        echo "exec \"$PYTHON\" -m momento \"\$@\""
    } > "$LAUNCHER.new"
    chmod 755 "$LAUNCHER.new"
    mv -f "$LAUNCHER.new" "$LAUNCHER"
    ok "launcher       -> $LAUNCHER${box:+ (runs inside distrobox $box)}"

    # 3. Desktop entry. Its file name must equal APP_ID (the GlobalShortcuts
    #    portal matches on it). Exec uses the absolute launcher path because
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
        ok "config         -> $CONF_DIR/config.toml (new)"
    fi

    # 6. Keep a copy of the installer so `--update`/`--uninstall` work later
    #    without a clone.
    cp -f "$SRC_DIR/install.sh" "$LIB_DIR/install.sh.new" && mv -f "$LIB_DIR/install.sh.new" "$LIB_DIR/install.sh"
    chmod 755 "$LIB_DIR/install.sh"

    case ":$PATH:" in
        *":$BIN_DIR:"*) ;;
        *) warn "$BIN_DIR is not on your PATH — add it, or run $LAUNCHER directly" ;;
    esac

    if user_systemd; then
        systemctl --user daemon-reload || true
        if [ "$ENABLE" = 0 ] && systemctl --user is-active --quiet momento.service; then
            systemctl --user restart momento.service && ok "restarted momento.service with the new version"
        fi
    fi
}

ENABLED=0
do_enable() {
    if ! user_systemd; then
        warn "no systemd user session here (SSH, container?) — start it later from your desktop with:"
        note "systemctl --user enable --now momento.service"
        return 0
    fi
    systemctl --user daemon-reload
    systemctl --user enable momento.service >/dev/null 2>&1
    # restart, not start: picks up the new version if it was already running
    if systemctl --user restart momento.service; then
        ENABLED=1
        ok "momento.service enabled and started (and starts at every login)"
    else
        bad "momento.service failed to start — see: journalctl --user -u momento.service -e"
    fi
}

summary() {
    local deps_ok="$1"
    echo
    say "${B}Momento is installed.${N}"
    if [ -n "$INSTALLED_PKGS" ]; then note "Added for Momento: $INSTALLED_PKGS"; fi
    note "Installed for $(id -un): $LAUNCHER, $LIB_DIR"
    if [ "$deps_ok" != 1 ]; then
        warn "some dependencies are still missing (see the list above) — Momento can't record until they're installed."
        note "Check again any time:  $INVOKED_AS --check"
    fi
    echo
    if [ "$ENABLED" = 1 ]; then
        echo "  Recording in the background now, and at every login."
    else
        echo "  Start recording (now and at every login):"
        echo "    systemctl --user enable --now momento.service"
    fi
    echo "  First start: your desktop asks which screen to share. Pick your monitor"
    echo "               (tick \"remember\" / \"allow restore\" if offered) — it's asked only once."
    echo "  Save a clip: press ${B}Super + Shift + G${N} and pick a length (or: momento save 5m)."
    echo "  Is it running?  momento status"
    echo "  Clips go to ~/Videos/Momento.   Update later:  $LIB_DIR/install.sh --update"
}

# ============================================================ uninstall ==
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
        if [ -d "$STATE_HOME/momento" ]; then rm -rf "${STATE_HOME:?}/momento"; ok "removed $STATE_HOME/momento"; fi
        say "Purged. Your saved clips were not touched."
    else
        say "Removed. Kept: $CONF_DIR (settings), $CACHE_HOME/momento (replay buffer — can be several GB) and your saved clips."
        note "Use --purge to also delete settings and the buffer."
    fi
    note "System packages installed for Momento are left in place (other apps may use them)."
}

# ================================================================= main ==
main() {
    MODE=install; YES=0; DEPS=1; ENABLE=1; UPDATE=0; DEV=0
    local args=() a
    for a in "$@"; do
        case "$a" in
            -y|--yes)     YES=1 ;;
            --no-deps)    DEPS=0 ;;
            --deps-only)  MODE=deps ;;
            --enable)     ENABLE=1 ;;
            --no-enable)  ENABLE=0 ;;
            --no-check)   ;;  # accepted for compatibility
            --update)     UPDATE=1; continue ;;
            --dev)        DEV=1; UPDATE=1; continue ;;
            --check)      MODE=check ;;
            --uninstall)  MODE=uninstall ;;
            --purge)      MODE=purge ;;
            -h|--help)    usage; exit 0 ;;
            *)            usage >&2; die "unknown option: $a" ;;
        esac
        args+=("$a")
    done

    # Clean up the download when we were bootstrapped.
    if [ -n "${MOMENTO_BOOTSTRAP_DIR:-}" ] && [ "$MOMENTO_BOOTSTRAP_DIR" = "$SRC_DIR" ]; then
        # shellcheck disable=SC2064
        trap "rm -rf '$MOMENTO_BOOTSTRAP_DIR'" EXIT
    fi

    detect_distro

    case "$MODE" in
        check)
            detect_gpu
            check_deps; exit $? ;;
        uninstall) do_uninstall 0; exit 0 ;;
        purge)     do_uninstall 1; exit 0 ;;
        deps)
            install_system_deps
            echo
            check_deps; exit $? ;;
    esac

    # --- install ---
    if [ "$(id -u)" -eq 0 ]; then
        die "run this as your normal user, not root/sudo — Momento installs per user and asks for sudo itself when it needs system packages."
    fi
    if [ "$UPDATE" = 1 ] || [ -z "$SRC_DIR" ] || [ ! -f "$SRC_DIR/momento/__init__.py" ] || [ ! -f "$SRC_DIR/data/momento.service" ]; then
        bootstrap ${args[@]+"${args[@]}"}
    fi

    say "Momento installer — $OS_NAME"
    if [ "$DEPS" = 1 ]; then install_system_deps; echo; fi
    pyside_venv
    do_install
    echo
    local deps_ok=1
    check_deps || deps_ok=0
    if [ "$ENABLE" = 1 ]; then
        echo
        if [ "$deps_ok" = 1 ]; then do_enable
        else warn "not starting the recorder until the missing dependencies are installed"; fi
    fi
    summary "$deps_ok"
}

main "$@"
