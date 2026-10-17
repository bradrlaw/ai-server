#!/usr/bin/env python3
"""Analyze fan-noise recordings: levels, room correction, tones, fan sweeps.

Record WAV (AIFF/FLAC also work) at a fixed gain with the mic in one spot.
Levels are relative to the recorder's digital full scale (dBFS), so compare
only recordings made with the same mic, position and gain. --cal-offset DB
(SPL = dBFS + DB, e.g. from a calibrated mic) reports dB SPL instead.

  Whole recordings, one per condition (steady state; --trim s cut off each end):
    fan-noise-analyze.py --room room-before.wav --room room-after.wav \\
        idle=idle.wav gpu1=gpu1-load.wav both=both-load.wav --csv before.csv --plot before.png
  One recording of scripts/fan-noise-sweep.py, cut into steps by its steps CSV:
    fan-noise-analyze.py --room room.wav --steps fan-sweep-X.steps.csv sweep.wav
  Two sessions (e.g. before / after a cooling change), from their --csv files:
    fan-noise-analyze.py --compare before.csv after.csv
  (its notes column marks the side, A or B, whose row carries a note: busy,
  stalled, clipped, unsteady, drift, stopped early; the notes are listed under the table)

Columns (A-weighted like hearing, except LZeq):
  LAeq        the average level
  LA90        the level exceeded 90% of the time: the steady noise, ignoring
              short sounds (a car, a disk seek). The figure to compare.
  LA10        the level exceeded 10% of the time; LZeq = unweighted, 10 Hz-20 kHz
  above room  LA90 minus the room's LA90
  machine     the machine alone: LA90 with the room subtracted as energy. Blank
              when under 3 dB above the room (can't be separated); 10+ dB above,
              the room barely matters.
  delta       machine difference to the reference; "~" = plain LA90 difference,
              used when one side is within 3 dB of the room: the real difference
              is at least that big
Tones (whine, hum), by the tone-to-noise ratio method of ECMA-418-1: TNR = dB a
tone stands above the noise in its critical band. About -4 dB is just audible;
PROMINENT = the standard's criterion (8 dB from 1 kHz up, more below: 13 dB at
250 Hz), to be confirmed by ear. "Nx GPU1 fan" = at N times that fan's rotation
rate (a blade or motor tone); "mains?" = a multiple of 60 Hz; "room" = also in
the room recording; "coarse FFT" = the window was too short to resolve a tone
that low (lines must be under 1% of its frequency): its TNR is rough, and it is
never called PROMINENT.
Rules of thumb: 1 dB is about the smallest change you hear side by side, 3 dB
is double the sound power, 10 dB sounds about twice as loud.
"""
import argparse
import csv
import math
import os
import sys

VENV_PY = "/srv/ai/venvs/comfyui/bin/python"    # has numpy, scipy, soundfile, matplotlib
try:
    import numpy as np
    try:
        import soundfile as sf
    except ImportError:
        sf = None
        from scipy.io import wavfile
except ImportError:
    if os.environ.get("FAN_NOISE_ANALYZE_REEXEC") or not os.path.exists(VENV_PY):
        sys.exit(f"needs numpy plus soundfile or scipy: run it with {VENV_PY}")
    os.environ["FAN_NOISE_ANALYZE_REEXEC"] = "1"
    os.execv(VENV_PY, [VENV_PY, os.path.abspath(__file__), *sys.argv[1:]])

FRAME_SEC = 0.125           # level samples ("fast" time weighting)
MIN_FRAMES = 8              # 1 s: fewer and a window has no levels
TONE_FMIN, TONE_FMAX = 89.1, 11220.0     # ECMA-418-1's range for discrete tones
TONE_AUDIBLE = -4.0         # TNR (dB) where a tone becomes audible in noise (ECMA-418-1 clause 5)
TONE_FLOOR = 100.0          # dB under the total level: weaker "tones" are digital artefacts
TONES_SHOWN = 5
MAINS_HZ = 60
MIN_ABOVE_ROOM = 3.0
ROOM_SPREAD = 1.0           # dB between room recordings worth a warning
DRIFT_WARN = 1.0            # dB between a sweep's lead-in and lead-out
UNSTEADY = 3.0              # dB of LAeq over LA90 worth a note
MIN_OFFSET_R = 0.5          # correlation needed to trust an automatic sweep offset
NOMINAL = (25, 31.5, 40, 50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 630, 800, 1000,
           1250, 1600, 2000, 2500, 3150, 4000, 5000, 6300, 8000, 10000, 12500, 16000, 20000)
EXACT = [1000 * 10 ** (n / 10) for n in range(-16, 14)]     # 1/3-octave centres (IEC 61260)
LEVELS = ("LAeq", "LA90", "LA10", "LZeq")
CSV_COLS = ["kind", "label", "file", "start_s", "end_s", "dur_s", "units", *LEVELS, "above_room",
            "machine", "delta", "ref", "duty_pct", "rpm", "flags", "notes", "tone_hz", "tone_tnr",
            "tones", *(f"A_{b:g}Hz" for b in NOMINAL)]


def a_gain(f):
    """A-weighting as a power gain (IEC 61672; +2.00 dB makes 1 kHz 0 dB)."""
    f2 = np.square(np.asarray(f, dtype=float))
    ra = 12194.0 ** 2 * f2 ** 2 / ((f2 + 20.6 ** 2) * np.sqrt((f2 + 107.7 ** 2) * (f2 + 737.9 ** 2))
                                   * (f2 + 12194.0 ** 2))
    return ra ** 2 * 10 ** 0.2


def a_db(f):
    return 10 * math.log10(float(a_gain(f)))


def db(p):
    return 10 * math.log10(p) if p > 0 else None


def hann(n):
    return 0.5 - 0.5 * np.cos(2 * np.pi * np.arange(n) / n)      # periodic: DC leaks into bin 1 only


def one_sided(n):
    c = np.full(n // 2 + 1, 2.0)
    c[0] = 1.0
    if n % 2 == 0:
        c[-1] = 1.0
    return c


def fmt(v, sign=False):
    if v is None:
        return ""
    return f"{v:+.1f}" if sign else f"{v:.1f}"


def to_float(data):
    if data.dtype == np.uint8:
        data = (data.astype(np.float32) - 128) / 128
    elif data.dtype == np.int16:
        data = data.astype(np.float32) / 32768
    elif data.dtype == np.int32:                    # scipy left-aligns 24-bit PCM in int32
        data = data.astype(np.float32) / 2 ** 31
    else:
        data = data.astype(np.float32)
    return data if data.ndim == 2 else data[:, None]


class Recording:
    """One audio file: the chosen channel and its 125 ms A/Z-weighted frame powers."""

    def __init__(self, path, channel=None):
        self.path = path
        if not os.path.isfile(path):
            raise SystemExit(f"{path}: no such file")
        try:
            if sf is not None:
                data, fs = sf.read(path, dtype="float32", always_2d=True)
            else:
                fs, data = wavfile.read(path)
                data = to_float(data)
        except Exception as e:      # noqa: BLE001 -- libsndfile and scipy raise assorted types
            raise SystemExit(f"{path}: can't read it as audio ({e})")
        self.fs = int(fs)
        nch = data.shape[1]
        if channel:
            if not 1 <= channel <= nch:
                raise SystemExit(f"{path}: has {nch} channel(s), no channel {channel}")
            ch = channel - 1
        else:
            ch = int(np.argmax([np.mean(np.square(data[:, c], dtype=np.float64)) for c in range(nch)]))
        x = np.ascontiguousarray(data[:, ch])
        del data
        self.x = x
        self.duration = len(x) / self.fs
        self.notes = []
        if nch > 1:
            self.notes.append(f"channel {ch + 1} of {nch}" + ("" if channel else " (loudest)"))
        clipped = int(np.count_nonzero(np.abs(x) >= 0.999))
        if clipped:
            self.notes.append(f"CLIPPED ({clipped} samples): lower the gain, record again")
        if self.fs < 44100:
            self.notes.append(f"{self.fs} Hz: record at 44.1 or 48 kHz")
        self.frame_len = n = int(round(FRAME_SEC * self.fs))
        m = len(x) // n
        if m < MIN_FRAMES:
            raise SystemExit(f"{path}: too short ({self.duration:.1f} s)")
        f = np.fft.rfftfreq(n, 1 / self.fs)
        w = hann(n)
        gz = one_sided(n) * ((f >= 10) & (f <= 20000)) / (n * np.sum(w ** 2))
        ga = gz * a_gain(f)
        self.pa, self.pz = np.empty(m), np.empty(m)
        self.silent = np.empty(m, dtype=bool)
        for i in range(0, m, 256):
            seg = x[i * n:min(m, i + 256) * n].reshape(-1, n).astype(np.float64)
            self.silent[i:i + len(seg)] = ~seg.any(axis=1)
            spec = np.square(np.abs(np.fft.rfft(seg * w, axis=1)))
            self.pa[i:i + len(seg)] = spec @ ga
            self.pz[i:i + len(seg)] = spec @ gz
        if self.silent.mean() > 0.01:
            self.notes.append(f"{100 * self.silent.mean():.0f}% digital silence (dropouts?)")

    def frames(self, t0, t1):
        """A and Z powers of the frames wholly inside [t0, t1), digital silence left out."""
        n = self.frame_len
        i0 = max(0, math.ceil(t0 * self.fs / n - 1e-9))
        i1 = min(len(self.pa), math.floor(t1 * self.fs / n + 1e-9))
        keep = ~self.silent[i0:i1]
        return self.pa[i0:i1][keep], self.pz[i0:i1][keep]

    def spectrum(self, t0, t1):
        """Welch power per line of [t0, t1): 0.25 Hz lines, coarser (down to 4 Hz) when the
        window is too short for 8 averages. ECMA-418-1 clause 7 wants lines under 1% of a
        tone's frequency (0.25% recommended); Tone tags the ones too low for the lines."""
        x = self.x[int(t0 * self.fs):int(t1 * self.fs)]
        n = 4 * self.fs
        while n > self.fs // 4 and len(x) < 4.5 * n:
            n //= 2
        while n > len(x):
            n //= 2
        if n < 64:
            return None, None
        hop = n // 2
        k = 1 + (len(x) - n) // hop
        w = hann(n)
        acc = np.zeros(n // 2 + 1)
        batch = max(1, 2 ** 22 // n)        # segments per FFT call: bounded memory
        for i in range(0, k, batch):
            idx = (np.arange(i, min(k, i + batch)) * hop)[:, None] + np.arange(n)
            seg = x[idx].astype(np.float64)
            seg -= seg.mean(axis=1, keepdims=True)
            acc += np.square(np.abs(np.fft.rfft(seg * w, axis=1))).sum(axis=0)
        return np.fft.rfftfreq(n, 1 / self.fs), one_sided(n) * acc / (k * n * np.sum(w ** 2))


def crit_band(f0):
    """ECMA-418-1 clause 10: the critical band centred on f0 Hz -> (width, low edge, high edge)."""
    dfc = 25.0 + 75.0 * (1.0 + 1.4 * (f0 / 1000.0) ** 2) ** 0.69
    f1 = f0 - dfc / 2 if f0 < 500 else -dfc / 2 + math.sqrt(dfc ** 2 + 4 * f0 ** 2) / 2
    return dfc, f1, f1 + dfc


def tnr_criterion(f):
    """ECMA-418-1 clause 11.5: the TNR (dB) from which a tone at f Hz is prominent."""
    return 8.0 + 8.33 * math.log10(1000.0 / f) if f < 1000 else 8.0


def proximity(fp):
    """ECMA-418-1 Formula 14: tones closer than this (Hz) to a tone at fp sound as one
    (from 1 kHz up, any two in one critical band do)."""
    return 21.0 * 10 ** (1.2 * abs(math.log10(fp / 212.0)) ** 1.8) if fp < 1000 else math.inf


class Tone:
    def __init__(self, freq, tnr, level, merged=None, df=0.0):
        self.freq, self.tnr, self.level = freq, tnr, level
        self.crit = tnr_criterion(freq)
        coarse = df >= 0.01 * freq      # ECMA-418-1 clause 7: lines under 1% of the frequency
        self.prominent = tnr >= self.crit and not coarse
        self.tags = [f"with {merged:.0f} Hz"] if merged is not None else []
        if coarse:
            self.tags.append("coarse FFT")

    @property
    def margin(self):
        return self.tnr - self.crit

    def __str__(self):
        tags = f" ({', '.join(self.tags)})" if self.tags else ""
        return f"{self.freq:.0f} Hz {self.tnr:+.1f}{' PROMINENT' if self.prominent else ''}{tags}"


def tone_edges(p, k, half, lim_lo, lim_hi):
    """ECMA-418-1 clause 11.2: the tone band around peak line k ends at the lowest line
    within `half` lines on each side; one on the window's edge gives way to the next
    local minimum beyond it (here: not past the critical band, lines lim_lo..lim_hi)."""
    a = max(lim_lo, k - half)
    lo = a + int(np.argmin(p[a:k]))
    if lo == a:
        while lo > lim_lo and p[lo - 1] < p[lo]:
            lo -= 1
    b = min(lim_hi, k + half)
    hi = k + 1 + int(np.argmin(p[k + 1:b + 1]))
    if hi == b:
        while hi < lim_hi and p[hi + 1] < p[hi]:
            hi += 1
    return lo, hi


def find_tones(f, pbin, cal, top=TONES_SHOWN):
    """Tones by ECMA-418-1 clause 11 (tone-to-noise ratio): the prominent ones first, then
    by how far each clears its criterion.

    A tone is a line that tops the 15% of a critical band centred on it. Its power is
    what stands above the straight line between the lowest lines either side of it
    (Formula 9); its noise is the rest of the critical band, scaled to the band's exact
    width (Formula 10). The strongest other tone in that band is taken out of the noise
    as well. When that one is weaker and proximate (clause 11.6: judged from the
    stronger), it merges into the tone; a weaker tone a stronger one didn't absorb is
    a tone of its own."""
    if f is None or len(f) < 32:
        return []
    df = float(f[1] - f[0])
    cum = np.concatenate(([0.0], np.cumsum(pbin)))
    floor = cum[-1] * 10 ** (-TONE_FLOOR / 10)
    ks = np.arange(max(1, int(np.searchsorted(f, TONE_FMIN))),
                   min(len(f) - 1, int(np.searchsorted(f, TONE_FMAX, side="right"))))
    ks = ks[(pbin[ks] > pbin[ks - 1]) & (pbin[ks] >= pbin[ks + 1])]
    lvl = 10 * np.log10(pbin + 1e-30)

    def tnr(pt, noise, c):
        noise = max(noise, 1e-12 * c["ptot"])       # a noise-free (synthetic) band
        return 10 * math.log10(pt * c["n"] * df / (noise * c["dfc"]))

    def excess(o, c):
        """Tone o's power on c's critical-band lines: what is over o's Formula 9 baseline."""
        u, v = max(o["lo"], c["i1"]), min(o["hi"], c["i2"])
        if u > v:
            return 0.0
        slope = (pbin[o["hi"]] - pbin[o["lo"]]) / (o["hi"] - o["lo"])
        base = (v - u + 1) * (pbin[o["lo"]] + slope * ((u + v) / 2 - o["lo"]))
        return max(0.0, float(cum[v + 1] - cum[u] - base))

    def in_band(o, c):
        return o is not c and c["i1"] <= o["k"] <= c["i2"]

    cands = []
    for k in ks:
        dfc, f1, f2 = crit_band(f[k])
        if f2 > f[-1]:
            continue
        half = max(2, int(round(0.075 * dfc / df)))
        if pbin[k] < pbin[max(0, k - half):k + half + 1].max():
            continue                # a sidelobe or the shoulder of a stronger line
        i1, i2 = int(np.searchsorted(f, f1)), int(np.searchsorted(f, f2, side="right")) - 1
        lo, hi = tone_edges(pbin, k, half, i1, i2)
        pt = float(cum[hi + 1] - cum[lo] - (hi - lo + 1) * (pbin[lo] + pbin[hi]) / 2)
        if pt <= floor:
            continue
        a, b, c = lvl[k - 1], lvl[k], lvl[k + 1]
        cands.append({"k": int(k), "freq": float((k + 0.5 * (a - c) / (a - 2 * b + c)) * df),
                      "pt": pt, "lo": lo, "hi": hi, "dfc": dfc, "i1": i1, "i2": i2,
                      "n": i2 - i1 + 1, "ptot": float(cum[i2 + 1] - cum[i1]), "tone": False})
    found, grew = [], True
    while grew:                     # a tone: audible once the tones beside it are out of its noise
        grew = False
        for c in cands:
            if not c["tone"]:
                others = sum(excess(o, c) for o in found if in_band(o, c))
                if tnr(c["pt"], c["ptot"] - c["pt"] - others, c) >= TONE_AUDIBLE:
                    c["tone"] = grew = True
                    found.append(c)

    tones, merged = [], set()
    for c in sorted(found, key=lambda c: -c["pt"]):
        if c["k"] in merged:
            continue
        s = max((o for o in found if in_band(o, c)), key=lambda o: o["pt"], default=None)
        pt, noise, partner = c["pt"], c["ptot"] - c["pt"], None
        if s is not None:
            noise -= excess(s, c)
            # clause 11.6: c is the primary; a weaker secondary within c's proximity is heard
            # as part of it. A stronger s didn't absorb c (c isn't in `merged`): c stands alone.
            if s["pt"] <= c["pt"] and abs(s["freq"] - c["freq"]) < proximity(c["freq"]):
                pt += s["pt"]
                merged.add(s["k"])
                partner = s["freq"]
        t = Tone(c["freq"], tnr(pt, noise, c), 10 * math.log10(pt) + a_db(c["freq"]) + cal, partner, df)
        if t.tnr >= TONE_AUDIBLE:
            tones.append(t)
    tones.sort(key=lambda t: (not t.prominent, -t.margin))
    return tones[:top]


def tag_tones(tones, df, room_freqs=(), fan_rpm=()):
    for t in tones:
        n = round(t.freq / MAINS_HZ)
        if 1 <= n <= 20 and abs(t.freq - n * MAINS_HZ) <= max(0.5, 0.25 * df):
            t.tags.append("mains?")
        for label, rpm in fan_rpm:
            rps = rpm / 60
            n = round(t.freq / rps)
            if 1 <= n <= 16 and abs(t.freq - n * rps) <= max(0.005 * t.freq, df):
                t.tags.append(f"{n}x {label} fan")
        if any(abs(t.freq - r) <= max(0.01 * r, 2 * df) for r in room_freqs):
            t.tags.append("room")


def line_hz(r):
    return float(r.f[1] - r.f[0]) if r.f is not None else 0.0


def band_levels(f, pbin, cal):
    if f is None:
        return [None] * len(EXACT)
    p = pbin * a_gain(f)
    out = []
    for fc in EXACT:
        lo, hi = fc * 10 ** -0.05, fc * 10 ** 0.05
        v = db(float(np.sum(p[(f >= lo) & (f < hi)]))) if lo < f[-1] else None
        out.append(None if v is None else v + cal)
    return out


class Result:
    """Levels, 1/3-octave bands and tones of one stretch of audio."""

    def __init__(self, label, kind, path, pa, pz, f, pbin, cal):
        self.label, self.kind, self.path = label, kind, path
        self.pa, self.pz, self.f, self.pbin, self.cal = pa, pz, f, pbin, cal
        self.t0 = self.t1 = None
        self.dur = len(pa) * FRAME_SEC
        self.lv = dict.fromkeys(LEVELS)
        if len(pa) >= MIN_FRAMES:
            for key, v in (("LAeq", np.mean(pa)), ("LA90", np.percentile(pa, 10)),
                           ("LA10", np.percentile(pa, 90)), ("LZeq", np.mean(pz))):
                self.lv[key] = None if db(float(v)) is None else db(float(v)) + cal
        self.bands = band_levels(f, pbin, cal)
        self.tones = find_tones(f, pbin, cal)
        self.notes, self.extra = [], {}
        self.above = self.machine = None
        self.delta = ""
        if self.lv["LAeq"] is not None and self.lv["LA90"] is not None \
                and self.lv["LAeq"] - self.lv["LA90"] > UNSTEADY:
            self.notes.append(f"unsteady (LAeq {self.lv['LAeq'] - self.lv['LA90']:.1f} dB over LA90)")

    @property
    def la90(self):
        return self.lv["LA90"]


def window_result(rec, label, kind, t0, t1, cal):
    pa, pz = rec.frames(t0, t1)
    f, pbin = rec.spectrum(t0, t1) if t1 - t0 >= 1 else (None, None)
    r = Result(label, kind, rec.path, pa, pz, f, pbin, cal)
    r.t0, r.t1 = t0, t1
    return r


def empty_result(label, kind, path, cal):
    return Result(label, kind, path, np.empty(0), np.empty(0), None, None, cal)


def trimmed(rec, trim):
    if rec.duration >= 2 * trim + 10:
        return trim, rec.duration - trim
    rec.notes.append(f"only {rec.duration:.0f} s, not trimmed")
    return 0.0, rec.duration


def machine(level, room):
    if level is None or room is None or level - room < MIN_ABOVE_ROOM:
        return None
    return 10 * math.log10(10 ** (level / 10) - 10 ** (room / 10))


def delta(a, b, room):
    """b - a: of the machine levels when both stand clear of the room, else '~' LA90."""
    if a is None or b is None:
        return ""
    if room is None:
        return f"{b - a:+.1f}"
    ma, mb = machine(a, room), machine(b, room)
    if ma is not None and mb is not None:
        return f"{mb - ma:+.1f}"
    return f"~{b - a:+.1f}"


def load_rooms(paths, channel, trim, cal):
    """Per-file room results, plus the combined one the corrections use (or None)."""
    files = []
    for i, p in enumerate(paths, 1):
        rec = Recording(p, channel)
        t0, t1 = trimmed(rec, trim)
        r = window_result(rec, f"room {i}" if len(paths) > 1 else "room", "room-file", t0, t1, cal)
        r.notes = rec.notes + r.notes
        files.append(r)
    if not files:
        return [], None
    if len(files) == 1:
        files[0].kind = "room"
        return [], files[0]
    spec = [r for r in files if r.f is not None]
    f0 = spec[0].f
    df0 = f0[1] - f0[0]
    dens = [np.interp(f0, r.f, r.pbin / (r.f[1] - r.f[0])) for r in spec]
    pbin = np.average(dens, axis=0, weights=[r.dur for r in spec]) * df0
    room = Result("room", "room", "+".join(r.path for r in files),
                  np.concatenate([r.pa for r in files]), np.concatenate([r.pz for r in files]),
                  f0, pbin, cal)
    # --compare reads only the combined row: it carries each file's problems
    room.notes += [f"{r.label}: {n}" for r in files for n in r.notes if not n.startswith("channel ")]
    lv = [r.la90 for r in files if r.la90 is not None]
    if lv and max(lv) - min(lv) > ROOM_SPREAD:
        room.notes.append(f"room recordings differ by {max(lv) - min(lv):.1f} dB (LA90): the room "
                          f"changed; differences that small are uncertain")
    return files, room


def room_tone_freqs(room):
    if room is None:
        return []
    return [t.freq for t in find_tones(room.f, room.pbin, room.cal, top=30)]


def finish(results, room, ref):
    """above room / machine / delta columns, against `ref` (a Result or None)."""
    rl = room.la90 if room else None
    for r in results:
        if r.la90 is None:
            continue
        if rl is not None:
            r.above = r.la90 - rl
            r.machine = machine(r.la90, rl)
        if ref is not None and ref.la90 is not None:
            r.delta = "ref" if r is ref else delta(ref.la90, r.la90, rl)


def split_label(spec):
    if "=" in spec and not os.path.exists(spec):
        label, path = spec.split("=", 1)
        if label:
            return label, path
    return os.path.splitext(os.path.basename(spec))[0], spec


def print_table(header, rows):
    print("| " + " | ".join(header) + " |")
    print("|" + "|".join("---" for _ in header) + "|")
    for row in rows:
        print("| " + " | ".join(row) + " |")


def print_room(rooms, room, units):
    if room is None:
        print("No --room recording: levels include the room; nothing is subtracted.")
        return
    src = ", ".join(f"{os.path.basename(r.path)} {fmt(r.la90)}" for r in rooms) \
        or os.path.basename(room.path)
    print(f"Room (machine off): LA90 {fmt(room.la90)} {units}, LAeq {fmt(room.lv['LAeq'])}  ({src})")
    for n in room.notes:
        print(f"  NOTE: {n}")
    for r in rooms:                 # the other notes are in room.notes already
        for n in r.notes:
            if n.startswith("channel "):
                print(f"  {r.label}: {n}")


def print_tones(results, room):
    rows = ([room] if room is not None else []) + [r for r in results if r.tones]
    if not any(r.tones for r in rows):
        return
    print("\nTones (tone-to-noise ratio, dB; about -4 is just audible; PROMINENT = ECMA-418-1's "
          "criterion,\nconfirm by ear):")
    width = max(len(r.label) for r in rows)
    for r in rows:
        if r.tones:
            print(f"  {r.label:<{width}}  " + " · ".join(str(t) for t in r.tones))


def legend(units, ref_label):
    print(f"\nLevels in {units}, A-weighted (LZeq unweighted). LA90 = exceeded 90% of the time: "
          f"the steady noise.\nmachine = LA90 with the room subtracted (blank: under "
          f"{MIN_ABOVE_ROOM:g} dB above the room). delta = machine minus {ref_label}'s;\n\"~\" = "
          f"plain LA90 difference (a side within {MIN_ABOVE_ROOM:g} dB of the room): the real "
          f"difference is at least that big.\n1 dB ~ just audible side by side · 3 dB = double "
          f"the sound power · 10 dB ~ twice as loud.")


def write_csv(path, results, units):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(CSV_COLS)
        for r in results:
            ref = r.extra.get("ref", "")
            top = r.tones[0] if r.tones else None
            w.writerow([r.kind, r.label, os.path.basename(r.path),
                        "" if r.t0 is None else f"{r.t0:.2f}", "" if r.t1 is None else f"{r.t1:.2f}",
                        f"{r.dur:.1f}", units, *(fmt(r.lv[k]) for k in LEVELS), fmt(r.above, True),
                        fmt(r.machine), r.delta, ref, r.extra.get("duty", ""), r.extra.get("rpm", ""),
                        r.extra.get("flags", ""), "; ".join(r.notes),
                        "" if top is None else f"{top.freq:.1f}", "" if top is None else fmt(top.tnr, True),
                        "; ".join(f"{t.freq:.1f}Hz/TNR{t.tnr:+.1f}/{t.level:.1f}dB(A)"
                                  + ("/PROMINENT" if t.prominent else "")
                                  + "".join(f"/{g}" for g in t.tags) for t in r.tones),
                        *(fmt(b) for b in r.bands)])


def plot(path, results, room, units, sweep_lines=None):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    shown = [r for r in results if r.f is not None]
    nax = 3 if sweep_lines else 2
    fig, axs = plt.subplots(nax, 1, figsize=(11, 4 * nax))
    cmap = plt.get_cmap("tab10" if len(shown) <= 10 else "viridis")
    x = np.arange(len(NOMINAL))
    for i, r in enumerate(shown):
        c = cmap(i) if len(shown) <= 10 else cmap(i / (len(shown) - 1))
        axs[0].plot(x, [np.nan if v is None else v for v in r.bands], marker=".", color=c, label=r.label)
        m = (r.f >= 20) & (r.f <= 20000)
        axs[1].semilogx(r.f[m], 10 * np.log10(r.pbin[m] + 1e-30) + r.cal, color=c, lw=0.7, label=r.label)
    if room is not None and room.f is not None:
        axs[0].plot(x, [np.nan if v is None else v for v in room.bands], "--", color="0.5", label="room")
        m = (room.f >= 20) & (room.f <= 20000)
        axs[1].semilogx(room.f[m], 10 * np.log10(room.pbin[m] + 1e-30) + room.cal, "--", color="0.5",
                        lw=0.7, label="room")
    axs[0].set_xticks(x, [f"{b:g}" for b in NOMINAL], rotation=90)
    axs[0].set(title="1/3-octave bands, A-weighted", xlabel="Hz", ylabel=f"{units}(A)")
    df = shown[0].f[1] - shown[0].f[0] if shown else 0
    axs[1].set(title=f"Spectrum (unweighted, {df:.1f} Hz bins)", xlabel="Hz", ylabel=units)
    if sweep_lines:
        for name, pts in sweep_lines.items():
            pts = sorted(p for p in pts if p[1] is not None)
            if pts:
                axs[2].plot(*zip(*pts), marker="o", label=name)
        if room is not None and room.la90 is not None:
            axs[2].axhline(room.la90, ls="--", color="0.5", label="room")
        axs[2].set(title="Fan sweep: LA90 by duty", xlabel="fan duty %", ylabel=f"LA90 {units}(A)")
    for ax in axs:
        ax.grid(True, which="both", alpha=0.3)
        if ax.get_legend_handles_labels()[0]:
            ax.legend(fontsize=7, ncol=2)
    fig.tight_layout()
    fig.savefig(path, dpi=110)


# ---------------------------------------------------------------- sweep mode
def read_steps(path):
    try:
        with open(path, newline="") as f:
            lines = f.read().splitlines()
    except OSError as e:
        raise SystemExit(f"{path}: {e.strerror}")
    meta = [ln[1:].strip() for ln in lines if ln.startswith("#")]
    rd = csv.DictReader([ln for ln in lines if ln.strip() and not ln.startswith("#")])
    rows = list(rd)
    zones = [c[:-len("_set_pct")] for c in (rd.fieldnames or []) if c.endswith("_set_pct")]
    if not rows or not zones:
        raise SystemExit(f"{path}: not a fan-noise-sweep steps CSV (no steps)")
    steps = []
    for r in rows:
        try:
            flags = [f"busy {r['busy']}" if r.get("busy") else "",
                     f"stalled {r['stalled']}" if r.get("stalled") else ""]
            steps.append({"n": int(r["step"]), "label": r["label"], "zone": r["zone"],
                          "duty": float(r["duty_pct"]), "t_start": float(r["t_start_s"]),
                          "t_measure": float(r["t_measure_s"]), "t_end": float(r["t_end_s"]),
                          "set": {z: float(r[f"{z}_set_pct"]) for z in zones},
                          "rpm": {z: float(r[f"{z}_rpm"]) for z in zones if r.get(f"{z}_rpm")},
                          "flags": ", ".join(x for x in flags if x)})
        except (KeyError, TypeError, ValueError) as e:
            raise SystemExit(f"{path}: bad step row ({e!r})")
    return meta, steps, zones


def find_offset(rec, steps, zones):
    """Where t=0 of the sweep falls in the recording: the shift that best lines up the
    level (dB) with the duty schedule (fan sound power ~ speed^5). Returns (r, offset)."""
    tc = (np.arange(len(rec.pa)) + 0.5) * rec.frame_len / rec.fs
    ok = ~rec.silent & (rec.pa > 0)
    la = 10 * np.log10(np.where(ok, rec.pa, 1.0))
    t_first = steps[0]["t_start"]
    ends = np.array([s["t_start"] for s in steps[1:]] + [steps[-1]["t_end"]])
    vals = np.array([10 * math.log10(max(1e-12, sum((s["set"][z] / 100) ** 5 for z in zones)))
                     for s in steps])
    span = ends[-1] - t_first
    if np.ptp(vals) == 0 or span <= 0:
        return None, 0.0
    best = (-2.0, 0.0)
    for off in np.arange(-10.0, max(0.0, rec.duration - 0.8 * span) + 0.25, 0.25):
        t = tc - off
        m = ok & (t >= t_first) & (t < ends[-1])
        if m.sum() * FRAME_SEC < 0.8 * span:
            continue
        e, a = vals[np.searchsorted(ends, t[m], side="right")], la[m]
        if e.std() == 0 or a.std() == 0:
            continue
        r = float(np.corrcoef(a, e)[0, 1])
        if r > best[0]:
            best = (r, float(off))
    return (None, 0.0) if best[0] < -1 else best


def sweep_mode(args, rooms, room, cal, units):
    meta, steps, zones = read_steps(args.steps)
    label, path = split_label(args.files[0])
    rec = Recording(path, args.channel)
    print(f"Sweep: {os.path.basename(args.steps)}  ({len(steps)} steps; {meta[0] if meta else ''})")
    aborted = [m for m in meta if m.startswith("ABORTED")]
    for m in aborted:
        print(f"  NOTE: the sweep stopped early: {m}")
    if args.offset == "auto":
        r, off = find_offset(rec, steps, zones)
        if r is None or r < MIN_OFFSET_R:
            raise SystemExit(f"Can't find the sweep in {path} (best match r={fmt(r) or 'none'}): the "
                             f"fans may be drowned by the room. Pass --offset SECONDS: how far into "
                             f"the recording you pressed Enter.")
        print(f"Recording: {path}  ({rec.duration:.0f} s; sweep t=0 found at {off:.2f} s, r={r:.2f})")
    else:
        try:
            off = float(args.offset)
        except ValueError:
            raise SystemExit(f"--offset: seconds or 'auto' (got {args.offset!r})")
        print(f"Recording: {path}  ({rec.duration:.0f} s; sweep t=0 at {off:.2f} s, given)")
    for n in rec.notes:
        print(f"  NOTE: {n}")
    print_room(rooms, room, units)

    results = []
    room_freqs = room_tone_freqs(room)
    rec_notes = [n for n in rec.notes if not n.startswith("channel ")]    # so the CSV keeps them
    for s in steps:
        w0, w1 = s["t_measure"] + off + 1.0, s["t_end"] + off - 2.0    # 1 s more settle, 2 s spare
        c0, c1 = max(w0, 0.0), min(w1, rec.duration)
        if c1 - c0 < 1.0:
            r = empty_result(s["label"], "step", path, cal)
            r.notes.append("not in the recording")
        else:
            r = window_result(rec, s["label"], "step", c0, c1, cal)
            r.notes = rec_notes + r.notes
            if (c0, c1) != (w0, w1):
                r.notes.append("partly outside the recording")
        r.extra = {"n": s["n"], "zone": s["zone"], "duty": f"{s['duty']:g}", "flags": s["flags"],
                   "rpm": " ".join(f"{z}={s['rpm'][z]:.0f}" for z in zones if z in s["rpm"]),
                   "rpm_col": "/".join(f"{s['rpm'][z]:.0f}" if z in s["rpm"] else "-" for z in zones)}
        if s["flags"]:
            r.notes.insert(0, s["flags"] + ": not clean")
        tag_tones(r.tones, line_hz(r), room_freqs,
                  [(z, rpm) for z, rpm in s["rpm"].items() if rpm > 0])
        results.append(r)
    lead_in = next((r for r in results if r.extra["zone"] == "lead-in"), None)
    lead_out = next((r for r in results if r.extra["zone"] == "lead-out"), None)
    finish(results, room, lead_in)
    for r in results:
        r.extra["ref"] = lead_in.label if lead_in else ""
    drift = None
    if lead_in and lead_out and lead_in.la90 is not None and lead_out.la90 is not None:
        drift = lead_out.la90 - lead_in.la90
        if abs(drift) > DRIFT_WARN:
            for r in results:       # it puts every step in doubt, not just the lead-out
                r.notes.append(f"sweep drift {drift:+.1f} dB (lead-out vs lead-in): the room or mic changed")
    if aborted and results:
        results[-1].notes.append(f"the sweep stopped after this step ({aborted[-1]})"
                                 + ("" if lead_out else ": no lead-out, so no drift check"))

    print()
    print_table(["#", "step", "rpm " + "/".join(zones), *LEVELS[:2], "above room", "machine",
                 "delta", "notes"],
                [[str(r.extra["n"]), r.label, r.extra["rpm_col"],
                  fmt(r.lv["LAeq"]), fmt(r.la90), fmt(r.above, True), fmt(r.machine), r.delta,
                  "; ".join(r.notes)] for r in results])
    if drift is not None:
        print(f"\nDrift check (lead-out minus lead-in, same duty): {drift:+.1f} dB"
              + (f"  -- over {DRIFT_WARN:g} dB: the room or the mic changed during the sweep; "
                 f"differences that small are uncertain" if abs(drift) > DRIFT_WARN else "  (fine)"))
    print_tones(results, room)
    legend(units, "the lead-in")

    lines = {}
    hold = lead_in.extra["duty"] if lead_in else None
    for z in zones + ["all"]:
        pts = [(float(r.extra["duty"]), r.la90) for r in results if r.extra["zone"] == z]
        if pts:
            if lead_in is not None:
                pts.append((float(hold), lead_in.la90))
            lines[f"{z} fan alone" if z != "all" else "all fans"] = pts
    return results, lines


# ---------------------------------------------------------------- whole files
def files_mode(args, rooms, room, cal, units):
    results = []
    room_freqs = room_tone_freqs(room)
    for spec in args.files:
        label, path = split_label(spec)
        if any(r.label == label for r in results):
            raise SystemExit(f"two recordings labelled {label!r}: use LABEL=FILE")
        rec = Recording(path, args.channel)
        t0, t1 = trimmed(rec, args.trim)
        r = window_result(rec, label, "file", t0, t1, cal)
        r.notes = rec.notes + r.notes
        tag_tones(r.tones, line_hz(r), room_freqs)
        results.append(r)
        del rec
    ref = results[0] if results else None
    if args.ref:
        ref = next((r for r in results if r.label == args.ref), None)
        if ref is None:
            raise SystemExit(f"--ref {args.ref}: no recording with that label "
                             f"({', '.join(r.label for r in results)})")
    finish(results, room, ref)
    for r in results:
        r.extra["ref"] = ref.label if ref else ""
    print_room(rooms, room, units)
    if results:
        print()
        print_table(["recording", "s", *LEVELS, "above room", "machine",
                     f"delta vs {ref.label}", "notes"],
                    [[r.label, f"{r.dur:.0f}", *(fmt(r.lv[k]) for k in LEVELS), fmt(r.above, True),
                      fmt(r.machine), r.delta, "; ".join(r.notes)] for r in results])
    print_tones(results, room)
    legend(units, ref.label if ref else "the reference")
    return results


# ---------------------------------------------------------------- compare
def read_results(path):
    try:
        with open(path, newline="") as f:
            rows = list(csv.DictReader(f))
    except OSError as e:
        raise SystemExit(f"{path}: {e.strerror}")
    if not rows or "LA90" not in rows[0]:
        raise SystemExit(f"{path}: not a fan-noise-analyze --csv file")
    out = {}
    for r in rows:
        if r["kind"] != "room-file":
            out[r["label"]] = r
    return out, {r["units"] for r in rows}


def compare_mode(a_path, b_path):
    (a, ua), (b, ub) = read_results(a_path), read_results(b_path)
    if ua != ub:
        raise SystemExit(f"different units ({'/'.join(ua)} vs {'/'.join(ub)}): compare like with like")

    def num(r, key):
        try:
            return float(r[key])
        except (KeyError, TypeError, ValueError):
            return None
    print(f"A = {a_path}\nB = {b_path}\ndelta = B - A (negative: B is quieter)")
    ra, rb = (num(a["room"], "LA90") if "room" in a else None), (num(b["room"], "LA90") if "room" in b else None)
    if ra is not None and rb is not None:
        print(f"Room LA90: A {ra:.1f}, B {rb:.1f} ({rb - ra:+.1f} dB)"
              + ("  -- the rooms differ: trust the machine columns, not the ~ ones"
                 if abs(rb - ra) > ROOM_SPREAD else ""))
    labels = [k for k in a if k in b and k != "room"]

    def tone(r):
        hz, tnr = num(r, "tone_hz"), num(r, "tone_tnr")
        if hz is None or tnr is None:
            return ""
        if r.get("tones"):          # the analysis' verdict: it knew the FFT lines
            prominent = "PROMINENT" in r["tones"].split("; ")[0].split("/")
        else:
            prominent = tnr >= tnr_criterion(hz)
        return f"{hz:.0f} Hz {tnr:+.1f}" + (" *" if prominent else "")

    def noted(r):
        """r's notes that matter (which channel was used doesn't)."""
        return [n for n in (r.get("notes") or "").split("; ") if n and not n.startswith("channel ")]
    rows = []
    for k in labels:
        la, lb = num(a[k], "LA90"), num(b[k], "LA90")
        ma, mb = num(a[k], "machine"), num(b[k], "machine")
        if ma is not None and mb is not None:
            d = f"{mb - ma:+.1f}"
        elif la is not None and lb is not None:
            d = f"~{lb - la:+.1f}"
        else:
            d = ""
        rows.append([k, fmt(la), fmt(lb), fmt(None if la is None or lb is None else lb - la, True),
                     fmt(ma), fmt(mb), d, tone(a[k]), tone(b[k]),
                     " ".join(s for s, r in (("A", a[k]), ("B", b[k])) if noted(r))])
    print()
    print_table(["recording", "A LA90", "B LA90", "LA90 delta", "A machine", "B machine",
                 "machine delta", "A top tone", "B top tone", "notes"], rows)
    for side, mine, other in (("A", a, b), ("B", b, a)):
        only = [k for k in mine if k not in other]
        if only:
            print(f"Only in {side}: {', '.join(only)}")
    flagged = [f"  {k}  {s}: {'; '.join(noted(d[k]))}" for k in ["room"] + labels
               for s, d in (("A", a), ("B", b)) if k in d and noted(d[k])]
    if flagged:
        print("\nNotes (those numbers may not be clean):\n" + "\n".join(flagged))
    print('\n"~" = plain LA90 difference (a side within 3 dB of its room): the real machine '
          "difference is at least that big if the rooms match.\ntop tone = the most prominent "
          "tone and its tone-to-noise ratio (dB); * = prominent by ECMA-418-1.")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*", metavar="[LABEL=]FILE",
                    help="recordings (label: the file name without extension)")
    ap.add_argument("--room", action="append", default=[], metavar="FILE",
                    help="the room with the machine off; repeat for before and after")
    ap.add_argument("--steps", metavar="CSV",
                    help="steps CSV of fan-noise-sweep.py: FILE is one recording of that sweep")
    ap.add_argument("--offset", default="auto",
                    help="sweep mode: seconds into the recording you pressed Enter (default: "
                         "found from the audio)")
    ap.add_argument("--trim", type=float, default=5.0,
                    help="seconds left out at each end of whole recordings (default 5)")
    ap.add_argument("--ref", metavar="LABEL", help="the recording delta compares to (default: the first)")
    ap.add_argument("--channel", type=int, metavar="N", help="channel to use, 1-based (default: the loudest)")
    ap.add_argument("--cal-offset", type=float, metavar="DB", help="report dB SPL: SPL = dBFS + DB")
    ap.add_argument("--csv", metavar="OUT", help="write the results as CSV (what --compare reads)")
    ap.add_argument("--plot", metavar="PNG", help="write spectra (and a sweep's LA90 by duty)")
    ap.add_argument("--compare", nargs=2, metavar=("A.csv", "B.csv"),
                    help="compare two --csv results (delta = B - A)")
    args = ap.parse_args(argv)

    if args.compare:
        if args.files or args.room or args.steps:
            raise SystemExit("--compare takes only the two CSV files")
        return compare_mode(*args.compare)
    if args.steps and len(args.files) != 1:
        raise SystemExit("--steps needs exactly one recording (the sweep)")
    if args.steps and args.ref:
        raise SystemExit("--ref is for whole recordings; a sweep compares to its lead-in")
    if not args.files and not args.room:
        ap.error("give recordings and/or --room")
    if args.trim < 0:
        raise SystemExit("--trim must be >= 0")
    cal = args.cal_offset or 0.0
    units = "dB SPL" if args.cal_offset is not None else "dBFS"

    rooms, room = load_rooms(args.room, args.channel, args.trim, cal)
    if room is not None:
        tag_tones(room.tones, line_hz(room))
    if args.steps:
        results, lines = sweep_mode(args, rooms, room, cal, units)
    else:
        results, lines = files_mode(args, rooms, room, cal, units), None
    everything = rooms + ([room] if room is not None else []) + results
    if args.csv:
        write_csv(args.csv, everything, units)
        print(f"\nCSV:  {args.csv}")
    if args.plot:
        plot(args.plot, results, room, units, lines)
        print(f"Plot: {args.plot}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
