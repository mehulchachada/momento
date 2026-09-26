<div align="center">

<img src="assets/logo.svg" width="160" alt="Momento logo">

# Momento

**Never miss the moment.**

Instant replay for Linux gaming. Momento keeps the last hour of your screen in the background,<br>
and one key saves what just happened, from the last 15 seconds up to the full hour.

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Platform: Linux](https://img.shields.io/badge/platform-Linux-FCC624?logo=linux&logoColor=black)](#install)
[![Wayland and X11](https://img.shields.io/badge/Wayland%20%26%20X11-supported-5C3EE8)](#install)
[![Steam Deck and handhelds](https://img.shields.io/badge/Steam%20Deck%20%26%20handhelds-friendly-1A9FFF?logo=steam&logoColor=white)](#using-a-controller)
[![Status: alpha](https://img.shields.io/badge/status-alpha-orange.svg)](#faq)

</div>

---

## What is it?

Momento is the Linux version of the PS5's Create button. It records in the background all the time, so you never have to remember to start. When something worth keeping happens, press **Super + Shift + G**, pick how far back to go, and the clip is saved to `~/Videos/Momento`.

<p align="center"><img src="assets/clip-bar.png" width="880" alt="The Momento clip bar: logo, record dot, 12:34 recorded, 742 GB free, the lengths 15s to 60m with 5m highlighted, and settings, pause and stop buttons"></p>

## Features

- **Any game.** Steam, non-Steam, emulators, browser games, and cloud gaming like GeForce NOW.
- **Pick the length after the moment.** 15s, 30s, 1m, 3m, 5m, 15m, 30m or 60m.
- **MP4 in seconds.** No re-encoding, so a 1-minute clip is ready in about 3 seconds. Plays and uploads anywhere.
- **One shortcut.** Super + Shift + G opens a slim bar at the bottom of the screen.
- **Settings in the bar.** Resolution, 60 or 120 fps, quality, sound and mic.
- **Works with a controller.** Hold View + Menu to open the bar, then pick with the D-pad and A.
- **Pause and stop** from the bar whenever you want.
- **Screenshots too.** The camera button saves a picture of your game to `~/Videos/Momento/Images`.
- **Keeps your history** through restarts and reboots.
- **Protects your game.** Uses little memory and never fills your disk.
- **Light on performance.** Your graphics card does the heavy lifting.
- **Local only.** Nothing is uploaded. No account.

## How it works

Momento quietly keeps the last hour of what's on your screen, like a dashcam. Older footage is deleted as new footage comes in, so it never grows. When you pick a length, it saves exactly that much, ending right now.

```
  |<------------------- last 60 minutes, always kept ------------------->|
  |----------------------------------------------------------|-- 30 s ---|
                                                                         ^
                                          you press Super + Shift + G here
                                          -> Momento_..._30s.mp4
```

## How it compares

| | **Momento** | Steam Game Recording | OBS Replay Buffer |
|---|---|---|---|
| Records | **Any game or app: Steam, non-Steam, emulators, GeForce NOW and other cloud gaming** | Only games running through Steam | Whatever is in your OBS scene |
| Saving a clip | **One shortcut, pick 15 s to 60 min, MP4 ready in seconds** | Trim on a timeline, then export to MP4 | Always saves the whole buffer |
| Memory used for an hour of history | **About 150 MB**, plus about 100 MB for the instant bar (can be turned off) | History on disk | About 6.8 GB of RAM at 1080p |
| History kept across restarts and reboots | **Yes** | Not documented | No (RAM) |
| Protects your game | **Memory cap, gives way under pressure, never fills your disk** | Disk-space limit you set | Memory limit you set |
| Hotkey on Wayland desktops | **Yes, no plugins** | Steam shortcuts | Needs a plugin |
| Steam Gaming Mode | Records today; clip bar coming | Built in | Not designed for it |

The full comparison, including GPU Screen Recorder, is in [docs/COMPARISON.md](docs/COMPARISON.md).

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/mehulchachada/momento/main/install.sh | bash
```

Or clone the repo and run `./install.sh`. It installs any missing system packages (it shows you the command and asks first), installs Momento for your user and starts it.

| Distro | Notes |
|---|---|
| Bazzite, Aurora, Bluefin | Nothing extra. Everything is already in the system. |
| Fedora | The installer handles it. AMD needs RPM Fusion for GPU encoding. |
| Arch, CachyOS, EndeavourOS, Manjaro | The installer handles it. |
| Ubuntu 24.04+, Debian 13+, Pop!_OS, Mint | The installer handles it. |
| openSUSE Tumbleweed | The installer handles it. Add Packman for GPU encoding. |
| SteamOS | Not yet. A Flatpak is coming. |

To install the packages yourself, use the list for your distro, then run `./install.sh --no-deps`.

<details><summary><b>Fedora Workstation / KDE</b></summary>

```bash
sudo dnf install python3-gobject gstreamer1 gstreamer1-plugins-base python3-dbus python3-pyside6 pipewire-gstreamer gstreamer1-plugins-good gstreamer1-plugins-bad-free gstreamer1-plugin-libav ffmpeg-free gstreamer1-plugin-openh264 layer-shell-qt pulseaudio-utils
```
GPU encoding: AMD needs [RPM Fusion](https://rpmfusion.org/Configuration) and `mesa-va-drivers-freeworld`. Intel: `libva-intel-media-driver`. NVIDIA: the proprietary driver. On Silverblue / Kinoite these packages must be layered with `rpm-ostree install`, which slows every system update.
</details>
<details><summary><b>Arch, CachyOS, EndeavourOS, Manjaro</b></summary>

```bash
sudo pacman -S --needed python-gobject gstreamer gst-plugins-base-libs python-dbus pyside6 gst-plugin-pipewire gst-plugins-good gst-plugins-bad gst-plugin-va gst-libav gst-plugins-ugly ffmpeg layer-shell-qt libpulse
```
GPU encoding: AMD works out of the box. Intel: `intel-media-driver`. NVIDIA: the proprietary driver.
</details>
<details><summary><b>Ubuntu, Debian, Pop!_OS, Mint</b></summary>

```bash
sudo apt install python3-gi gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 gstreamer1.0-tools python3-dbus python3-pyside6.qtcore python3-pyside6.qtgui python3-pyside6.qtwidgets gstreamer1.0-pipewire gstreamer1.0-plugins-good gstreamer1.0-pulseaudio gstreamer1.0-plugins-bad gstreamer1.0-libav gstreamer1.0-plugins-ugly ffmpeg layer-shell-qt pulseaudio-utils
```
That's for Debian 13+ and Ubuntu 24.10+. On Ubuntu 24.04, Pop!_OS 24.04 and Mint 22, leave out the three `python3-pyside6.*` packages and `layer-shell-qt`, and add `python3-venv`. GPU encoding: AMD `mesa-va-drivers`, Intel `intel-media-va-driver`, NVIDIA the proprietary driver.
</details>
<details><summary><b>openSUSE Tumbleweed</b></summary>

```bash
sudo zypper install python3-gobject typelib-1_0-Gst-1_0 typelib-1_0-GstVideo-1_0 gstreamer-utils python3-dbus-python python3-pyside6 gstreamer-plugin-pipewire gstreamer-plugins-good gstreamer-plugins-bad gstreamer-plugins-libav ffmpeg layer-shell-qt6 pulseaudio-utils
```
GPU encoding: AMD `Mesa-libva`, Intel `intel-media-driver`, plus [Packman](https://en.opensuse.org/Additional_package_repositories#Packman): `sudo zypper dup --from packman --allow-vendor-change`.
</details>

**Checking the install:** run `./install.sh --check` to see what's installed and what's missing.

## First launch

The first time Momento starts, your desktop asks what to share, like when you share your screen on Discord.

1. Pick **your monitor**, not a single window.
2. Tick **Remember** or **Allow restoring** if you see it, so it doesn't ask again.
3. Click **Share**. If your desktop asks you to confirm the shortcut, accept it or pick another key.

## Using it

| Do this | Result |
|---|---|
| **Super + Shift + G** | Opens the clip bar |
| Click or tap a length | Saves that much, ending right now |
| **Left/Right + Enter**, or **1** to **8** | Picks a length with the keyboard |
| **P**, or the pause button | Pauses or resumes recording. Your history is kept |
| **S**, or the gear button | Opens settings in the bar |
| The stop button | Stops recording and clears the history (the bar asks first). The bar then shows **Start** |
| The camera button | Takes a screenshot. The bar gets out of the way first, and a notification tells you it's saved |
| **Esc** | Closes the bar without saving |

The **Super** key is the Windows / start-menu key. Momento uses Super + Shift + G because KDE already uses Super + G.

```bash
momento save 30s        # save without opening the bar (15s, 1m, 5m, 60m, 90s...)
momento screenshot      # save a picture of your game
momento status          # is it recording, and how much is kept
momento settings        # current settings and your sound devices
momento set fps 120     # change a setting
momento pause           # pause (what you have can still be saved)
momento resume          # resume
momento stop            # stop recording and clear the history
```

## Using a controller

Hold **View + Menu** (the two small buttons in the middle) for half a second to open the bar. Do it again to close it.

| Button | In the bar |
|---|---|
| **D-pad** or left stick | Move around |
| **A** | Save the selected length, or choose |
| **B** | Back, or close the bar |
| **Y** | Settings |
| **X** | Pause or resume recording |
| **LB / RB** | Jump between the lengths and the buttons |

Buttons go by position, so on a PlayStation controller A is Cross, B is Circle, Y is Triangle and X is Square. On a Nintendo controller the bottom button saves and the right one goes back.

While the bar is open, Momento takes over the controller so your game doesn't react to your presses, and gives it back when the bar closes. If another app already holds the controller, the game may still see them. The shortcut itself does reach the game, so pick one your game doesn't use.

To change the shortcut, open settings (**Y**) and choose under **Controller**:

- **View + Menu** (the default)
- **Left paddle** or **Right paddle**: the back buttons on the ROG Ally and Xbox Elite controllers
- **L3 + R3**: press both sticks in
- **Off**

Or in a terminal: `momento set controller left_paddle`. If a paddle seems swapped or a button does nothing, run `momento controller --watch` and press it to see what Momento gets.

**Steam Gaming Mode:** the bar can't appear over the game there yet. Use Steam Input to bind a button to `momento save 30s` instead.

## Video quality

Free space Momento needs to start (a full hour of history in brackets):

| Resolution | Standard | High (default) | Ultra |
|---|---|---|---|
| **1080p, 60 fps** (default) | 6 GB (4.8) | **8 GB (7.2)** | 13 GB (11.9) |
| 1080p, 120 fps | 8 GB (7.2) | 12 GB (10.5) | 19 GB (18) |
| 1440p, 60 fps | 9 GB (7.6) | 12 GB (11.4) | 20 GB (19) |
| 2160p (4K), 60 fps | 15 GB (14.3) | 22 GB (21.3) | 34 GB (33.2) |

- **Handheld or 1080p screen:** 1080p High, 60 fps (the default).
- **120 Hz screen and fast games:** 1080p High, 120 fps.
- **1440p or 4K monitor:** 1440p High.
- **Different screen shape** (like a 16:10 handheld): you get black bars. The picture is never stretched.

## Performance

- About **4.5% of one CPU core**.
- About **2-3% lower FPS** in a heavy graphics test.
- About **1.5 W** of extra power.
- About **150 MB** of memory, plus about 100 MB for the instant bar (can be turned off).
- About **7 GB written to disk per hour**.
- A 1-minute clip saves in about **3 seconds**.

These were measured on one ROG Ally (Z1 Extreme) on a light desktop workload. Results will vary with other hardware and games.

<details><summary><b>Test machine</b></summary>

ASUS ROG Ally (Z1 Extreme, 16 GB), Bazzite 44, KDE Plasma 6.7 on Wayland, on AC power. Momento 0.1.0 at 1080p, High, 60 fps, desktop sound on, mic off. September 2026.
</details>

## Storage warnings

Momento checks free space so it never fills your disk.

| When | What happens |
|---|---|
| Not enough space to start | It doesn't start and shows **Low storage**. It starts by itself once there's room. |
| Disk almost full while recording | It stops recording. What you already have can still be saved. |
| A setting that won't fit | That option gets a red mark and **Apply** is disabled. |
| Free-space hint in the bar | Green: plenty of room. Yellow: getting tight. Red: not enough. |

## Settings

Change settings with the **gear** in the bar, or with `momento set …` in a terminal. Everything lives in `~/.config/momento/config.toml`. If you edit that file by hand, restart Momento: `systemctl --user restart momento.service`.

| Setting | Key | Default |
|---|---|---|
| Record | `[capture] target` | `screen` (full screen). `window` records only the game window you pick |
| Resolution | `[capture] resolution` | `1080p` (also `720p`, `1440p`, `2160p`, `native`) |
| Frame rate | `[capture] fps` | `60` (or `120`) |
| Quality | `[capture] quality` | `high` (also `standard`, `ultra`) |
| Game sound | `[audio] desktop` | `true` |
| Microphone | `[audio] microphone` | `false` |
| Clip folder | `[output] dir` | your Videos folder + `/Momento` |
| Shortcut | `[hotkey] trigger` | `LOGO+SHIFT+g` (Super + Shift + G) |
| Controller shortcut | `[controller] open_chord` | `["select", "start"]` (View + Menu), held `hold_ms = 500`. `enabled = false` turns controllers off |
| History length | `[buffer] max_seconds` | `3600` (60 min, the maximum) |
| Instant bar | `[ui] keep_bar_loaded` | `true`: keeps the bar ready so it opens immediately; uses ~100 MB. Set `false` to save memory |

## FAQ

<details><summary><b>My clip is black or shows the wrong screen</b></summary>

The wrong thing was picked in the share dialog. Reset it and pick your monitor again:

```bash
rm ~/.local/state/momento/portal_token
systemctl --user restart momento.service
```
</details>
<details><summary><b>Keep the Momento bar out of my clips</b></summary>

On KDE Plasma 6.6 or newer this is automatic: the bar never shows up in your clips. Elsewhere, open settings and set **Record** to **Game window**, so Momento records only your game (no bar, no notifications). You pick the game window once. When the game closes, open the bar and press play to pick it again.
</details>
<details><summary><b>My clip has no sound</b></summary>

Momento records your **default** sound output. If the game plays through another device, make that the default, or pick it under **Sound** in the bar's settings. Also check that sound isn't set to Off.
</details>
<details><summary><b>The bar doesn't show over my game on GNOME</b></summary>

GNOME doesn't let apps draw over exclusive fullscreen games. Switch the game to borderless or windowed fullscreen. Or bind `momento save 30s` to a key in *Settings → Keyboard → Custom Shortcuts*.
</details>
<details><summary><b>Does it work in Steam Gaming Mode?</b></summary>

Recording works, but the bar can't appear there yet. Bind a controller button to `momento save 30s` through Steam Input.
</details>
<details><summary><b>Super + Shift + G does nothing</b></summary>

Check that `momento status` says *recording*. On KDE, look for Momento under *System Settings → Keyboard → Shortcuts*. If your desktop has no app shortcuts, add a custom shortcut that runs `momento overlay`.
</details>
<details><summary><b>How much disk space does it use? Will it wear out my SSD?</b></summary>

At the default settings the history takes about 7.2 GB and never grows. Momento writes about 7 GB per hour, a small part of a typical SSD's rated life. To write less, pick a lower resolution or quality, or a shorter history.
</details>
<details><summary><b>Does it slow my games down?</b></summary>

Barely, on our test machine (see [Performance](#performance)). If the installer says only a *software* encoder was found, you will notice it. Install your graphics driver's video support from the [Install](#install) section.
</details>
<details><summary><b>Running <code>momento</code> starts a different program</b></summary>

An unrelated developer tool is also called `momento`. Check with `command -v momento`. This app lives in `~/.local/bin/momento`; put `~/.local/bin` first in your `PATH`, or use the full path. The shortcut and app menu are not affected.
</details>

## Uninstall

```bash
~/.local/share/momento/install.sh --uninstall   # removes Momento, keeps settings and clips
~/.local/share/momento/install.sh --purge       # also removes settings and the history (clips are never deleted)
```

## Roadmap

- Flatpak on Flathub, including SteamOS
- The clip bar in Steam Gaming Mode
- Smaller files (HEVC and AV1)
- Microphone on its own audio track

## Contributing

Bug reports, testing and pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE) © 2026 Momento contributors
