<div align="center">

<img src="assets/logo.svg" width="160" alt="Momento logo">

# Momento

**Never miss the moment.**

Instant replay for Linux gaming. Momento keeps the last hour of your screen in the background,<br>
and one key saves what just happened, from the last 15 seconds up to the full hour.

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Platform: Linux](https://img.shields.io/badge/platform-Linux-FCC624?logo=linux&logoColor=black)](#install)
[![Wayland and X11](https://img.shields.io/badge/Wayland%20%26%20X11-supported-5C3EE8)](#install)
[![Steam Deck and handhelds](https://img.shields.io/badge/Steam%20Deck%20%26%20handhelds-friendly-1A9FFF?logo=steam&logoColor=white)](#binding-a-controller-button)
[![Status: alpha](https://img.shields.io/badge/status-alpha-orange.svg)](#faq-and-troubleshooting)

</div>

---

## What is it?

Momento is the Linux version of the PS5's Create button or Xbox's "record that". It records in the background all the time, so you never have to remember to start recording. When something worth keeping happens, press **Super + Shift + G**. A slim bar slides in at the bottom of the screen. Pick how far back to go, and the clip is saved to your **Videos** folder.

<p align="center"><img src="assets/clip-bar.png" width="716" alt="The Momento clip bar: record dot, 12:34 buffered, and the lengths 15s 30s 1m 3m 5m 15m 30m 60m with 5m selected"></p>

It works with Steam games, emulators, browser games and anything else on your screen. It runs on desktops, laptops and handhelds like the ROG Ally, Legion Go and Steam Deck.

## Features

- **Always recording.** Keeps up to the last 60 minutes, including sound.
- **One shortcut.** Super + Shift + G brings up a slim clip bar at the bottom of the screen, on top of your game.
- **Eight clip lengths.** 15 s, 30 s, 1 min, 3 min, 5 min, 15 min, 30 min and 60 min.
- **Fast saves.** Clips aren't re-encoded, so even a full hour is saved in a few seconds.
- **Light on performance.** Your graphics card's video engine does the encoding on AMD, Intel and NVIDIA. On our test handheld it used about 4.5% of one CPU core (see [Performance](#performance)).
- **Game audio included.** Momento records whatever you're hearing and follows you when you switch between speakers, headphones and HDMI. Microphone recording is optional.
- **Settings in the bar.** A gear button in the clip bar changes resolution, quality, sound and mic without touching a config file.
- **Controller-friendly.** Bind it to a button through Steam Input, or trigger saves from the command line.
- **Works on KDE Plasma, GNOME, Hyprland, Sway and X11**, and can record Steam Gaming Mode.
- **Local only.** Nothing is uploaded, and you don't need an account.
- **No root needed.** Installs for your user only, which suits Bazzite, SteamOS and other immutable systems.

## How it works

1. **Momento records quietly in the background.** It keeps only the most recent hour and deletes older footage as it goes, so disk use stays fixed.
2. **Something happens that you want to keep.** Press **Super + Shift + G**.
3. **The clip bar slides in at the bottom of the screen** with the eight lengths in a row. Pick one.
4. **The last X minutes, up to right now, are saved** to `~/Videos/Momento/`.

```
  |<------------------- last 60 minutes, always kept ------------------->|
  |----------------------------------------------------------|-- 30 s ---|
                                                                         ^
                                          you press Super + Shift + G here
                                          -> Momento_..._30s.mp4
```

### How big is a clip?

At the default settings (1080p, High quality, 60 fps):

| Clip length | 15 s | 30 s | 1 min | 3 min | 5 min | 15 min | 30 min | 60 min |
|---|---|---|---|---|---|---|---|---|
| File size (approx.) | 28 MB | 57 MB | 114 MB | 340 MB | 570 MB | 1.7 GB | 3.4 GB | 6.8 GB |

Clips are ordinary MP4 files. They play in any video player and upload directly to Discord, YouTube and similar sites.

## How it compares

| | **Momento** | Steam Game Recording | OBS Replay Buffer |
|---|---|---|---|
| Records | **Any game or app: Steam, non-Steam, emulators, GeForce NOW and other cloud gaming** | Only games running through Steam | Whatever is in your OBS scene |
| Saving a clip | **One shortcut, pick 15 s to 60 min, MP4 ready in seconds** | Trim on a timeline, then export to MP4 | Always saves the whole buffer |
| Memory used for an hour of history | **About 150 MB** (history on disk) | History on disk | About 6.8 GB of RAM at 1080p |
| History kept across restarts and reboots | **Yes** | Not documented | No (RAM) |
| Protects your game | **Memory cap, gives way under pressure, never fills your disk** | Disk-space limit you set | Memory limit you set |
| Hotkey on Wayland desktops | **Yes, no plugins** | Steam shortcuts | Needs a plugin |
| Steam Gaming Mode | Records today; clip bar coming | Built in | Not designed for it |

Momento is built for one job: saving what just happened in any game, fast, without getting in the way. See [docs/COMPARISON.md](docs/COMPARISON.md) for the full comparison (including GPU Screen Recorder), sources and advice on which to use.

## Install

Paste this into a terminal:

```bash
curl -fsSL https://raw.githubusercontent.com/mehulchachada/momento/main/install.sh | bash
```

Or clone the repo and run the installer from it (`make install` does the same):

```bash
git clone https://github.com/mehulchachada/momento.git && cd momento && ./install.sh
```

**What it does:** installs any missing system packages (it shows you the exact command and asks first), installs Momento for your user, and starts it. That's it: press **Super + Shift + G** in a game to save a clip.

Works on Fedora, Bazzite / Bluefin / Aurora, Arch / CachyOS / EndeavourOS / Manjaro, Ubuntu / Debian / Pop!_OS / Mint and openSUSE. You need Ubuntu 24.04+ or Debian 13+ on the Debian side.

Useful options (with the one-liner, put them after `bash -s --`, e.g. `... | bash -s -- --yes`):

| Option | What it does |
|---|---|
| `--yes` | don't ask, install missing packages right away |
| `--no-deps` | skip the system packages, you handle them |
| `--no-enable` | install, but don't start Momento or add it to login |
| `--check` | only show what's installed and what's missing |
| `--update` | download the latest version and reinstall |

**Bazzite, Bluefin and Aurora** already ship everything Momento needs in the system image, so nothing gets layered and no `sudo` is needed. On plain **Fedora Silverblue / Kinoite** the installer never layers packages on its own: it prints the `rpm-ostree install` command and explains the trade-off (layering slows down every system update). On **SteamOS** it points you to an Arch distrobox for now, and on **NixOS** it lists what to add to your config. A Flatpak is planned for all three.

**Uninstall** (keeps your clips):

```bash
curl -fsSL https://raw.githubusercontent.com/mehulchachada/momento/main/install.sh | bash -s -- --uninstall
```

### Installing the packages yourself

Prefer to do it by hand? These are the exact packages the installer uses, tested on each distro. Afterwards run the installer with `--no-deps`.

<details>
<summary><b>Fedora Workstation / KDE</b></summary>

```bash
sudo dnf install python3-gobject gstreamer1 gstreamer1-plugins-base python3-dbus python3-pyside6 pipewire-gstreamer gstreamer1-plugins-good gstreamer1-plugins-bad-free gstreamer1-plugin-libav ffmpeg-free gstreamer1-plugin-openh264 layer-shell-qt pulseaudio-utils
```

GPU encoding (without it Momento falls back to the CPU):

- **AMD:** Fedora's own Mesa can't encode H.264. Enable [RPM Fusion](https://rpmfusion.org/Configuration), then `sudo dnf install mesa-va-drivers-freeworld` (the installer adds it for you once RPM Fusion is enabled).
- **Intel:** `sudo dnf install libva-intel-media-driver`
- **NVIDIA:** the proprietary driver from RPM Fusion. The NVENC plugin is already in `gstreamer1-plugins-bad-free`.
</details>

<details>
<summary><b>Bazzite, Bluefin, Aurora, Silverblue, Kinoite (Fedora Atomic)</b></summary>

Bazzite, Bluefin and Aurora: nothing to install, just run the installer.

Silverblue / Kinoite: the Fedora packages above have to be layered (`rpm-ostree install ...`, then reboot). It works, but it slows down every system update. The alternative is to run Momento inside a Fedora [distrobox](https://distrobox.it/).
</details>

<details>
<summary><b>Arch Linux, CachyOS, EndeavourOS, Manjaro</b></summary>

```bash
sudo pacman -S --needed python-gobject gstreamer gst-plugins-base-libs python-dbus pyside6 gst-plugin-pipewire gst-plugins-good gst-plugins-bad gst-plugin-va gst-libav gst-plugins-ugly ffmpeg layer-shell-qt libpulse
```

GPU encoding: AMD works out of the box (the VA-API driver is part of `mesa`). Intel: `intel-media-driver`. NVIDIA: the NVENC plugin comes with `gst-plugins-bad`, you only need the proprietary driver.
</details>

<details>
<summary><b>Ubuntu, Debian, Pop!_OS, Linux Mint</b></summary>

Debian 13+ / Ubuntu 24.10+:

```bash
sudo apt install python3-gi gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 gstreamer1.0-tools python3-dbus python3-pyside6.qtcore python3-pyside6.qtgui python3-pyside6.qtwidgets gstreamer1.0-pipewire gstreamer1.0-plugins-good gstreamer1.0-pulseaudio gstreamer1.0-plugins-bad gstreamer1.0-libav gstreamer1.0-plugins-ugly ffmpeg layer-shell-qt pulseaudio-utils
```

**Ubuntu 24.04, Pop!_OS 24.04 and Mint 22** don't package PySide6. Install the same list without the three `python3-pyside6.*` packages and without `layer-shell-qt` (24.04 only has the Qt 5 version), plus `python3-venv`. The installer then downloads PySide6 from PyPI into Momento's own folder (`~/.local/share/momento/venv`), so your system Python stays untouched.

GPU encoding: `mesa-va-drivers` for AMD, `intel-media-va-driver` for Intel. NVIDIA's NVENC plugin is in `gstreamer1.0-plugins-bad`, you only need the proprietary driver.
</details>

<details>
<summary><b>openSUSE Tumbleweed</b></summary>

```bash
sudo zypper install python3-gobject typelib-1_0-Gst-1_0 typelib-1_0-GstVideo-1_0 gstreamer-utils python3-dbus-python python3-pyside6 gstreamer-plugin-pipewire gstreamer-plugins-good gstreamer-plugins-bad gstreamer-plugins-libav ffmpeg layer-shell-qt6 pulseaudio-utils
```

GPU encoding: `Mesa-libva` for AMD, `intel-media-driver` for Intel. openSUSE's default repositories leave out H.264 GPU encoding, so for that add [Packman](https://en.opensuse.org/Additional_package_repositories#Packman) and run `sudo zypper dup --from packman --allow-vendor-change`. Without it Momento records with the CPU encoder.
</details>

<details>
<summary><b>SteamOS (Steam Deck)</b></summary>

SteamOS wipes system changes on every update, so don't install packages on the host. Until the Flatpak is ready, the route is an Arch [distrobox](https://distrobox.it/) (experimental). Run the installer inside it; the `momento` launcher it creates hops into the box by itself when started from the desktop:

```bash
distrobox create -i archlinux:latest momento && distrobox enter momento
curl -fsSL https://raw.githubusercontent.com/mehulchachada/momento/main/install.sh | bash
```
</details>

### Checking the install

The installer ends with a checklist. For anything missing, it names the package to install on your distro. Run the check again at any time with:

```bash
./install.sh --check      # or: ~/.local/share/momento/install.sh --check
```

## First launch

The first time Momento starts, your desktop asks what you want to share. It's the same dialog you see when sharing your screen on Discord or in a video call.

1. Choose **your monitor**, not a single window, so recording keeps working when games open new windows.
2. If there's a **Remember** or **Allow restoring** checkbox, tick it so the dialog doesn't come back after a restart.
3. Click **Share** or **OK**.

Your desktop may also ask you to confirm the **Super + Shift + G** shortcut. Accept it, or choose a different key there.

> The **Super** key is the one with the Windows logo, the key that opens your start menu. Hold Super and Shift, then tap G. (Plain Super + G is KDE's own Grid View, which is why Momento adds Shift.)

Momento is now recording. To check, run:

```bash
momento status
```

## Using it

| Do this | Result |
|---|---|
| **Super + Shift + G** | The clip bar slides in at the bottom of the screen |
| Click or tap a length | Saves that much, ending right now |
| **Left/Right** + **Enter**, or **1** to **8** | Picks a length with the keyboard |
| **Esc**, or Super + Shift + G again | Closes the bar without saving |
| **P**, or the pause button | Pauses recording. What's already buffered can still be saved. Press again to resume; your replay history is kept |
| The stop button | Shuts Momento down after you confirm it in the bar. When Momento is off, the bar shows **Start** instead |
| **S**, or the gear button | Opens settings right in the bar (see [Settings](#settings)) |

The pause, stop and gear buttons sit at the right end of the bar. Press **Right** past 60m to reach them.

**From a terminal or a script:**

```bash
momento save 30s        # save the last 30 seconds without opening the bar
momento save 5m         # also 15s, 1m, 3m, 15m, 30m, 60m, or e.g. 90s, 2m
momento overlay         # open the clip bar
momento status          # recording state and how much is buffered
momento settings        # current video and audio settings, plus your audio devices
momento pause           # pause recording (the buffer is kept and can be saved)
momento resume          # resume recording (history is kept)
momento quit            # stop recording (or: momento stop)
```

The Momento entry in your app menu opens the clip bar. Right-click it to start the recorder or save a quick 30-second or 5-minute clip.

## Binding a controller button

You can turn a button, or a chord of two buttons, into the Momento shortcut with Steam Input:

1. In Steam, open the game's **Controller settings**. For apps outside Steam, use *Settings → Controller → Desktop Layout*.
2. Choose a button you don't use, or a chord such as **View + a back button**.
3. Map it to **Keyboard key → Super + Shift + G** to open the clip bar. For instant saves without the bar, map it instead to a key you've bound to `momento save 30s` in your desktop's shortcut settings.

**ROG Ally / Ally X:** the back paddles, or the Armoury Crate and Command Center buttons, work well. On Bazzite they can be remapped through Steam Input like any other button.

**Legion Go:** try the Legion L/R buttons or the rear Y1-Y3 buttons.

**Steam Deck:** the four back grips (L4, L5, R4, R5) are a natural fit.

In Gaming Mode the clip bar can't appear over the game yet, so bind a button to a fixed-length save such as `momento save 30s` instead. See the [FAQ](#faq-and-troubleshooting).

## Where clips go

Clips are saved to `~/Videos/Momento/`. Momento uses your system's Videos folder, even if it has a different name in your language. File names look like this:

```
Momento_2026-09-26_21-14-03_30s.mp4
```

You can choose a different folder in the settings.

## Video quality

You choose the **resolution**, the **quality** and the **frame rate** (60 or 120 fps). Together they decide how sharp clips look and how much disk space the 60-minute history takes. Change them with the gear button in the clip bar, or from a terminal.

**Free space Momento needs to start**, with the size of a full hour of history in brackets:

| Resolution | Standard | High (default) | Ultra |
|---|---|---|---|
| 720p, 60 fps | 4 GB (2.9) | 6 GB (4.8) | 8 GB (7.2) |
| **1080p, 60 fps** (default) | 6 GB (4.8) | **8 GB (7.2)** | 13 GB (11.9) |
| 1080p, 120 fps | 8 GB (7.2) | 12 GB (10.5) | 19 GB (18) |
| 1440p, 60 fps | 9 GB (7.6) | 12 GB (11.4) | 20 GB (19) |
| 1440p, 120 fps | 12 GB (11.4) | 18 GB (17.1) | 29 GB (28.4) |
| 2160p (4K), 60 fps | 15 GB (14.3) | 22 GB (21.3) | 34 GB (33.2) |
| 2160p (4K), 120 fps | 22 GB (21.3) | 33 GB (32.2) | 51 GB (49.7) |
| Native | same as 1440p | | |

The history never grows past its size, because the oldest footage is deleted as new footage comes in. The extra ~1 GB on top is kept free on purpose so Momento never fills your disk.

**Recommendations:**
- **Handheld or 1080p monitor:** 1080p High, 60 fps (the default).
- **120 Hz screen and fast games:** 1080p High, 120 fps.
- **1440p or 4K monitor:** 1440p High.

120 fps only helps if your screen runs at 120 Hz or more.

```bash
momento set resolution 1440p     # 720p, 1080p, 1440p, 2160p, native
momento set quality ultra        # standard, high, ultra
momento set fps 120              # 60, 120
momento settings                 # show the current values and the space they need
```

If your screen's shape doesn't match the resolution you picked (for example a 16:10 handheld recording at 1080p), the video gets black bars. It is never stretched.

## Performance

These numbers come from **one test machine** and a light desktop workload, not from a range of hardware or games. Your results will differ with other GPUs, drivers, screens and workloads.

<details>
<summary><b>Test machine and conditions</b></summary>

| | |
|---|---|
| Device | ASUS ROG Ally (RC71L) |
| CPU | AMD Ryzen Z1 Extreme, 8 cores / 16 threads |
| GPU | AMD Radeon 780M (integrated), H.264 via VA-API |
| Memory | 16 GB shared: 6 GB reserved as VRAM, 9.4 GB visible to Linux |
| Storage | external SanDisk Extreme USB SSD, 1 TB |
| OS | Bazzite 44 (Fedora 44 base), kernel 7.2, Mesa 26.2, GStreamer 1.28 |
| Desktop | KDE Plasma 6.7 on Wayland |
| Displays | built-in 1920x1080 at 120 Hz + external 1920x1080 at 120 Hz |
| Power | on AC power, `performance` power profile |
| Momento settings | 1080p, High, 60 fps (15 Mbps), desktop audio on, mic off |
| Workload | desktop with light use (browser, terminal); no game running |
| Method | Momento's own CPU from its systemd cgroup; package power from the amdgpu sensor; 100-180 s windows split into 10 s buckets, repeated; frame rate impact from an offscreen GPU-bound OpenGL test, 3-6 runs each; captured motion from a 60 fps test animation |
| Date | September 2026, Momento 0.1.0 |

Not measured yet: battery drain, real games, other GPUs (Intel, NVIDIA, desktop AMD), X11, GNOME.
</details>

Results on that machine, comparing Momento running against Momento stopped:

| | Cost |
|---|---|
| CPU | about **4.5% of one core** (0.3% of the whole processor) |
| Graphics | about **+3%** busy; the video encoding runs on the GPU's separate video engine |
| Frame rate in a GPU-heavy test | **about 2-3% lower**, which is within normal run-to-run variation |
| Power | about **+1.5 W** for the whole chip |
| Memory | about **150 MB** |
| Disk writes | about **7 GB per hour** (1.9 MB/s) |
| Saving a 1-minute clip | about **3 seconds**, with no lasting cost |
| Captured motion | **54-58 unique frames per second** on a 60 fps animation |

Higher resolutions, Ultra quality and 120 fps write more data per hour (see the table above) but still run on the video engine, so the CPU cost should stay small; they were not measured separately. Nothing is re-encoded when you save, which is why saves are fast and don't cause stutter. Battery life impact has not been measured yet.

For a comparison with OBS and Steam's own recorder, see [How it compares](#how-it-compares).

## Storage and warnings

Momento checks your free disk space so it never fills your drive:

| When | What Momento does | What you see |
|---|---|---|
| **Starting or resuming**, and there isn't enough space for your settings (see the table above) | Doesn't start. It checks again every 30 seconds and starts by itself once there is room. | The bar shows a red dot and **Low storage**, with a line above it: *Not enough free space: needs 8 GB, 3.1 GB free.* One desktop notification. |
| **While recording**, free space drops below 512 MB | Stops recording. Footage it already has can still be saved. | The same Low storage warning. |
| **Changing settings** to something that won't fit | Refuses the change and keeps your current settings. | In the gear menu, options that won't fit get a small red mark, the footer shows e.g. *Needs 19 GB · 9 GB free*, and **Apply** is disabled. |
| **Saving a clip**, and the clip folder's drive is too full | Doesn't save. | *Not enough space to save this clip: needs X, Y free.* |

Space that Momento's own history already uses counts as available, because it gets reused.

To fix a warning, free up space on the drive, pick a lower resolution, quality or frame rate, or move the history to a bigger drive with `[buffer] dir` in the settings file.

**Your history is kept** when you pause and resume, change a setting, restart Momento or reboot. Only **Stop** in the clip bar clears it, and the bar asks before doing that. Clip lengths count recorded footage: "5m" is the last 5 minutes Momento actually recorded, even if you paused in between. If you changed resolution partway through, a clip only includes the part recorded at the current resolution, and Momento tells you it's shorter than asked.

## Settings

**In the clip bar:** press **S** or select the gear. The bar grows upward into a few rows:

- **Resolution:** 720p, 1080p, 1440p, 4K or Native
- **Quality:** Standard, High or Ultra
- **Sound:** your default output (it follows you when you switch between speakers, headphones and HDMI), one specific output, or Off
- **Mic:** Off or On. With On you can also pick which mic to use

**Up/Down** moves between rows and **Left/Right** changes the value. You can also tap an option. The bottom line shows how much disk space the 60-minute history will take. **Enter** applies the change and restarts recording; your replay history is kept. **Esc** goes back without changing anything. If Momento is off, the change is saved and used the next time it starts.

**From a terminal:**

```bash
momento set audio_source off                  # default, off, or a name from `momento settings`
momento set mic on                            # on, off
momento set mic_device default                # default, or a name from `momento settings`
```

All settings are stored in `~/.config/momento/config.toml`, a plain text file with a comment next to each option. If you edit the file by hand, restart Momento afterwards:

```bash
systemctl --user restart momento.service
```

The settings most people change:

| Setting | Key | Default | Notes |
|---|---|---|---|
| Resolution | `[capture] resolution` | `"1080p"` | `720p`, `1080p`, `1440p`, `2160p` or `native` (your screen's own size). See [Video quality](#video-quality) |
| Quality | `[capture] quality` | `"high"` | `standard`, `high` or `ultra` |
| Hotkey | `[hotkey] trigger` | `"LOGO+SHIFT+g"` (Super + Shift + G) | For example `"CTRL+ALT+r"` or `"F9"`. Your desktop's shortcut settings can also change it |
| Microphone | `[audio] microphone` | `false` | `true` mixes your mic into clips |
| Game and desktop sound | `[audio] desktop` | `true` | |
| Clip folder | `[output] dir` | `""` (your Videos folder + `/Momento`) | Any folder. `~` works |
| History length | `[buffer] max_seconds` | `3600` (60 min) | Lower it to use less disk space |
| Mouse cursor | `[capture] show_cursor` | `false` | |

Experts can set an exact video bitrate with `[capture] bitrate_kbps`. The default, `0`, picks it automatically from the resolution and quality.

## FAQ and troubleshooting

<details>
<summary><b>My clip is black or shows the wrong screen</b></summary>

The screen-share dialog got the wrong choice, or it was denied. To make Momento forget that choice and ask again, run:

```bash
rm ~/.local/state/momento/portal_token
systemctl --user restart momento.service
```

Then pick your whole monitor in the dialog. If you have more than one monitor, pick the one you play on.
</details>

<details>
<summary><b>My clip has no sound</b></summary>

Momento records whatever your **default** output is playing (speakers, headphones, Bluetooth or HDMI), and follows along when you switch. If you hear sound but the clip is silent, the game is probably playing to a device that isn't the default: pick that device as the default in your sound settings, or choose it under **Sound** in the clip bar's gear menu. Also check that sound isn't set to *Off* there, and that `./install.sh --check` finds an AAC encoder.
</details>

<details>
<summary><b>The clip bar doesn't appear over my game on GNOME</b></summary>

GNOME doesn't let other apps draw over games running in exclusive fullscreen. Switch the game to borderless or windowed fullscreen and the bar will appear.

You can also save without the bar: bind `momento save 30s` to a key in *Settings → Keyboard → Custom Shortcuts*. KDE Plasma (with `layer-shell-qt` installed), Hyprland and Sway don't have this limitation.
</details>

<details>
<summary><b>Does it work in Steam Gaming Mode?</b></summary>

Recording works: in Gaming Mode, Momento records Steam's own game output. The clip bar doesn't work yet, because Gaming Mode doesn't let other apps draw over games. For now, bind a controller button to `momento save 30s` (or another length) through Steam Input, or use Steam's built-in Game Recording. Support for the clip bar in Gaming Mode is on the roadmap.
</details>

<details>
<summary><b>The screen-share dialog appears every time</b></summary>

Tick **Remember** or **Allow restoring** in the dialog. If your desktop doesn't offer that option, you need a newer version (KDE Plasma 6 or GNOME 46+).
</details>

<details>
<summary><b>How much disk space does it use? Will it wear out my SSD?</b></summary>

At the default settings (1080p High, 60 fps), the history takes about 7.2 GB and never grows past that, and Momento needs about 8 GB free to start (see [Storage and warnings](#storage-and-warnings)). While it runs, it writes about 7 GB per hour. A typical 1 TB SSD is rated for several hundred terabytes of writes, so even a few hours of gaming a day uses a small part of its rated life.

To write less, use a lower resolution, Standard quality or 60 fps, or lower `max_seconds`. You can also move the buffer to another drive with `[buffer] dir`.
</details>

<details>
<summary><b>Does it slow my games down?</b></summary>

On our test machine (a ROG Ally), Momento used about 4.5% of one CPU core and cost about 2-3% frame rate in a GPU-heavy test. It hasn't been measured in real games or on other hardware yet. See [Performance](#performance) for the full numbers and test conditions. If the installer reports that only a *software* encoder was found, you will notice it, especially on a handheld. Install your GPU's video driver from the install section above.
</details>

<details>
<summary><b>Why does my clip start a second earlier than I picked?</b></summary>

Clips are cut without re-encoding, which is what makes saving fast. The trade-off is that a clip has to start at the nearest keyframe, so clip lengths are accurate to about one second.
</details>

<details>
<summary><b>Super + Shift + G does nothing</b></summary>

First check that Momento is running: `momento status` should say *recording*. Remember it's Super **+ Shift** + G: plain Super + G is KDE's Grid View.

On KDE, look for **Momento** under *System Settings → Keyboard → Shortcuts*; you can change the key there too. Shortcuts registered by apps need a recent desktop (KDE Plasma 6, GNOME 48+ or Hyprland). If yours doesn't support them, add a custom shortcut in your keyboard settings that runs `momento overlay`.
</details>

<details>
<summary><b>Running <code>momento</code> starts a different program</b></summary>

A developer tool from an unrelated company, the command-line client of the *Momento* serverless cache service, is also called `momento`. Most gamers will never have it, but if you do, check which one runs with `command -v momento`. This app installs its command to `~/.local/bin/momento`: put `~/.local/bin` earlier in your `PATH`, or call it by its full path. The app-menu entry, the hotkey and the background service always use the full path, so they are not affected.
</details>

<details>
<summary><b>How can I see what it's doing?</b></summary>

```bash
momento status
journalctl --user -u momento.service -f
```
</details>

## Uninstall

```bash
~/.local/share/momento/install.sh --uninstall   # removes Momento, keeps settings and clips
~/.local/share/momento/install.sh --purge       # also deletes settings and the replay history
```

The installer keeps a copy of itself there, so this works whether you used the one-liner or a clone (from a clone, `./install.sh --uninstall` works too). System packages Momento installed for you (GStreamer plugins and so on) are left in place, since other apps may use them.

Your saved clips are never deleted.

## Roadmap

- Flatpak on Flathub for an easy install everywhere, including SteamOS
- The clip bar inside Steam Gaming Mode
- Full controller navigation in the clip bar
- HEVC and AV1 for smaller files
- Microphone on its own audio track

## Contributing and credits

Bug reports, testing on more distros and handhelds, and pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md).

Momento is built on [GStreamer](https://gstreamer.freedesktop.org/), [PipeWire](https://pipewire.org/), [xdg-desktop-portal](https://flatpak.github.io/xdg-desktop-portal/), [FFmpeg](https://ffmpeg.org/), [Qt for Python](https://doc.qt.io/qtforpython-6/) and KDE's [layer-shell-qt](https://invent.kde.org/plasma/layer-shell-qt). The idea comes from the PS5's Create button.

**For developers:** see [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) for how it works under the hood, the dev setup and the tests.

## License

[MIT](LICENSE) © 2026 Momento contributors
