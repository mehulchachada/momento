# How Momento compares

This page compares Momento with the three instant-replay options a Linux gamer is most likely to already have or be pointed to:

1. **OBS Studio's Replay Buffer**
2. **Steam Game Recording** (built into the Steam client since November 2024, including Steam Deck and Linux)
3. **GPU Screen Recorder** (`gpu-screen-recorder` + `gsr-ui`), the closest Linux-native competitor

It sticks to facts. Where another tool does something Momento doesn't yet, this page says so. Figures for the other products come from their own documentation, source code or third-party reports, and are labelled **reported**. Only Momento's figures were measured by us (see [Methodology](#methodology)). Nothing here is a head-to-head benchmark: we did not install or run OBS, Steam recording or GPU Screen Recorder for this comparison.

Status as checked on 2026-09-26: OBS Studio 32.2.2 (Flathub), GPU Screen Recorder 6.1.2 (Flathub), the current stable Steam client. On the test machine (ROG Ally, Bazzite, KDE Plasma 6 Wayland) neither OBS Studio nor GPU Screen Recorder was installed. Bazzite does ship the `obs-vkcapture` layer and the OBS Flatpak VkCapture/GStreamer plugins, but not OBS itself.

## Feature and performance matrix

| | **Momento** | **OBS Replay Buffer** | **Steam Game Recording** | **GPU Screen Recorder** |
|---|---|---|---|---|
| What it records | The whole screen (monitor you pick in the portal), or gamescope's output in Gaming Mode | Whatever the OBS scene contains: screen (PipeWire portal), window, or game via `obs-vkcapture` | Only games launched through Steam, via the Steam overlay. "It will not capture video of your desktop or other programs" [S1] | Monitor (KMS), window, region, or portal [G1] |
| Capture path on Wayland | xdg-desktop-portal ScreenCast -> PipeWire -> GStreamer `pipewiresrc`; gamescope PipeWire node in Gaming Mode | PipeWire portal ("Screen Capture (PipeWire)"), unified PipeWire source since 30.2 [O3] | Steam overlay hook in the game process; gamescope in Gaming Mode | KMS via a small privileged helper (`gsr-kms-server`, needs root / `cap_sys_admin`), or portal without root [G1] |
| Encoder | H.264 on the GPU: VA-API (`vah264enc`, AMD/Intel), NVENC, QSV; x264 only as a fallback | H.264/HEVC/AV1 via VA-API, NVENC, QSV, or x264. Zero-copy (shared texture) VA-API encode since 30.2 [O3] | GPU encode on AMD and NVIDIA; CPU otherwise [S1]. Codec and zero-copy details for Linux are not documented; press reports mention HEVC support arriving in a later beta [S4] | H.264 (default), HEVC, AV1, VP8/9 via VA-API or NVENC; zero-copy [G1] |
| Zero-copy | Yes when PipeWire hands over DMA-BUFs: `vapostproc` does colour conversion and scaling on the GPU, frames stay in VA memory. Falls back to `videoconvert` (CPU) if that path fails to negotiate | Yes (30.2+) [O3] | Not documented | Yes, its core design goal [G1] |
| Where the buffer lives | **Disk**: 10 s MPEG-TS segments in `~/.cache/momento/buffer` | **RAM** (encoded packets in memory) [O1] | **Disk**, in Steam's raw DASH (`.m4s`) format [S1][S3] | **RAM** by default, disk with `-replay-storage disk` [G1] |
| Max buffer length | 60 min (hard cap) | 21,600 s (6 h) in the UI, also capped by "Maximum Memory" (20-8192 MB default range, up to 75 % of system RAM) [O1][O2] | 120 min by default and configurable, plus a disk-space cap [S1][S3] | 2 s to 86,400 s (24 h) [G2] |
| RAM for a 60-min buffer at 15 Mbps | about 150 MB (buffer is on disk) | about **6.8 GB** of RAM for the buffer alone (see [RAM maths](#ram-for-a-long-buffer)) | Not RAM-bound (disk) | about 6.8 GB in RAM mode; small in disk mode |
| Disk for a 60-min buffer | About 6.8 GB at 1080p High (15 Mbps), fixed ceiling | None while buffering | Bounded by the disk-space cap you set; oldest footage is overwritten [S1] | None in RAM mode; about 6.8 GB in disk mode |
| Choosing clip length at save time | **Yes**: clip bar with 15 s / 30 s / 1 / 3 / 5 / 15 / 30 / 60 min, or `momento save <any length>` | **No**: "Save Replay" writes the whole buffer; one length per configuration [O4] | Yes, but by editing: drop a clip on the timeline and drag its ends; default clip is short [S1][S6] | Partly: gsr-ui has hotkeys for full buffer, last 1 min and last 10 min; `gsr-cli save-replay <seconds>` for any length [G2][G3] |
| Save latency | About 3 s for a 1-minute clip (measured); longer clips are a straight copy, limited by disk speed. No re-encode, no export step | Fast: remux from RAM in a background thread; no published figure | Two steps: "Save in Steam" is quick; an MP4 needs a separate Export, which can re-encode (Original / File size / Custom) [S1][S6] | Fast from RAM; no published figure |
| Trim accuracy | Start snapped to the nearest keyframe (1 s GOP), so about +-0.5 s; end is "now" | Buffer is trimmed in whole GOPs, so clips can run over the set time; reported cases of much longer files [O5] | Set by hand on a visual timeline; precision not documented | Keyframe-aligned; default keyframe interval 2 s [G2] |
| Overhead | CPU about 4.5% of one core (0.3% of the 8-core Z1 Extreme), one test machine: ROG Ally, light desktop workload, package power about +1.5 W (±1 W) (measured, ROG Ally) | No official figure. The GPU Screen Recorder author reports large FPS drops with OBS + NVENC at 4K (30 -> 7 fps, 60 -> 23 fps) on NVIDIA [G1]; treat as a competitor's claim | Reported 38 -> 35 fps in Dragon Age: The Veilguard on Steam Deck [S7] | Reported: 30 -> 30 fps and 60 -> 58 fps in the same 4K NVIDIA tests; "no fps drop at all" at 4K60 AV1 on an RX 7800 XT [G1] |
| Audio | Default output (follows device switches) plus optional mic, mixed into one AAC track | Multiple tracks (up to 6), per-source mixing; per-app capture on Linux needs a third-party PipeWire plugin | Game audio, optionally other programs' audio and mic; stereo/mono, auto levels [S1][S6] | Opus (default) or AAC; `-a` can be given several times; per-application audio on PipeWire (`--list-application-audio`) [G1][G2] |
| Gaming Mode (gamescope) | Records (gamescope PipeWire node). **Clip bar does not appear**: bind a button to `momento save 30s` | Not designed for it; normally used in Desktop Mode | **Native**: Steam button combos, overlay timeline [S6] | gsr-ui marks itself as a gamescope external overlay (source code [G4]); not verified here |
| Overlay / UX | Slim bottom bar over fullscreen games on KDE and wlroots (layer-shell); frameless window elsewhere | OBS main window; no in-game overlay | Full Steam overlay with timeline, markers (Ctrl+F12), per-game settings, clip sharing [S1] | ShadowPlay-style fullscreen overlay (Alt+Z). X11 app; "primarily designed for X11", runs on Wayland with caveats [G3] |
| Hotkey on KDE Wayland | Super+Shift+G via the GlobalShortcuts portal | **Hotkeys only fire while OBS is focused** on Wayland; needs a plugin (e.g. `obs-wayland-hotkeys`). Native support is an open PR [O6][O7] | Steam's own shortcuts (overlay-level) | Grabs input devices directly and creates a virtual keyboard; can conflict with remappers like keyd [G3] |
| Controller | Steam Input mapping to the key combo, or to `momento save 30s` | Steam Input to a key, once hotkeys work | Built in: Steam+A record, Steam+Y marker, Steam+D-pad Up clip [S6] | Built-in joystick hotkeys (PlayStation/Home button combos) [G4] |
| Install footprint | About 0.5 MB of Python; uses GStreamer, PipeWire, FFmpeg and PySide6 already in the Bazzite image | Flatpak: about 504 MB installed plus the freedesktop runtime (Flathub metadata) | Part of Steam | Flatpak: about 19 MB installed plus the freedesktop runtime (Flathub metadata). System-wide install needed for monitor capture on AMD/Intel [G1] |
| Root needed | No | No | No | Only for KMS monitor capture (setcap helper); portal mode needs none [G1] |
| Codecs out | H.264 + AAC in MP4 | Anything OBS supports | MP4 on export | H.264/HEVC/AV1/VP8/VP9, MP4/MKV/others [G1] |
| Maturity | Alpha, one small project | Very mature, huge user base | Mature, maintained by Valve; open Linux bugs (below) | Mature, very active, Linux-only |

### RAM for a long buffer

OBS and GPU Screen Recorder (default mode) keep the encoded buffer in RAM. For 60 minutes at 15 Mbps video plus 160 kbps audio:

```
(15,000 + 160) kbit/s x 3,600 s / 8 = 6,822,000 kB  ~ 6.8 GB (6.35 GiB)
```

OBS's own settings page uses the same formula for its "estimated memory usage" and caps "Maximum Memory" at 75 % of installed RAM [O2]. On the ROG Ally used for testing, Linux sees about 9.4 GiB (the rest is reserved as GPU memory), so the cap is about 7.2 GB. A 60-minute 1080p buffer technically fits, but leaves under 3 GB for the OS, the browser and the game, which is not practical on a handheld. At 15 Mbps a realistic RAM buffer on this machine is a few minutes, not an hour.

GPU Screen Recorder avoids this with `-replay-storage disk`. Momento and Steam always buffer on disk, which costs SSD writes instead of RAM: about 6.7 GB per hour of play at Momento's default setting. That is a small fraction of a modern SSD's rated endurance, but it is not zero.

## Why gamers pick Momento

- **Every game, not just Steam games.** Momento records whatever is on your screen: Steam, Heroic, Lutris, GOG and Epic games, emulators, browser games, and cloud gaming like **GeForce NOW** and Xbox Cloud Gaming. Steam Game Recording only captures games running through Steam with its overlay, "not your desktop or other programs" [S1], and has open bugs with non-Steam games on SteamOS [S8].
- **Save the moment after it happens, in one press.** Hit the shortcut, pick 15 s, 30 s, 1, 3, 5, 15, 30 or 60 min, done. No timeline to scrub and no clip handles to drag (Steam), no fixed length decided in advance (OBS), no choice limited to "full buffer / 1 min / 10 min" (GPU Screen Recorder's UI).
- **A ready-to-share MP4 in seconds.** Clips are copied straight out of the buffer without re-encoding: a 1-minute clip took about 3 s on our test handheld, and lands in `~/Videos/Momento` ready for Discord or YouTube. Steam needs a separate Export step to get an MP4, which can re-encode [S1][S6].
- **Your game keeps the RAM.** The hour of history lives on disk, so Momento uses about 150 MB of memory. An OBS RAM buffer of the same hour would need about 6.8 GB, more than two thirds of what Linux can use on a ROG Ally (see [RAM maths](#ram-for-a-long-buffer)).
- **The game always comes first.** Momento's service is capped at 1.5 GB, gives memory back to the game under pressure, and is the process Linux stops first if RAM runs out. It checks free disk space before it records and stops before your drive fills up.
- **Your history survives.** Pausing, changing settings, restarting or rebooting keeps the replay. Clip lengths count recorded footage, so "last 5 minutes" still works right after a pause.
- **Real 60 or 120 fps.** Motion is captured at the full frame rate (54-58 unique frames per second measured on a 60 fps animation), with a 120 fps option for high-refresh screens and fast shooters.
- **Sound follows you.** Switch from speakers to a headset or HDMI mid-session and the recording follows.
- **Made for Linux handhelds.** Native Wayland, no root, no helper with special permissions, no keyboard grabbing. On Bazzite everything it needs is already in the system image. Bind a back paddle or button chord through Steam Input and it's controller-driven.
- **Light.** About 4.5% of one CPU core and about 1.5 W on our test handheld, with encoding on the GPU's separate video engine.
- **Yours.** Open source, local only, no account, nothing uploaded.

## What's coming next

Momento is young (alpha), and a few things are still on the way:

- **Clip bar inside Steam Gaming Mode.** Momento already records in Gaming Mode; until the bar arrives there, bind a controller button to `momento save 30s` through Steam Input. Steam's own recorder is built into that mode today, with Steam+button shortcuts and a timeline [S6].
- **Smaller files with HEVC and AV1.** Momento uses H.264 today, which every device and site plays. OBS, Steam and GPU Screen Recorder offer HEVC or newer codecs.
- **Separate mic track** for people who edit their clips. Today game sound and mic are mixed into one track.
- **Longer history.** Momento keeps up to 60 minutes; Steam defaults to 120 [S1].
- **Preview before saving.** A quick look at the moment before you keep it.

## Which should you use?

- **You play outside Steam: GeForce NOW or other cloud gaming, emulators, other launchers, or a mix:** **Momento.** It records all of it the same way, and Steam's recorder can't.
- **You want PS5-style "save what just happened" on a Linux PC or handheld:** **Momento.** One shortcut, eight lengths, an MP4 a few seconds later.
- **Your handheld or laptop is short on RAM:** **Momento** or Steam, which both keep the history on disk. Avoid long RAM-based buffers.
- **You only play Steam games in Gaming Mode:** Steam Game Recording works there today. Momento records there too, and its clip bar for Gaming Mode is on the way.
- **You already stream with OBS:** its replay buffer is essentially free to turn on for short clips. Momento can run next to it for long, pick-the-length saves.
- **You want every codec and advanced option:** GPU Screen Recorder has the most knobs, at the cost of more setup.

## Methodology

Momento's numbers were measured on the machine that wrote this page: ASUS ROG Ally (Ryzen Z1 Extreme, Radeon 780M, 16 GB LPDDR5 with about 9.4 GiB visible to Linux), Bazzite `44.20260825`, KDE Plasma 6 Wayland, recording through the ScreenCast portal with `vah264enc` at the default 1080p / High (15 Mbps) / 60 fps.

- **CPU:** about 4.5% of one core (0.3% of the 8-core Z1 Extreme), one test machine: ROG Ally, light desktop workload. CPU time of `momento.service` (its systemd user cgroup) over 100-180 s windows split into 10 s buckets, repeated runs, as a percentage of one core. No CPU colour conversion; the largest single cost is the encoder/videorate thread at about 1.3-1.7%.
- **Power:** about +1.5 W (±1 W). Difference in package power with the service recording versus stopped, same workload. From the amdgpu package-power sensor, which includes other activity; treat it as an estimate. Battery drain was not measured.
- **RAM:** about 150 MB. `MemoryCurrent` / RSS of the service with a full buffer.
- **Disk:** about 7 GB per hour, from the growth of `~/.cache/momento/buffer`.
- **Save time:** about 3 s (measured for a 1-minute clip) (30 s) and not yet measured (a 7 GB stream copy, limited by disk speed) (60 min), wall time of `momento save`, including the flush that closes the current segment.
- **Workload:** KDE Plasma 6 desktop, light use, on AC power, performance power profile, two 1920x1080 120 Hz displays, 1080p High 60 fps via the screen-cast portal and vah264enc.

Figures for OBS, Steam and GPU Screen Recorder were **not** measured by us. They are quoted from the sources below, and several of them come from a competitor or a single user report. Treat them as indications, not benchmarks. A fair comparison would run all four on the same machine, game and settings, and nobody (us included) has published one yet.

## Sources

- [S1] Valve, *Steam Game Recording* support FAQ: https://help.steampowered.com/en/faqs/view/23B7-49AD-4A28-9590
- [S2] Valve, Steam Game Recording overview page (not cited above; for reference): https://store.steampowered.com/gamerecording
- [S3] Shacknews, *How to use Steam Game Recording*: https://www.shacknews.com/article/142140/how-to-use-steam-game-recording
- [S4] PC Gamer, *Steam Game Recording is now available for everyone*: https://www.pcgamer.com/software/platforms/steam-game-recording-is-now-available-for-everyone-and-its-packed-with-neat-features/ ; background on Steam's VA-API encoding on Linux: https://9to5linux.com/steam-client-now-supports-va-api-hardware-encoding-on-linux-ceg-drm-games
- [S6] Steam Deck HQ, *How To Use Game Recording On The Steam Deck*: https://steamdeckhq.com/tips-and-guides/how-to-use-game-recording-on-the-steam-deck-all-features-explained/
- [S7] Steam Deck HQ, *Game Recording Comes Out of Beta*: https://steamdeckhq.com/news/game-recording-comes-in-new-steam-deck-update/
- [S8] steam-for-linux #11031, background recordings not saving video for non-Steam games: https://github.com/ValveSoftware/steam-for-linux/issues/11031
- [S9] Bazzite #3206, game recording freezes in Gaming Mode: https://github.com/ublue-os/bazzite/issues/3206
- [O1] OBS source, `plugins/obs-ffmpeg/obs-ffmpeg-mux.c` (replay buffer kept as in-memory packets, trimmed by `max_time_sec` / `max_size_mb`): https://github.com/obsproject/obs-studio/blob/master/plugins/obs-ffmpeg/obs-ffmpeg-mux.c
- [O2] OBS source, `frontend/forms/OBSBasicSettings.ui` (5-21600 s, 20-8192 MB) and `frontend/settings/OBSBasicSettings.cpp` (memory cap at 75 % of RAM): https://github.com/obsproject/obs-studio/tree/master/frontend
- [O3] OBS Studio 30.2 release notes (unified PipeWire source, shared-texture VA-API/NVENC/QSV encoding on Linux): https://obsproject.com/blog/obs-studio-30-2-release-notes
- [O4] OBS replay buffer saves the whole buffer (`replay_buffer_save` in [O1])
- [O5] obs-studio #9933, replay buffer not respecting maximum time: https://github.com/obsproject/obs-studio/issues/9933
- [O6] obs-studio #10538, global shortcuts do not work on Wayland: https://github.com/obsproject/obs-studio/issues/10538 ; plugin: https://github.com/leia-uwu/obs-wayland-hotkeys
- [O7] obs-studio PR #13661, Wayland global hotkeys (open as of September 2026): https://github.com/obsproject/obs-studio/pull/13661
- [G1] GPU Screen Recorder README: https://git.dec05eba.com/gpu-screen-recorder/about/
- [G2] GPU Screen Recorder source, `src/args_parser.c` (`-r` 2-86400 s, `-replay-storage ram|disk`, `-keyint` default 2.0 s): https://git.dec05eba.com/gpu-screen-recorder/tree/src/args_parser.c
- [G3] GPU Screen Recorder UI README: https://git.dec05eba.com/gpu-screen-recorder-ui/about/
- [G4] GPU Screen Recorder UI source, `src/Config.cpp` (save 1 min / 10 min hotkeys), `src/Overlay.cpp` (`GAMESCOPE_EXTERNAL_OVERLAY`), `src/GlobalHotkeys/GlobalHotkeysJoystick.cpp`: https://git.dec05eba.com/gpu-screen-recorder-ui/tree/src
- Flathub metadata for install sizes: https://flathub.org/apps/com.obsproject.Studio , https://flathub.org/apps/com.dec05eba.gpu_screen_recorder
