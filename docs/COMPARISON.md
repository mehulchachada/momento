# How Momento compares

Momento next to the instant-replay tools a Linux gamer is most likely to have already: Steam Game Recording, OBS Studio's Replay Buffer and GPU Screen Recorder.

Only Momento's numbers were measured by us (see [Performance](../README.md#performance)). Everything about the other tools comes from their own docs, source code or public reports, listed under [Sources](#sources). "Not documented" means we couldn't find it. Checked September 2026.

| | **Momento** | **Steam Game Recording** | **OBS Replay Buffer** | **GPU Screen Recorder** |
|---|---|---|---|---|
| What it records | Any game or app: just the game window, or the full screen | Only games launched through Steam [S1] | Whatever is in your OBS scene | Screen, window or region [G1] |
| Saving a clip | One press, pick 15 s to 60 min | Trim on a timeline, then export to MP4 [S1][S6] | Always saves the whole buffer [O4] | Full buffer, last 1 min or last 10 min; any length from the terminal [G2][G3] |
| MP4 ready | In seconds, no re-encoding | After a separate export, which can re-encode [S1][S6] | Fast | Fast |
| Longest replay | 60 min | 120 min by default, adjustable [S1] | Up to 6 h, limited by RAM [O2] | Up to 24 h [G2] |
| Where the replay is kept | On disk | On disk [S1] | In RAM [O1] | In RAM, or on disk if you choose [G1] |
| Memory for an hour at 1080p | About 150 MB | Kept on disk | About 6.8 GB (see below) | About 6.8 GB in RAM mode |
| Replay kept after a restart | Yes | Not documented | No (RAM) | No in RAM mode |
| Keyboard shortcut on Wayland | Yes, no plugins | Steam's shortcuts | Needs a plugin [O6] | Yes; reads input devices directly, can clash with key remappers [G3] |
| Controller | Hold View + Menu, then the D-pad | Steam button combos [S6] | Through Steam Input, once hotkeys work | Built-in button combos [G4] |
| In-game overlay | Slim bar | Steam overlay with a timeline [S1] | None (the OBS window) | Fullscreen overlay [G3] |
| Steam Gaming Mode | Records; the bar is coming | Built in [S6] | Not designed for it | Not verified |
| Formats | H.264 MP4 | MP4 | H.264, HEVC, AV1 | H.264, HEVC, AV1 and more [G1] |
| Audio | Game sound plus optional mic, one track | Game, other apps and mic [S1][S6] | Up to 6 tracks | Several tracks, per-app audio [G1] |
| Needs root | No | No | No | Only for some screen capture modes [G1] |
| Maturity | Alpha | Mature | Very mature | Mature |

**Why 6.8 GB?** An hour at 15 Mbps is about 6.8 GB of video. Tools that keep the replay in RAM need that much free memory. On a 16 GB handheld, where Linux sees about 9.4 GB, that leaves little for the game.

## Where others are ahead

- **Steam Gaming Mode:** Steam's recorder works there today. Momento records there, but its bar can't appear yet. Bind a button to `momento save 30s` through Steam Input.
- **Smaller files:** OBS, Steam and GPU Screen Recorder offer HEVC or AV1. Momento uses H.264, which plays everywhere. HEVC and AV1 are planned.
- **Separate audio tracks:** OBS and GPU Screen Recorder can put the mic on its own track. Planned for Momento.
- **Longer replays:** Steam keeps 120 minutes by default; Momento keeps up to 60.

## Which should you use?

- **You play outside Steam** (cloud gaming, emulators, other launchers): Momento. It records all of it the same way.
- **You want "save what just happened" in one press:** Momento. Pick the length, get an MP4 a few seconds later.
- **Your handheld or laptop is short on RAM:** Momento or Steam, which keep the replay on disk.
- **You only play Steam games in Gaming Mode:** Steam Game Recording, for now.
- **You already stream with OBS:** its replay buffer is easy to turn on for short clips. Momento can run next to it.
- **You want every codec and option:** GPU Screen Recorder.

## Sources

- [S1] Valve, Steam Game Recording FAQ: https://help.steampowered.com/en/faqs/view/23B7-49AD-4A28-9590
- [S6] Steam Deck HQ, Game Recording on the Steam Deck: https://steamdeckhq.com/tips-and-guides/how-to-use-game-recording-on-the-steam-deck-all-features-explained/
- [O1] OBS source, replay buffer kept in memory: https://github.com/obsproject/obs-studio/blob/master/plugins/obs-ffmpeg/obs-ffmpeg-mux.c
- [O2] OBS source, replay buffer limits (up to 21,600 s, memory capped at 75% of RAM): https://github.com/obsproject/obs-studio/tree/master/frontend
- [O4] OBS "Save Replay" writes the whole buffer (same file as [O1])
- [O6] OBS global shortcuts on Wayland: https://github.com/obsproject/obs-studio/issues/10538
- [G1] GPU Screen Recorder: https://git.dec05eba.com/gpu-screen-recorder/about/
- [G2] GPU Screen Recorder options: https://git.dec05eba.com/gpu-screen-recorder/tree/src/args_parser.c
- [G3] GPU Screen Recorder UI: https://git.dec05eba.com/gpu-screen-recorder-ui/about/
- [G4] GPU Screen Recorder UI source: https://git.dec05eba.com/gpu-screen-recorder-ui/tree/src
