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

Momento is the Linux version of the PS5's Create button or Xbox's "record that". It records in the background all the time, so you never have to remember to start recording. When something worth keeping happens, press **Super + G**. A slim bar slides in at the top of the screen. Pick how far back to go, and the clip is saved to your **Videos** folder.

<p align="center"><img src="assets/clip-bar.png" width="716" alt="The Momento clip bar: record dot, 12:34 buffered, and the lengths 15s 30s 1m 3m 5m 15m 30m 60m with 5m selected"></p>

It works with Steam games, emulators, browser games and anything else on your screen. It runs on desktops, laptops and handhelds like the ROG Ally, Legion Go and Steam Deck.

## Features

- **Always recording.** Keeps up to the last 60 minutes, including sound.
- **One key.** Super + G brings up the clip bar on top of your game.
- **Eight clip lengths.** 15 s, 30 s, 1 min, 3 min, 5 min, 15 min, 30 min and 60 min.
- **Fast saves.** Clips aren't re-encoded, so even a full hour is saved in a few seconds.
- **Light on performance.** Your graphics card does the video encoding on AMD, Intel and NVIDIA, so your frame rate barely changes.
- **Game audio included.** Microphone recording is optional.
- **Controller-friendly.** Bind it to a button through Steam Input, or trigger saves from the command line.
- **Works on KDE Plasma, GNOME, Hyprland, Sway and X11**, and can record Steam Gaming Mode.
- **Local only.** Nothing is uploaded, and you don't need an account.
- **No root needed.** Installs for your user only, which suits Bazzite, SteamOS and other immutable systems.

## How it works

1. **Momento records quietly in the background.** It keeps only the most recent hour and deletes older footage as it goes, so disk use stays fixed.
2. **Something happens that you want to keep.** Press **Super + G**.
3. **The clip bar slides in at the top of the screen** with the eight lengths in a row. Pick one.
4. **The last X minutes, up to right now, are saved** to `~/Videos/Momento/`.

```
  |<------------------- last 60 minutes, always kept ------------------->|
  |----------------------------------------------------------|-- 30 s ---|
                                                                         ^
                                                  you press Super + G here
                                                  -> Momento_..._30s.mp4
```

### How big is a clip?

At the default settings (1080p, High quality, 60 fps):

| Clip length | 15 s | 30 s | 1 min | 3 min | 5 min | 15 min | 30 min | 60 min |
|---|---|---|---|---|---|---|---|---|
| File size (approx.) | 28 MB | 57 MB | 114 MB | 340 MB | 570 MB | 1.7 GB | 3.4 GB | 6.8 GB |

Clips are ordinary MP4 files. They play in any video player and upload directly to Discord, YouTube and similar sites.

## Install

Installing has two steps. First install the system packages Momento uses, then run its installer. The installer itself only writes to your home folder.

**Step 2 is the same on every distro:**

```bash
git clone https://github.com/mehulchachada/momento.git && cd momento && ./install.sh --enable
```

`--enable` starts Momento now and at every login. Leave it off if you'd rather start it yourself.

### Bazzite, Bluefin, Aurora (Fedora Atomic)

Everything Momento needs ships with the system image, so no `rpm-ostree` layering is needed. Just run step 2.

> On plain Fedora Silverblue or Kinoite, the Fedora packages below would have to be layered with `rpm-ostree install`. That works, but it slows down system updates. A Flatpak is planned.

### Fedora Workstation / KDE

```bash
sudo dnf install python3-gobject python3-dbus python3-pyside6 pipewire-gstreamer gstreamer1-plugins-good gstreamer1-plugins-bad-free gstreamer1-plugin-libav ffmpeg-free layer-shell-qt
```

GPU video drivers:

- **AMD:** Fedora's own Mesa can't encode H.264. Enable [RPM Fusion](https://rpmfusion.org/Configuration), then run `sudo dnf swap mesa-va-drivers mesa-va-drivers-freeworld`.
- **Intel:** install `intel-media-driver` from RPM Fusion.
- **NVIDIA:** install the RPM Fusion driver and `gstreamer1-plugins-bad-freeworld` for NVENC.

### Arch Linux, CachyOS, EndeavourOS, Manjaro

```bash
sudo pacman -S --needed python-gobject python-dbus pyside6 gst-plugin-pipewire gst-plugins-good gst-plugins-bad gst-plugin-va gst-libav ffmpeg layer-shell-qt
```

GPU video drivers: `libva-mesa-driver` for AMD, `intel-media-driver` for Intel. On NVIDIA, the NVENC encoder comes with `gst-plugins-bad`.

### Ubuntu, Debian, Pop!_OS, Linux Mint

```bash
sudo apt install python3-gi gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 python3-dbus python3-pyside6.qtwidgets gstreamer1.0-pipewire gstreamer1.0-plugins-good gstreamer1.0-pulseaudio gstreamer1.0-plugins-bad gstreamer1.0-libav ffmpeg
```

You need Ubuntu 24.04+ or Debian 13+ for Python 3.11+ and PySide6.

- **GPU video drivers:** `mesa-va-drivers` for AMD, `intel-media-va-driver-non-free` for Intel.
- **KDE Plasma:** also install `layer-shell-qt` so the clip bar can appear over fullscreen games.

### openSUSE Tumbleweed

```bash
sudo zypper install python3-gobject typelib-1_0-Gst-1_0 python3-dbus-python python3-pyside6 gstreamer-plugin-pipewire gstreamer-plugins-good gstreamer-plugins-bad gstreamer-plugins-libav ffmpeg layer-shell-qt6
```

openSUSE's default repositories leave out H.264 encoding and AAC audio. Add [Packman](https://en.opensuse.org/Additional_package_repositories#Packman) and switch to its codec packages with `sudo zypper dup --from packman --allow-vendor-change`.

### SteamOS (Steam Deck)

SteamOS undoes system changes on every update. Until the Flatpak is ready, the most reliable route is an Arch [distrobox](https://distrobox.it/) with the Arch packages above.

### Checking the install

The installer ends with a checklist. For anything missing, it names the package to install on your distro. Run the check again at any time with:

```bash
./install.sh --check
```

## First launch

The first time Momento starts, your desktop asks what you want to share. It's the same dialog you see when sharing your screen on Discord or in a video call.

1. Choose **your monitor**, not a single window, so recording keeps working when games open new windows.
2. If there's a **Remember** or **Allow restoring** checkbox, tick it so the dialog doesn't come back after a restart.
3. Click **Share** or **OK**.

Your desktop may also ask you to confirm the **Super + G** shortcut. Accept it, or choose a different key there.

Momento is now recording. To check, run:

```bash
momento status
```

## Using it

| Do this | Result |
|---|---|
| **Super + G** | The clip bar slides in at the top of the screen |
| Click or tap a length | Saves that much, ending right now |
| **Left/Right** + **Enter**, or **1** to **8** | Picks a length with the keyboard |
| **Esc**, or Super + G again | Closes the bar without saving |

**From a terminal or a script:**

```bash
momento save 30s        # save the last 30 seconds without opening the bar
momento save 5m         # also 15s, 1m, 3m, 15m, 30m, 60m, or e.g. 90s, 2m
momento overlay         # open the clip bar
momento status          # recording state and how much is buffered
momento settings        # current resolution and quality
momento quit            # stop recording
```

The Momento entry in your app menu opens the clip bar. Right-click it to start the recorder or save a quick 30-second or 5-minute clip.

## Binding a controller button

You can turn a button, or a chord of two buttons, into the Momento shortcut with Steam Input:

1. In Steam, open the game's **Controller settings**. For apps outside Steam, use *Settings → Controller → Desktop Layout*.
2. Choose a button you don't use, or a chord such as **View + a back button**.
3. Map it to **Keyboard key → Super + G** to open the clip bar. For instant saves without the bar, map it instead to a key you've bound to `momento save 30s` in your desktop's shortcut settings.

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

Momento always records at 60 fps. You choose the **resolution** and the **quality**. Together they decide how sharp clips look and how much disk space the 60-minute history takes:

| Resolution | Standard | High (default) | Ultra |
|---|---|---|---|
| 720p | 2.7 GB | 4.5 GB | 6.8 GB |
| **1080p** (default) | 4.5 GB | **6.8 GB** | 11 GB |
| 1440p | 7.2 GB | 11 GB | 18 GB |
| 2160p (4K) | 13.5 GB | 20 GB | 32 GB |
| Native (your screen's size) | 7.2 GB | 11 GB | 18 GB |

These are the approximate totals for a full hour of history. The buffer never grows past them, because old footage is deleted as new footage comes in.

**Recommendations:** on a handheld or a 1080p monitor, use 1080p High (the default). On a 1440p or 4K monitor, use 1440p High.

Change them from a terminal. Momento restarts recording by itself to apply the change:

```bash
momento set resolution 1440p     # 720p, 1080p, 1440p, 2160p, native
momento set quality ultra        # standard, high, ultra
momento settings                 # show the current values
```

If your screen's shape doesn't match the resolution you picked (for example a 16:10 handheld recording at 1080p), the video gets black bars. It is never stretched.

## Settings

All settings are stored in `~/.config/momento/config.toml`, a plain text file with a comment next to each option. If you edit the file by hand, restart Momento afterwards:

```bash
systemctl --user restart momento.service
```

The settings most people change:

| Setting | Key | Default | Notes |
|---|---|---|---|
| Resolution | `[capture] resolution` | `"1080p"` | `720p`, `1080p`, `1440p`, `2160p` or `native` (your screen's own size). See [Video quality](#video-quality) |
| Quality | `[capture] quality` | `"high"` | `standard`, `high` or `ultra` |
| Hotkey | `[hotkey] trigger` | `"LOGO+g"` (Super + G) | For example `"CTRL+ALT+r"` or `"F9"`. Your desktop's shortcut settings can also change it |
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

Momento records whatever your default output device is playing. If you switched headsets or speakers after it started, restart it with `systemctl --user restart momento.service`. Also check that `[audio] desktop = true` in the settings, and that `./install.sh --check` finds an AAC encoder.
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

At the default settings (1080p High), the buffer takes about 6.8 GB and never grows past that. While it runs, Momento writes about 6.7 GB per hour. A typical 1 TB SSD is rated for several hundred terabytes of writes, so even a few hours of gaming a day uses a small part of its rated life.

To write less, use a lower resolution or Standard quality, or lower `max_seconds`. You can also move the buffer to another drive with `[buffer] dir`.
</details>

<details>
<summary><b>Does it slow my games down?</b></summary>

With hardware encoding (AMD, Intel or NVIDIA), the cost is usually a few percent at most. If the installer reports that only a *software* encoder was found, you will notice it, especially on a handheld. Install your GPU's video driver from the install section above.
</details>

<details>
<summary><b>Why does my clip start a second earlier than I picked?</b></summary>

Clips are cut without re-encoding, which is what makes saving fast. The trade-off is that a clip has to start at the nearest keyframe, so clip lengths are accurate to about one second.
</details>

<details>
<summary><b>Super + G does nothing</b></summary>

Your desktop may not support shortcuts registered by apps. That needs a recent xdg-desktop-portal: KDE Plasma 6, GNOME 48+ or Hyprland. You can add the shortcut yourself instead: in your desktop's keyboard settings, create a custom shortcut that runs `momento overlay`.
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

From the folder you cloned:

```bash
./install.sh --uninstall     # removes Momento, keeps settings and clips
./install.sh --purge         # also deletes settings and the replay buffer
```

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
