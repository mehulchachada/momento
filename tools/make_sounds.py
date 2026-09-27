#!/usr/bin/env python3
"""Synthesize the clip bar's UI sounds (momento/sounds/*.wav).

    python3 tools/make_sounds.py                  # rewrite momento/sounds/
    python3 tools/make_sounds.py --out DIR        # somewhere else
    python3 tools/make_sounds.py --reel reel.wav  # also: every sound in a row, to listen to
    python3 tools/make_sounds.py --preview /tmp/sfx   # the record/pause/stop candidates, to compare
    python3 tools/make_sounds.py --use record=A --use pause=A --use stop=A   # make them from candidates

Everything is generated here, from sine partials and seeded noise: no samples,
nothing recorded. Pure Python (wave + array), deterministic, so running it again
gives the same files. One timbre family: a sine with a little 2nd and 3rd
harmonic (so low notes still speak on a handheld's small speakers), a fast
raised-cosine attack and an exponential decay; the open / close "air" is
band-passed noise swept up or down with a faint tone under it.

Format: 48 kHz, 16-bit, mono, 40-400 ms each. Peaks are normalised to
PEAK_DBFS (-18 dBFS); ``move``, heard most often, to MOVE_DBFS (-26 dBFS).
"""

from __future__ import annotations

import argparse
import array
import math
import sys
import wave
from pathlib import Path

RATE = 48_000
PEAK_DBFS = -18.0
MOVE_DBFS = -26.0
REEL_GAP_S = 0.45
OUT = Path(__file__).resolve().parent.parent / "momento" / "sounds"

# The shared timbre: (harmonic, relative level).
SOFT = ((1, 1.0), (2, 0.16), (3, 0.05))
WARM = ((1, 1.0), (2, 0.32), (3, 0.12))    # low notes: more harmonics to be heard on small speakers


def _n(seconds: float) -> int:
    return int(round(seconds * RATE))


def _silence(seconds: float) -> list[float]:
    return [0.0] * _n(seconds)


def _mix(dst: list[float], src: list[float], at: float = 0.0, gain: float = 1.0) -> list[float]:
    """Add ``src`` into ``dst`` starting at ``at`` seconds (``dst`` grows as needed)."""
    start = _n(at)
    end = start + len(src)
    if end > len(dst):
        dst.extend([0.0] * (end - len(dst)))
    for i, v in enumerate(src):
        dst[start + i] += v * gain
    return dst


def _fade_out(buf: list[float], seconds: float = 0.008) -> list[float]:
    """A short raised-cosine tail so every sound ends on exact silence (no click)."""
    n = min(len(buf), _n(seconds))
    for i in range(n):
        buf[len(buf) - n + i] *= 0.5 * (1 + math.cos(math.pi * (i + 1) / n))
    return buf


def tone(f0: float, f1: float, dur: float, attack: float = 0.004, tau: float = 0.05,
         partials=SOFT) -> list[float]:
    """A note gliding exponentially from ``f0`` to ``f1`` Hz; raised-cosine attack,
    exponential decay with time constant ``tau`` seconds."""
    n = _n(dur)
    na = max(1, _n(attack))
    out = [0.0] * n
    phase = 0.0
    ratio = f1 / f0
    for i in range(n):
        t = i / RATE
        f = f0 * ratio ** (i / n)
        phase += 2 * math.pi * f / RATE
        if f * partials[-1][0] < RATE / 2:
            s = sum(a * math.sin(h * phase) for h, a in partials)
        else:
            s = math.sin(phase)
        env = 0.5 * (1 - math.cos(math.pi * i / na)) if i < na else 1.0
        env *= math.exp(-max(0.0, t - attack) / tau)
        out[i] = s * env
    return _fade_out(out)


def _noise(n: int, seed: int) -> list[float]:
    """White noise in [-1, 1) from a small LCG: the same every run, on every machine."""
    x = seed & 0xFFFFFFFF
    out = [0.0] * n
    for i in range(n):
        x = (1664525 * x + 1013904223) & 0xFFFFFFFF
        out[i] = x / 2147483648.0 - 1.0
    return out


def air(f0: float, f1: float, dur: float, peak_at: float, body: tuple[float, float],
        q: float = 1.4, seed: int = 1, tone_gain: float = 0.22) -> list[float]:
    """A soft whoosh: noise through a band-pass whose centre sweeps ``f0`` -> ``f1`` Hz,
    swelling to its loudest at ``peak_at`` (0..1 of the length), with a faint tone
    gliding ``body`` = (from, to) Hz under it."""
    n = _n(dur)
    src = _noise(n, seed)
    out = [0.0] * n
    low = band = 0.0
    damp = 1.0 / q
    ratio = f1 / f0
    for i in range(n):
        x = i / n
        fc = f0 * ratio ** x
        g = 2 * math.sin(math.pi * fc / RATE)       # Chamberlin state-variable filter
        high = src[i] - low - damp * band
        band += g * high
        low += g * band
        # a swell: sine-shaped rise to peak_at, then down to silence
        if x < peak_at:
            env = math.sin(0.5 * math.pi * x / peak_at) ** 2
        else:
            env = math.cos(0.5 * math.pi * (x - peak_at) / (1 - peak_at)) ** 2
        out[i] = band * env
    peak = max(abs(v) for v in out) or 1.0
    out = [v / peak for v in out]
    under = tone(body[0], body[1], dur, attack=dur * peak_at, tau=dur, partials=SOFT)
    return _fade_out(_mix(out, under, gain=tone_gain))


def click(dur: float, seed: int, fc: float = 4200.0, tau: float = 0.003) -> list[float]:
    """A tiny band-passed noise tick (the shutter's blades)."""
    n = _n(dur)
    src = _noise(n, seed)
    out = [0.0] * n
    low = band = 0.0
    g = 2 * math.sin(math.pi * fc / RATE)
    na = _n(0.0008)                                  # not a hard edge: a 0.8 ms rise
    for i in range(n):
        high = src[i] - low - 0.8 * band
        band += g * high
        low += g * band
        env = 0.5 * (1 - math.cos(math.pi * i / na)) if i < na else 1.0
        out[i] = band * env * math.exp(-i / RATE / tau)
    return _fade_out(out, 0.002)


def _pitched(buf: list[float], factor: float) -> list[float]:
    """Resample by ``factor`` (> 1: higher and shorter), linear interpolation."""
    n = int(len(buf) / factor)
    out = [0.0] * n
    for i in range(n):
        p = i * factor
        j = int(p)
        frac = p - j
        a = buf[j]
        b = buf[j + 1] if j + 1 < len(buf) else 0.0
        out[i] = a + (b - a) * frac
    return _fade_out(out)


# ---------------------------------------------------------------------------
# the sounds

def s_move():
    # focus moving: the softest thing here, a tiny high tick
    return tone(1568.0, 1568.0, 0.040, attack=0.0015, tau=0.007, partials=((1, 1.0), (2, 0.08)))


def s_select():
    # a value chosen: a soft click with a little body an octave down
    buf = tone(1046.5, 1046.5, 0.070, attack=0.0015, tau=0.014)
    return _mix(buf, tone(523.25, 523.25, 0.070, attack=0.002, tau=0.018), gain=0.55)


def s_open():
    return air(520.0, 2600.0, 0.240, peak_at=0.62, body=(523.25, 783.99), seed=11)


def s_close():
    return air(2600.0, 520.0, 0.220, peak_at=0.30, body=(783.99, 523.25), seed=23)


def s_gallery_open():
    # the bar's open, a fifth higher and a little quicker
    return _pitched(air(520.0, 2600.0, 0.270, peak_at=0.62, body=(523.25, 783.99), seed=37), 1.5)


def s_gallery_close():
    return _pitched(air(2600.0, 520.0, 0.250, peak_at=0.30, body=(783.99, 523.25), seed=41), 1.5)


def s_save():
    # a pleasant two-note confirm, up a fifth: G5 then D6
    buf = tone(783.99, 783.99, 0.170, attack=0.003, tau=0.055)
    return _mix(buf, tone(1174.66, 1174.66, 0.270, attack=0.003, tau=0.075), at=0.085)


def s_shot():
    # a light shutter: two quick blade ticks over a very short high ping
    buf = click(0.040, seed=5)
    _mix(buf, click(0.045, seed=9, fc=3300.0, tau=0.004), at=0.060, gain=0.8)
    return _mix(buf, tone(2093.0, 2093.0, 0.050, attack=0.001, tau=0.008), gain=0.35)


def s_record():
    # play / resume / start: rising C5 -> G5
    return tone(523.25, 783.99, 0.230, attack=0.006, tau=0.090)


def s_pause():
    # falling G5 -> C5
    return tone(783.99, 523.25, 0.230, attack=0.006, tau=0.090)


def s_stop():
    # low and soft: G4 easing down to E4
    return tone(392.0, 329.63, 0.320, attack=0.008, tau=0.110, partials=WARM)


def s_error():
    # refused: two low soft blips on E4
    buf = tone(329.63, 329.63, 0.080, attack=0.004, tau=0.030, partials=WARM)
    return _mix(buf, tone(329.63, 329.63, 0.090, attack=0.004, tau=0.030, partials=WARM), at=0.115)


def s_delete():
    # a soft confirm after a delete: two notes down, E5 then B4
    buf = tone(659.25, 659.25, 0.120, attack=0.003, tau=0.040)
    return _mix(buf, tone(493.88, 493.88, 0.190, attack=0.003, tau=0.060), at=0.070)


SOUNDS = {
    "move": s_move, "select": s_select, "open": s_open, "close": s_close,
    "gallery_open": s_gallery_open, "gallery_close": s_gallery_close,
    "save": s_save, "shot": s_shot, "record": s_record, "pause": s_pause,
    "stop": s_stop, "error": s_error, "delete": s_delete,
}
LEVELS = {"move": MOVE_DBFS}


# ---------------------------------------------------------------------------
# candidate record / pause / stop sounds (not the defaults yet: pick with --use)
#
# Same family as above: sine partials, raised-cosine attack, exponential decay.
# Each partial here has its own decay (a multiple of the note's tau), so the upper
# ones die first and the note rounds off, the way a soft mallet or a felt hammer does.
# (ratio to the fundamental, level, decay as a multiple of tau)

MALLET = ((1, 1.0, 1.0), (2, 0.10, 0.5), (4, 0.30, 0.16))                 # marimba-like: the 2-octave overtone, fast
CHIME = ((1, 1.0, 1.0), (2, 0.24, 0.6), (3, 0.08, 0.4), (5.4, 0.05, 0.12))  # a little glassier
PURE = ((1, 1.0, 1.0), (2, 0.16, 0.7), (3, 0.05, 0.5))                   # SOFT, rounding off
ROUND = ((1, 1.0, 1.0), (2, 0.34, 0.55), (3, 0.12, 0.35), (4, 0.04, 0.25))  # low notes: WARM, rounding off


def note(f: float, dur: float, tau: float, parts=PURE, attack: float = 0.004,
         settle: tuple[float, float] | None = None) -> list[float]:
    """One note of ``f`` Hz, each partial decaying at its own rate. ``settle`` =
    (start ratio, seconds): the pitch starts that far off and eases onto ``f``."""
    n = _n(dur)
    na = max(1, _n(attack))
    out = [0.0] * n
    phase = 0.0
    for i in range(n):
        t = i / RATE
        fi = f * (1 + (settle[0] - 1) * math.exp(-t / settle[1])) if settle else f
        phase += 2 * math.pi * fi / RATE
        td = max(0.0, t - attack)
        s = 0.0
        for ratio, level, k in parts:
            if fi * ratio < RATE / 2 - 2000:
                s += level * math.sin(ratio * phase) * math.exp(-td / (tau * k))
        env = 0.5 * (1 - math.cos(math.pi * i / na)) if i < na else 1.0
        out[i] = s * env
    return _fade_out(out)


def _notes(*parts) -> list[float]:
    """(at seconds, gain, note samples), ... mixed into one buffer."""
    buf: list[float] = []
    for at, gain, samples in parts:
        _mix(buf, samples, at=at, gain=gain)
    return _fade_out(buf)


C4, E4, F4, G4 = 261.63, 329.63, 349.23, 392.0
C5, E5, F5, G5, A5 = 523.25, 659.25, 698.46, 783.99, 880.0
D6 = 1174.66


# record / play: a positive "go", rising
def record_a():
    return _notes((0.0, 0.85, note(C5, 0.150, 0.045, MALLET)),
                  (0.075, 1.0, note(F5, 0.245, 0.075, MALLET)))


def record_b():
    return _notes((0.0, 0.70, note(C5, 0.120, 0.040)),
                  (0.050, 0.80, note(E5, 0.120, 0.040)),
                  (0.100, 1.0, note(G5, 0.230, 0.075)))


def record_c():
    return _notes((0.0, 0.75, note(G4, 0.130, 0.035, ROUND)),
                  (0.070, 1.0, note(G5, 0.270, 0.080, CHIME)),
                  (0.070, 0.18, note(D6, 0.200, 0.050, PURE)))


# pause: a gentle "hold", temporary, never lower than stop
def pause_a():
    return _notes((0.0, 1.0, note(F5, 0.100, 0.028, MALLET)),
                  (0.120, 0.80, note(F5, 0.120, 0.032, MALLET)))


def pause_b():
    return _notes((0.0, 1.0, note(G5, 0.140, 0.045)),
                  (0.100, 0.85, note(E5, 0.170, 0.050)))


def pause_c():
    return _notes((0.0, 1.0, note(A5, 0.100, 0.025, CHIME)),
                  (0.130, 0.85, note(G5, 0.130, 0.035, CHIME)))


# stop: "done / off", lower than pause, rounded, a longer ring; final but not alarming
def stop_a():
    return _notes((0.0, 0.80, note(C5, 0.140, 0.045, MALLET)),
                  (0.100, 1.0, note(F4, 0.245, 0.090, MALLET)))


def stop_b():
    return _notes((0.0, 0.70, note(G4, 0.120, 0.040, ROUND)),
                  (0.060, 0.80, note(E4, 0.120, 0.040, ROUND)),
                  (0.120, 1.0, note(C4, 0.225, 0.080, ROUND)))


def stop_c():
    return note(C4, 0.340, 0.120, ROUND, attack=0.010, settle=(1.02, 0.040))


# name -> variant -> (sound, what it is)
VARIANTS = {
    "record": {
        "A": (record_a, "soft mallet (marimba-like), a perfect fourth up: C5 then F5"),
        "B": (record_b, "soft sine, a quick rising major arpeggio: C5 E5 G5, the last one rings"),
        "C": (record_c, "an octave lift: a warm G4 tap into a glassy G5 chime with a faint D6 sparkle"),
    },
    "pause": {
        "A": (pause_a, "soft mallet, two equal taps on F5 (the note record A lands on), the second softer"),
        "B": (pause_b, "soft sine, a short falling minor third: G5 then E5"),
        "C": (pause_c, "glassy chime, a 'tick-tock' a step down: A5 then G5, short and light"),
    },
    "stop": {
        "A": (stop_a, "soft mallet, a falling fifth that lands low: C5 then F4 (mirrors record A)"),
        "B": (stop_b, "warm, a falling major arpeggio an octave below record B: G4 E4 C4, the last rings"),
        "C": (stop_c, "one low, warm, rounded C4 with a longer decay, easing onto its pitch"),
    },
}


def render(name: str, variant: str | None = None) -> array.array:
    """One sound as 16-bit samples, its peak normalised to its level. ``variant``:
    one of VARIANTS[name] instead of the default."""
    buf = VARIANTS[name][variant][0]() if variant else SOUNDS[name]()
    peak = max(abs(v) for v in buf) or 1.0
    scale = 10 ** (LEVELS.get(name, PEAK_DBFS) / 20) * 32767 / peak
    return array.array("h", (int(round(v * scale)) for v in buf))


PREVIEW_GAP_S = 0.6       # between the options in the preview
SEQUENCE_GAP_S = 1.0      # between the steps of a sequence
MARKER_DBFS = -30.0


def _marker(count: int) -> array.array:
    """``count`` soft ticks, then a breath: the group number, to count along."""
    tick = click(0.030, seed=77, fc=2400.0, tau=0.004)
    buf: list[float] = []
    for k in range(count):
        _mix(buf, tick, at=0.16 * k)
    peak = max(abs(v) for v in buf) or 1.0
    scale = 10 ** (MARKER_DBFS / 20) * 32767 / peak
    out = array.array("h", (int(round(v * scale)) for v in buf))
    return out + array.array("h", [0]) * _n(0.55)


def preview(prefix: Path) -> list[Path]:
    """Write ``<prefix>-options.wav`` (record A B C, pause A B C, stop A B C; group
    1, 2, 3 announced by that many ticks), ``<prefix>-options.txt`` (its legend) and
    ``<prefix>-sequence-{A,B,C}.wav`` (record, pause, record, stop, 1 s apart)."""
    prefix.parent.mkdir(parents=True, exist_ok=True)
    silence = array.array("h", [0])
    reel = silence * _n(0.3)
    legend = ["Momento bar sounds: record / pause / stop options",
              f"{prefix.name}-options.wav: each group starts with 1, 2 or 3 soft ticks,",
              f"then options A, B, C with {PREVIEW_GAP_S:.1f} s between them.", ""]
    t = 0.3
    for g, name in enumerate(VARIANTS, 1):
        reel += _marker(g)
        t += len(_marker(g)) / RATE
        legend.append(f"{g} tick{'s' if g > 1 else ''}: {name}")
        for v, (_, desc) in VARIANTS[name].items():
            s = render(name, v)
            legend.append(f"  {t:5.1f} s  {name} {v} ({len(s) / RATE * 1000:.0f} ms): {desc}")
            reel += s + silence * _n(PREVIEW_GAP_S)
            t += len(s) / RATE + PREVIEW_GAP_S
        legend.append("")
    paths = [prefix.with_name(prefix.name + "-options.wav"), prefix.with_name(prefix.name + "-options.txt")]
    write_wav(paths[0], reel)
    legend.append(f"{prefix.name}-sequence-A/B/C.wav: record, pause, record (play again), stop,")
    legend.append(f"{SEQUENCE_GAP_S:.0f} s apart, with set A (record A, pause A, stop A), B or C.")
    for v in ("A", "B", "C"):
        seq = silence * _n(0.3)
        for step in ("record", "pause", "record", "stop"):
            seq += render(step, v) + silence * _n(SEQUENCE_GAP_S)
        path = prefix.with_name(f"{prefix.name}-sequence-{v}.wav")
        write_wav(path, seq[:len(seq) - _n(SEQUENCE_GAP_S) + _n(0.5)])
        paths.append(path)
    paths[1].write_text("\n".join(legend) + "\n")
    return paths


def write_wav(path: Path, samples: array.array) -> None:
    data = samples if sys.byteorder == "little" else array.array("h", samples)
    if sys.byteorder != "little":
        data.byteswap()
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(data.tobytes())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, default=OUT, help="where the WAVs go (default: momento/sounds)")
    ap.add_argument("--reel", type=Path, help="also write every sound in a row to this WAV")
    ap.add_argument("--use", action="append", default=[], metavar="NAME=V",
                    help="make NAME from candidate V instead of the default (e.g. record=B; repeatable)")
    ap.add_argument("--preview", type=Path, metavar="PREFIX",
                    help="only write the candidates' preview: PREFIX-options.wav/.txt, PREFIX-sequence-A/B/C.wav")
    args = ap.parse_args(argv)
    if args.preview:
        for path in preview(args.preview):
            print(path)
        return 0
    use = dict(u.split("=", 1) for u in args.use)
    for name, v in use.items():
        if v not in VARIANTS.get(name, {}):
            ap.error(f"--use {name}={v}: no such candidate")
    args.out.mkdir(parents=True, exist_ok=True)
    reel = array.array("h")
    gap = array.array("h", [0]) * _n(REEL_GAP_S)
    for name in SOUNDS:
        s = render(name, use.get(name))
        write_wav(args.out / f"{name}.wav", s)
        peak = max(abs(v) for v in s)
        print(f"{name:14s} {len(s) / RATE * 1000:4.0f} ms  peak {20 * math.log10(peak / 32767):6.1f} dBFS")
        reel.extend(s)
        reel.extend(gap)
    if args.reel:
        write_wav(args.reel, reel)
        print(f"reel: {args.reel} ({len(reel) / RATE:.1f} s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
