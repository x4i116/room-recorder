#!/usr/bin/env python3
"""
room_recorder.py - record from your PC's microphone, 24/7 or voice-activated.

Setup (once):
    pip install sounddevice soundfile numpy matplotlib
    (Linux only: sudo apt install libportaudio2 libsndfile1)

Examples:
    python room_recorder.py --list-devices          # find your mic's number
    python room_recorder.py --meter                 # watch live levels to tune the threshold
    python room_recorder.py                         # voice-activated, auto-calibrated threshold
    python room_recorder.py --mode continuous       # record 24/7 in 30-minute files
    python room_recorder.py --device 2 --max-gb 10  # pick a mic, cap storage at 10 GB

    python room_recorder.py --make-spectrograms     # draw images for older recordings
    python room_recorder.py --denoise high --boost 12   # crank the mic, strip the hum

Recordings go to ./recordings/YYYY-MM-DD/HH-MM-SS_<mode>.flac
Each one gets a matching .jpg (waveform + spectrogram) unless --no-spectrograms.
Press Ctrl+C to stop; the current file is closed cleanly.
"""

import argparse
import collections
import datetime as dt
import queue
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf

BLOCK_SEC = 0.1  # audio is processed in 100 ms blocks


def log(msg):
    print(f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def dbfs(block):
    """Loudness of a block in dBFS (0 = max, quiet room is usually -60 to -45)."""
    rms = np.sqrt(np.mean(np.square(block, dtype=np.float64)))
    return 20 * np.log10(max(rms, 1e-10))


class ClipWriter:
    def __init__(self, outdir, rate, fmt, label):
        now = dt.datetime.now()
        day_dir = outdir / now.strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        self.path = day_dir / f"{now:%H-%M-%S}_{label}.{fmt}"
        self.file = sf.SoundFile(self.path, "w", samplerate=rate, channels=1,
                                 subtype="PCM_16", format=fmt.upper())
        self.started = time.monotonic()
        self.frames = 0

    def write(self, block):
        self.file.write(block)
        self.frames += len(block)

    def close(self):
        self.file.close()
        return self.frames


AUDIO_EXTS = (".flac", ".wav")
_spec_pool = None  # one background worker draws spectrogram images


def _file_size(p):
    try:
        return p.stat().st_size
    except OSError:
        return 0


def enforce_storage_limit(outdir, max_bytes):
    """Delete the oldest recordings (and their images) until under max_bytes."""
    files = sorted((p for p in outdir.rglob("*") if p.suffix in AUDIO_EXTS),
                   key=lambda p: p.stat().st_mtime)
    sizes = {p: _file_size(p) + _file_size(p.with_suffix(".jpg")) for p in files}
    total = sum(sizes.values())
    while len(files) > 1 and total > max_bytes:
        oldest = files.pop(0)
        total -= sizes[oldest]
        oldest.unlink(missing_ok=True)
        oldest.with_suffix(".jpg").unlink(missing_ok=True)
        log(f"Storage limit reached - deleted {oldest.name}")
        try:
            oldest.parent.rmdir()  # removes the day folder only if now empty
        except OSError:
            pass


def save_spectrogram(audio_path):
    """Save <clip>.jpg next to a recording: waveform on top, spectrogram below."""
    from matplotlib.figure import Figure
    from matplotlib.ticker import FuncFormatter

    data, rate = sf.read(audio_path, dtype="float32")
    if data.ndim > 1:
        data = data.mean(axis=1)
    n = len(data)
    win = 1024 if rate > 24000 else 512
    if n < win * 2:
        return
    dur = n / rate

    # Spectrogram: at most ~3000 columns so long 24/7 files stay fast and small
    hop = max(win // 4, -(-(n - win) // 3000))
    count = 1 + (n - win) // hop
    idx = np.arange(win)[None, :] + hop * np.arange(count)[:, None]
    window = np.hanning(win).astype(np.float32)
    spec_db = 20 * np.log10(np.abs(np.fft.rfft(data[idx] * window, axis=1)).T + 1e-10)
    vmax = float(np.percentile(spec_db, 99.5))

    # Waveform envelope (min/max per bin)
    bins = min(2000, n)
    chunks = data[: n // bins * bins].reshape(bins, -1)
    t_env = np.linspace(0, dur, bins)

    bg, fg = "#121212", "#d0d0d0"
    fig = Figure(figsize=(14, 5.5), facecolor=bg)
    ax_w, ax_s = fig.subplots(2, 1, sharex=True,
                              gridspec_kw={"height_ratios": [1, 3], "hspace": 0.06})
    ax_w.fill_between(t_env, chunks.min(axis=1), chunks.max(axis=1),
                      color="#4fc3f7", linewidth=0)
    peak = max(float(np.abs(data).max()), 1e-3)
    ax_w.set_ylim(-peak, peak)
    ax_w.set_yticks([])
    ax_s.imshow(spec_db, origin="lower", aspect="auto", cmap="magma",
                vmin=vmax - 70, vmax=vmax, extent=(0, dur, 0, rate / 2000))
    ax_s.set_ylabel("Frequency (kHz)", color=fg)
    ax_s.set_xlabel("Time (min:sec)", color=fg)
    ax_s.xaxis.set_major_formatter(
        FuncFormatter(lambda s, _: f"{int(s // 60)}:{int(s % 60):02d}"))
    ax_s.set_xlim(0, dur)
    for ax in (ax_w, ax_s):
        ax.set_facecolor(bg)
        ax.tick_params(colors=fg)
        for spine in ax.spines.values():
            spine.set_color("#444")

    clock, _, label = audio_path.stem.partition("_")
    fig.suptitle(f"{audio_path.parent.name}   {clock.replace('-', ':')}   "
                 f"{label}   {dur:.1f} s", color=fg, x=0.01, ha="left", fontsize=12)
    fig.savefig(audio_path.with_suffix(".jpg"), dpi=100, facecolor=bg,
                bbox_inches="tight", pil_kwargs={"quality": 85})


def _safe_spectrogram(path):
    try:
        save_spectrogram(path)
    except Exception as e:
        log(f"Couldn't draw spectrogram for {path.name}: {e}")


def queue_spectrogram(path, args):
    """Draw the image in the background so recording never pauses."""
    global _spec_pool
    if not args.spectrograms:
        return
    if _spec_pool is None:
        _spec_pool = ThreadPoolExecutor(max_workers=1)
    _spec_pool.submit(_safe_spectrogram, path)


def make_missing_spectrograms(outdir):
    files = sorted(p for p in outdir.rglob("*")
                   if p.suffix in AUDIO_EXTS and not p.with_suffix(".jpg").exists())
    if not files:
        log("Every recording already has a spectrogram image.")
        return
    for i, p in enumerate(files, 1):
        log(f"[{i}/{len(files)}] {p.relative_to(outdir)}")
        _safe_spectrogram(p)
    log("Done.")


def audio_blocks(args):
    """Yield mono float32 blocks from the microphone forever."""
    q = queue.Queue()

    def callback(indata, frames, time_info, status):
        if status:
            log(f"Audio warning: {status}")
        q.put(indata[:, 0].copy())

    with sd.InputStream(device=args.device, channels=1, samplerate=args.rate,
                        blocksize=int(args.rate * BLOCK_SEC), dtype="float32",
                        callback=callback):
        while True:
            yield q.get()


# Noise reduction strength: (over-subtraction, lowest gain a frequency can drop to)
DENOISE_PRESETS = {
    "off": (0.0, 1.0),
    "low": (1.5, 0.25),
    "medium": (2.5, 0.10),
    "high": (4.0, 0.04),
}


class SoundCleaner:
    """Streaming noise reduction and background-noise tracker.

    Learns the steady background (hard drive hum, fans, PC whine, HVAC) separately
    for every frequency and keeps adapting, so it follows the room as it changes.
    Each block comes back cleaned, along with how far the voice range (250-4000 Hz)
    rose above the learned background - that number is what triggers recording.
    """

    WARMUP_FRAMES = 100  # first second: plain average to get a starting estimate

    def __init__(self, rate, denoise="medium", highpass=100, boost_db=0.0):
        self.hop = rate // 100              # 10 ms steps
        self.win = self.hop * 4             # 40 ms frames, 75 % overlap
        self.window = np.sqrt(np.hanning(self.win + 1)[:-1]).astype(np.float32)
        freqs = np.fft.rfftfreq(self.win, 1 / rate)
        self.band = (freqs >= 250) & (freqs <= 4000)
        self.low_cut = freqs < highpass
        self.over, self.floor = DENOISE_PRESETS[denoise]
        self.boost = 10 ** (boost_db / 20)
        self.history = np.zeros(self.win - self.hop, np.float32)
        self.ola = np.zeros(self.win, np.float32)
        self.noise = np.zeros(len(freqs))
        self.gain = np.ones(len(freqs))
        self.frames = 0

    def _track_noise(self, power):
        self.frames += 1
        if self.frames <= self.WARMUP_FRAMES:
            self.noise += (power - self.noise) / self.frames
            return
        # Bins that look like background follow quickly (~1 s). Bins far above it are
        # probably a voice or event, so they only creep in (~50 s) - long enough that
        # talking isn't "learned", short enough that a new fan eventually is.
        background = power < 4 * self.noise
        rate = np.where(background, 0.01, 0.0002)
        self.noise += rate * (power - self.noise)

    def process(self, block):
        x = np.concatenate([self.history, block])
        out = np.empty(len(block), np.float32)
        sig = noi = 0.0
        for i in range(len(block) // self.hop):
            spec = np.fft.rfft(x[i * self.hop: i * self.hop + self.win] * self.window)
            power = spec.real ** 2 + spec.imag ** 2 + 1e-12
            if self.frames >= self.WARMUP_FRAMES:
                sig += power[self.band].sum()
                noi += self.noise[self.band].sum()
            self._track_noise(power)

            if self.over:
                g = np.clip(1 - self.over * self.noise / power, self.floor, 1.0)
                g = np.convolve(g, [0.25, 0.5, 0.25], mode="same")  # smooth across freq
                self.gain = 0.5 * self.gain + 0.5 * g                 # and across time
            g = self.gain.copy()
            g[self.low_cut] = 0.0

            frame = np.fft.irfft(spec * g, self.win).astype(np.float32) * self.window
            self.ola += frame
            out[i * self.hop:(i + 1) * self.hop] = self.ola[:self.hop] / 2
            self.ola[:-self.hop] = self.ola[self.hop:]
            self.ola[-self.hop:] = 0.0
        self.history = x[-(self.win - self.hop):]
        above = 10 * np.log10(sig / noi) if noi > 0 else 0.0
        return self._limit(out * self.boost), above

    @staticmethod
    def _limit(x):
        """Soft-limit peaks above half scale so boosted audio never clips harshly."""
        a = np.abs(x)
        loud = a > 0.5
        if loud.any():
            x = x.copy()
            x[loud] = np.sign(x[loud]) * (0.5 + 0.5 * np.tanh((a[loud] - 0.5) / 0.5))
        return x


def clean_blocks(blocks, cleaner):
    for block in blocks:
        yield cleaner.process(block)


def warm_up(cleaned, seconds):
    log(f"Learning the room's background noise for {seconds:g} s - keep it quiet...")
    for _ in range(int(seconds / BLOCK_SEC)):
        next(cleaned)
    log("Got it. Noise reduction keeps adapting as the room changes.")


def run_meter(cleaned):
    log("Live meter (Ctrl+C to stop). Talk, clap, stay quiet - note the numbers.")
    log("'Above noise' is what sensitivity compares against: High 5, Normal 8, Low 12 dB.")
    while True:
        got = [next(cleaned) for _ in range(5)]  # ~0.5 s
        lvl = np.mean([dbfs(b) for b, _ in got])
        above = max(a for _, a in got)
        bar = "#" * max(0, min(40, int(above)))
        print(f"\r  level {lvl:6.1f} dBFS   above noise {above:5.1f} dB |{bar:<40}|",
              end="", flush=True)


def run_continuous(cleaned, args, outdir, max_bytes):
    chunk_sec = args.chunk_minutes * 60
    writer = None
    try:
        for block, _ in cleaned:
            if writer is None:
                writer = ClipWriter(outdir, args.rate, args.format, "continuous")
                log(f"Recording -> {writer.path}")
            writer.write(block)
            if time.monotonic() - writer.started >= chunk_sec:
                writer.close()
                log(f"Saved {writer.path.name}")
                queue_spectrogram(writer.path, args)
                writer = None
                enforce_storage_limit(outdir, max_bytes)
    finally:
        if writer:
            writer.close()
            log(f"Saved {writer.path}")
            queue_spectrogram(writer.path, args)


def run_voice(cleaned, args, outdir, max_bytes):
    if args.threshold is None:
        log(f"Trigger: sounds {args.margin:g} dB above the room's background noise")
    else:
        log(f"Trigger: fixed level {args.threshold:g} dBFS")

    preroll = collections.deque(maxlen=max(1, int(args.preroll / BLOCK_SEC)))
    writer = None
    last_loud = 0.0
    loud_time = 0.0
    log("Listening... (Ctrl+C to stop)")

    def finish():
        nonlocal writer
        dur = writer.close() / args.rate
        if loud_time < args.min_loud:
            writer.path.unlink(missing_ok=True)  # just a click/bump - discard
        else:
            log(f"Saved {writer.path.name} ({dur:.1f} s)")
            queue_spectrogram(writer.path, args)
            enforce_storage_limit(outdir, max_bytes)
        writer = None

    try:
        for block, above in cleaned:
            now = time.monotonic()
            if args.threshold is None:
                loud = above > args.margin
            else:
                loud = dbfs(block) > args.threshold

            if writer is None:
                preroll.append(block)
                if loud:
                    writer = ClipWriter(outdir, args.rate, args.format, "voice")
                    for b in preroll:  # include the second before the trigger
                        writer.write(b)
                    preroll.clear()
                    last_loud, loud_time = now, BLOCK_SEC
                    log("Sound detected - recording")
                continue

            writer.write(block)
            if loud:
                last_loud = now
                loud_time += BLOCK_SEC
            quiet_too_long = now - last_loud > args.hang
            too_long = now - writer.started > args.max_clip_minutes * 60
            if quiet_too_long or too_long:
                finish()
    finally:
        if writer:
            finish()


def main():
    p = argparse.ArgumentParser(description="Record your room from a PC microphone.")
    p.add_argument("--mode", choices=["voice", "continuous"], default="voice")
    p.add_argument("--out", default="recordings", help="output folder (default: ./recordings)")
    p.add_argument("--device", type=int, default=None, help="mic number from --list-devices")
    p.add_argument("--list-devices", action="store_true", help="show audio devices and exit")
    p.add_argument("--meter", action="store_true", help="show live levels for threshold tuning")
    p.add_argument("--rate", type=int, default=16000, help="sample rate (16000 is fine for voice)")
    p.add_argument("--format", choices=["flac", "wav"], default="flac",
                   help="flac is lossless and ~half the size of wav")
    p.add_argument("--max-gb", type=float, default=20, help="delete oldest files above this size")
    p.add_argument("--no-spectrograms", action="store_true",
                   help="don't save a waveform/spectrogram .jpg with each recording")
    p.add_argument("--make-spectrograms", action="store_true",
                   help="draw images for existing recordings that don't have one, then exit")
    p.add_argument("--allow-sleep", action="store_true",
                   help="Windows: don't stop the PC from sleeping while recording")
    # sound cleanup
    p.add_argument("--denoise", choices=list(DENOISE_PRESETS), default="medium",
                   help="remove steady background noise (hard drive, fans, hum)")
    p.add_argument("--highpass", type=float, default=100,
                   help="cut rumble below this many Hz (0 = off)")
    p.add_argument("--boost", type=float, default=0,
                   help="dB of extra volume on saved audio, soft-limited (e.g. 12)")
    # continuous mode
    p.add_argument("--chunk-minutes", type=float, default=30, help="file length in continuous mode")
    # voice mode
    p.add_argument("--margin", type=float, default=8,
                   help="trigger when the voice range is this many dB above background noise")
    p.add_argument("--threshold", type=float, default=None,
                   help="instead trigger at a fixed level in dBFS shown by --meter, e.g. -40")
    p.add_argument("--calibrate-sec", type=float, default=3,
                   help="seconds spent learning the background noise at startup")
    p.add_argument("--hang", type=float, default=5, help="seconds of quiet before a clip ends")
    p.add_argument("--preroll", type=float, default=1.0, help="seconds kept from before the trigger")
    p.add_argument("--min-loud", type=float, default=0.3,
                   help="discard clips with less than this many seconds of sound")
    p.add_argument("--max-clip-minutes", type=float, default=10,
                   help="split very long clips (e.g. a TV left on)")
    args = p.parse_args()

    if args.list_devices:
        print(sd.query_devices())
        print(f"\nDefault input device: {sd.default.device[0]}")
        return

    outdir = Path(args.out).resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    max_bytes = int(args.max_gb * 1024 ** 3)

    args.spectrograms = not args.no_spectrograms
    if args.spectrograms or args.make_spectrograms:
        try:
            import matplotlib  # noqa: F401
        except ImportError:
            log("Spectrogram images need matplotlib (pip install matplotlib) - skipping them.")
            args.spectrograms = False
            if args.make_spectrograms:
                return
    if args.make_spectrograms:
        try:
            make_missing_spectrograms(outdir)
        except KeyboardInterrupt:
            print()
            log("Stopped.")
        return

    try:
        cleaner = SoundCleaner(args.rate, args.denoise, args.highpass, args.boost)
        cleaned = clean_blocks(audio_blocks(args), cleaner)
        warm_up(cleaned, max(args.calibrate_sec, 1.5))
        if args.meter:
            run_meter(cleaned)
        if sys.platform == "win32" and not args.allow_sleep:
            import ctypes  # ES_CONTINUOUS | ES_SYSTEM_REQUIRED: block sleep while running
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)
            log("Keeping the PC awake while recording (screen can still turn off)")
        log(f"Mode: {args.mode} | noise reduction: {args.denoise} | boost: {args.boost:g} dB"
            f" | saving to {outdir} | cap {args.max_gb:g} GB")
        if args.mode == "continuous":
            run_continuous(cleaned, args, outdir, max_bytes)
        else:
            run_voice(cleaned, args, outdir, max_bytes)
    except KeyboardInterrupt:
        print()
        log("Stopped.")
    except sd.PortAudioError as e:
        sys.exit(f"Microphone error: {e}\nTry --list-devices and pass --device <number>.")
    finally:
        if _spec_pool:
            log("Finishing spectrogram images...")
            _spec_pool.shutdown(wait=True)


if __name__ == "__main__":
    main()
