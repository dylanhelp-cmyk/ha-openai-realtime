"""Reference-aligned SpeexDSP AEC, bounded audio clocks and delay measurement."""

import audioop
import ctypes as ct
import ctypes.util
import json
import logging
import math
import os
import time
from collections import deque
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile

import numpy as np

log = logging.getLogger(__name__)
RATE = 16000
SAMPLES = 320
FRAME_BYTES = SAMPLES * 2


def pcm(samples):
    return np.clip(samples, -32768, 32767).astype("<i2").tobytes()


class ClockedFrames:
    """Frame PCM16 capture without treating every network arrival as a new clock."""

    def __init__(self):
        self.data = bytearray()
        self.next_time = None
        self.resets = 0

    def append(self, data, received_at):
        duration = (len(self.data) + len(data)) / (RATE * 2)
        if self.next_time is None:
            self.next_time = received_at - duration
        else:
            error = received_at - (self.next_time + duration)
            if abs(error) > 0.25:
                self.data.clear()
                self.next_time = received_at - len(data) / (RATE * 2)
                self.resets += 1
            else:
                # Follow slow clock drift, not individual Wi-Fi packet jitter.
                self.next_time += float(np.clip(error * 0.005, -0.00005, 0.00005))
        self.data.extend(data)
        frames = []
        while len(self.data) >= FRAME_BYTES:
            frames.append((self.next_time, bytes(self.data[:FRAME_BYTES])))
            del self.data[:FRAME_BYTES]
            self.next_time += SAMPLES / RATE
        return frames


class ReferenceHistory:
    def __init__(self, seconds=12):
        self.seconds = seconds
        self.frames = deque()
        self.resampler = None
        self.next_time = None

    def append(self, frame, stamp):
        converted, self.resampler = audioop.ratecv(
            frame, 2, 1, 24000, RATE, self.resampler
        )
        samples = np.frombuffer(converted, dtype="<i2").astype(np.float64)
        if self.next_time is None or abs(stamp - self.next_time) > 0.1:
            self.next_time = stamp
        else:
            self.next_time += float(
                np.clip((stamp - self.next_time) * 0.005, -0.00005, 0.00005)
            )
        stamp = self.next_time
        self.next_time += len(samples) / RATE
        self.frames.append((stamp, samples))
        while self.frames and self.frames[0][0] < stamp - self.seconds:
            self.frames.popleft()
        return stamp, samples

    def read(self, start, count=SAMPLES, rate=RATE):
        wanted = start + np.arange(count) / rate
        result = np.zeros(count, dtype=np.float64)
        if not count:
            return result
        for stamp, samples in reversed(self.frames):
            if stamp + len(samples) / RATE < wanted[0]:
                break
            if stamp > wanted[-1]:
                continue
            selected = (wanted >= stamp) & (wanted < stamp + len(samples) / RATE)
            result[selected] = np.interp(
                (wanted[selected] - stamp) * RATE, np.arange(len(samples)), samples
            )
        return result


class SpeexEcho:
    """Small ctypes binding to the maintained distribution libspeexdsp ABI."""

    def __init__(self, filter_ms=256, suppress_db=-25, active_db=-8):
        library = os.environ.get("SPEEXDSP_LIBRARY") or ctypes.util.find_library(
            "speexdsp"
        )
        if not library:
            raise RuntimeError("libspeexdsp is required for echo cancellation")
        self.lib = ct.CDLL(library)
        ptr, integer = ct.c_void_p, ct.c_int
        audio = ct.POINTER(ct.c_int16)
        functions = {
            "speex_echo_state_init": ([integer, integer], ptr),
            "speex_echo_state_destroy": ([ptr], None),
            "speex_echo_state_reset": ([ptr], None),
            "speex_echo_ctl": ([ptr, integer, ptr], integer),
            "speex_echo_cancellation": ([ptr, audio, audio, audio], None),
            "speex_preprocess_state_init": ([integer, integer], ptr),
            "speex_preprocess_state_destroy": ([ptr], None),
            "speex_preprocess_ctl": ([ptr, integer, ptr], integer),
            "speex_preprocess_run": ([ptr, audio], integer),
        }
        for name, (args, result) in functions.items():
            function = getattr(self.lib, name)
            function.argtypes, function.restype = args, result
        self.echo = self.lib.speex_echo_state_init(
            SAMPLES, math.ceil(filter_ms / 20) * SAMPLES
        )
        self.preprocess = self.lib.speex_preprocess_state_init(SAMPLES, RATE)
        if not self.echo or not self.preprocess:
            self.close()
            raise RuntimeError("Cannot allocate SpeexDSP echo state")
        try:
            self._ctl(self.lib.speex_echo_ctl, self.echo, 24, RATE)
            for request, value in ((0, 0), (2, 0), (20, suppress_db), (22, active_db)):
                self._ctl(
                    self.lib.speex_preprocess_ctl, self.preprocess, request, value
                )
            if self.lib.speex_preprocess_ctl(self.preprocess, 24, self.echo):
                raise RuntimeError("Cannot attach SpeexDSP echo reference")
        except Exception:
            self.close()
            raise
        self.audio_type = ct.c_int16 * SAMPLES

    @staticmethod
    def _ctl(function, state, request, value):
        data = ct.c_int(value)
        if function(state, request, ct.byref(data)):
            raise RuntimeError("SpeexDSP rejected a processing setting")

    def process(self, microphone, reference):
        if len(microphone) != FRAME_BYTES or len(reference) != FRAME_BYTES:
            raise ValueError("AEC requires 20 ms mono PCM16 frames at 16 kHz")
        rec = self.audio_type.from_buffer_copy(microphone)
        ref = self.audio_type.from_buffer_copy(reference)
        out = self.audio_type()
        self.lib.speex_echo_cancellation(self.echo, rec, ref, out)
        self.lib.speex_preprocess_run(self.preprocess, out)
        return bytes(out)

    def reset(self):
        self.lib.speex_echo_state_reset(self.echo)

    def close(self):
        if getattr(self, "preprocess", None):
            self.lib.speex_preprocess_state_destroy(self.preprocess)
            self.preprocess = None
        if getattr(self, "echo", None):
            self.lib.speex_echo_state_destroy(self.echo)
            self.echo = None


def normalised_matches(pattern, signal):
    """Valid cross correlation, normalised per candidate window; FFT avoids O(n²)."""
    pattern = np.asarray(pattern, dtype=np.float64)
    signal = np.asarray(signal, dtype=np.float64)
    if len(signal) < len(pattern) or not len(pattern):
        return np.empty(0)
    pattern = pattern - np.mean(pattern)
    signal = signal - np.mean(signal)
    size = 1 << (len(pattern) + len(signal) - 1).bit_length()
    corr = np.fft.irfft(
        np.fft.rfft(signal, size) * np.conj(np.fft.rfft(pattern, size)), size
    )
    corr = corr[: len(signal) - len(pattern) + 1]
    sums = np.concatenate(([0.0], np.cumsum(signal * signal)))
    energy = sums[len(pattern) :] - sums[: -len(pattern)]
    denominator = np.sqrt(np.maximum(energy, 0) * np.dot(pattern, pattern))
    return np.divide(corr, denominator, out=np.zeros_like(corr), where=denominator > 1)


def _spectral_features(samples):
    """Phase-independent voice-band envelopes, centred every 4 ms.

    Independent clocks, speaker EQ and microphone processing can destroy a
    long chirp's sample correlation without destroying its frequency sweep.
    Per-band scaling limits EQ/gain differences; subtracting the noise floor
    prevents steady background sound from looking like the probe.
    """
    window, hop = 256, 64
    padded = np.pad(samples, (window // 2, window // 2))
    frames = np.lib.stride_tricks.sliding_window_view(padded, window)[::hop]
    power = np.abs(np.fft.rfft(frames * np.hanning(window), axis=1)) ** 2
    # 25 adjacent 125 Hz bands from 375 to 3500 Hz.
    bands = np.sqrt(power[:, 6:56].reshape(len(frames), 25, 2).sum(axis=2))
    bands = np.maximum(bands - np.percentile(bands, 20, axis=0), 0)
    scale = np.maximum(np.percentile(bands, 98, axis=0), np.max(bands) * 0.02)
    return np.minimum(bands / np.maximum(scale, 1), 2)


def _feature_matches(pattern, signal):
    """Normalised multiband envelope correlation at each candidate offset."""
    if len(signal) < len(pattern) or not len(pattern):
        return np.empty(0)
    pattern = pattern - pattern.mean(axis=0)
    size = 1 << (len(pattern) + len(signal) - 1).bit_length()
    cross = np.fft.rfft(signal, size, axis=0) * np.conj(
        np.fft.rfft(pattern, size, axis=0)
    )
    corr = np.fft.irfft(cross.sum(axis=1), size)[: len(signal) - len(pattern) + 1]
    sums = np.vstack((np.zeros(signal.shape[1]), signal.cumsum(axis=0)))
    energy = np.concatenate(([0.0], (signal * signal).sum(axis=1).cumsum()))
    variance = (
        energy[len(pattern) :]
        - energy[: -len(pattern)]
        - ((sums[len(pattern) :] - sums[: -len(pattern)]) ** 2).sum(axis=1)
        / len(pattern)
    )
    denominator = np.sqrt(np.maximum(variance, 0) * np.sum(pattern * pattern))
    return np.clip(
        np.divide(corr, denominator, out=np.zeros_like(corr), where=denominator > 1),
        -1,
        1,
    )


def _peak(matches, radius):
    best = int(np.argmax(matches))
    competing = matches.copy()
    competing[max(0, best - radius) : best + radius + 1] = 0
    ratio = float(matches[best]) / max(float(np.max(competing)), 1e-9)
    return best, float(matches[best]), ratio


def measure_delay(reference_frames, mic_frames, max_delay_ms=5000, min_confidence=0.35):
    """Estimate the physical round trip from the sent reference to captured audio."""
    if not reference_frames or not mic_frames:
        raise ValueError("No playback reference or microphone capture")
    start = min(reference_frames[0][0], mic_frames[0][0])
    end = max(reference_frames[-1][0] + 0.02, mic_frames[-1][0] + 0.02)
    count = math.ceil((end - start) * RATE) + SAMPLES
    if count > RATE * 45:
        raise ValueError("Calibration capture exceeded its bounded duration")
    reference = np.zeros(count)
    microphone = np.zeros(count)
    for frames, destination in (
        (reference_frames, reference),
        (mic_frames, microphone),
    ):
        for stamp, samples in frames:
            index = round((stamp - start) * RATE)
            length = min(len(samples), len(destination) - index)
            if index >= 0 and length > 0:
                destination[index : index + length] = samples[:length]
    peak = float(np.max(np.abs(reference)))
    audible = np.flatnonzero(np.abs(reference) > max(20, peak * 0.01))
    if len(audible) < RATE // 2:
        raise ValueError("The test reference was too short or quiet")
    left, right = int(audible[0]), int(audible[-1]) + 1
    probe = reference[left:right]
    # Search positive round-trip offsets only. Downsampling to 8 kHz retains
    # the voice-band probe and provides 0.125 ms delay resolution.
    decimation = 2
    signal = microphone[
        left : min(len(microphone), right + round(max_delay_ms * RATE / 1000))
    ]
    matches = np.abs(normalised_matches(probe[::decimation], signal[::decimation]))
    if not len(matches):
        raise ValueError("The test capture ended before an echo could return")
    best, waveform, waveform_ratio = _peak(matches, round(0.08 * RATE / decimation))
    waveform_delay = best * decimation * 1000 / RATE

    # Use the three-sweep spectral pattern for clock/phase robustness. Check
    # each third independently as well: a single tone, speech fragment or
    # inconsistent playback delay must not save a plausible-looking offset.
    pattern, capture = _spectral_features(probe), _spectral_features(signal)
    spectral_matches = _feature_matches(pattern, capture)
    limit = math.floor(max_delay_ms / 4) + 1
    spectral_matches = spectral_matches[:limit]
    if not len(spectral_matches):
        raise ValueError("The test capture ended before an echo could return")
    spectral_best, spectral, spectral_ratio = _peak(spectral_matches, 20)
    spectral_delay = spectral_best * 4.0
    sweeps, delays = [], []
    for indices in np.array_split(np.arange(len(pattern)), 3):
        first, last = int(indices[0]), int(indices[-1]) + 1
        candidates = _feature_matches(
            pattern[first:last], capture[first : len(capture) - len(pattern) + last]
        )[:limit]
        position = int(np.argmax(candidates))
        sweeps.append(float(candidates[position]))
        delays.append(position * 4.0)
    spread = max(delays + [spectral_delay]) - min(delays + [spectral_delay])

    # Retain sample-level precision where the waveform itself is reliable.
    # The spectral fallback has 4 ms resolution, within the default 20 ms
    # adaptive-filter lead. Its score has a different meaning from waveform NCC.
    precise = (
        waveform >= min_confidence
        and waveform_ratio >= 1.15
        and abs(waveform_delay - spectral_delay) <= 20
    )
    delay = waveform_delay if precise else spectral_delay
    confidence = waveform if precise else spectral
    ratio = waveform_ratio if precise else spectral_ratio
    matched = microphone[
        left + round(delay * RATE / 1000) : right + round(delay * RATE / 1000)
    ]
    clipped = float(np.mean(np.abs(matched) >= 32760))
    mic_rms = float(np.sqrt(np.mean(matched * matched)))
    mic_peak = float(np.max(np.abs(matched)))
    if (
        spectral < min_confidence
        or min(sweeps) < min_confidence
        or spectral_ratio < 1.15
        or spread > 16
        or clipped > 0.005
    ):
        raise ValueError(
            f"Unreliable echo measurement (correlation={waveform:.2f}, spectral={spectral:.2f}, "
            f"candidate_delay={spectral_delay:.0f} ms, peak_ratio={spectral_ratio:.2f}, "
            f"sweeps={'/'.join(f'{score:.2f}' for score in sweeps)}, spread={spread:.0f} ms, "
            f"mic_rms={mic_rms:.0f}, mic_peak={mic_peak:.0f}, clipped={clipped:.1%}); "
            "keep quiet; check mic levels and delay search range; use comfortable speaker volume"
        )
    return {
        "delay_ms": round(delay, 2),
        "confidence": round(confidence, 3),
        "peak_ratio": round(ratio, 2),
        "clipped_fraction": round(clipped, 5),
        "method": "waveform" if precise else "spectral",
        "waveform_confidence": round(waveform, 3),
        "spectral_confidence": round(spectral, 3),
        "sweep_min_confidence": round(min(sweeps), 3),
        "delay_spread_ms": round(spread, 2),
        "mic_rms": round(mic_rms, 1),
        "mic_peak": round(mic_peak, 1),
    }


class CalibrationStore:
    def __init__(self, path="/data/echo_calibration.json"):
        self.path = Path(path)

    @staticmethod
    def target(settings, device):
        if settings.tts_output == "airplay":
            return {
                "output": "airplay",
                "host": settings.airplay_host,
                "port": settings.airplay_port,
                "protocol": settings.airplay_protocol,
                "latency_ms": settings.airplay_latency_ms,
                "interface": settings.airplay_interface,
                "encryption": settings.airplay_encryption,
                "microphone": getattr(device, "identity", ""),
                "reference_version": 1,
                **(
                    {"render_lead_ms": settings.airplay_render_lead_ms}
                    if settings.airplay_render_lead_ms != 2000
                    else {}
                ),
            }
        # Preserve existing HTTP profile keys so upgrading keeps saved offsets.
        return {
            "player": settings.tts_media_player,
            "microphone": getattr(device, "identity", ""),
            "announce": settings.tts_announce,
            "prebuffer_ms": settings.tts_stream_prebuffer_ms,
            "reference_version": 1,
        }

    @classmethod
    def key(cls, settings, device):
        return sha256(
            json.dumps(cls.target(settings, device), sort_keys=True).encode()
        ).hexdigest()

    def _read(self):
        try:
            data = json.loads(self.path.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def load(self, settings, device):
        record = self._read().get(self.key(settings, device), {})
        if not isinstance(record, dict):
            return None
        delay = record.get("delay_ms")
        if (
            type(delay) not in (int, float)
            or not math.isfinite(delay)
            or not 0 <= delay <= 10000
        ):
            return None
        return float(delay)

    def save(self, settings, device, result):
        data = self._read()
        while len(data) >= 16:
            del data[next(iter(data))]
        data[self.key(settings, device)] = {
            **result,
            "target": self.target(settings, device),
            "measured_at": time.time(),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with NamedTemporaryFile("w", dir=self.path.parent, delete=False) as file:
                temporary = file.name
                json.dump(data, file, indent=2, allow_nan=False)
            os.replace(temporary, self.path)
        finally:
            if temporary and Path(temporary).exists():
                Path(temporary).unlink()


class EchoProcessor:
    def __init__(self, settings, device, store=None):
        self.s = settings
        self.history = ReferenceHistory()
        self.clock = ClockedFrames()
        self.raw = deque(maxlen=50)
        self.delay_ms = None
        self.target_delay_ms = None
        self.native = None
        self.last_track = 0
        self.last_stats = 0
        self.clock_resets = 0
        if settings.aec_enabled and settings.tts_output in {"media_player", "airplay"}:
            delay = settings.aec_delay_ms
            if delay < 0:
                delay = store.load(settings, device) if store else None
            if delay is not None:
                self.delay_ms = self.target_delay_ms = float(delay)
                self.native = SpeexEcho(
                    settings.aec_filter_ms,
                    settings.aec_suppression_db,
                    settings.aec_suppression_active_db,
                )
                log.info(
                    "Live AEC active: delay=%.1f ms, tail=%d ms, tracking=%s",
                    delay,
                    settings.aec_filter_ms,
                    settings.aec_auto_delay,
                )
            else:
                log.info(
                    "Live AEC awaiting calibration; microphone passes through until a delay is measured or set"
                )

    def reference(self, frame, stamp):
        if self.native:
            self.history.append(frame, stamp)

    def _track(self, stamp):
        if (
            not self.s.aec_auto_delay
            or stamp - self.last_track < 1
            or len(self.raw) < 40
        ):
            return
        self.last_track = stamp
        window = list(self.raw)[-10:]
        mic = np.concatenate([samples for _, samples in window])
        search = self.s.aec_delay_search_ms
        beginning = window[0][0] - (self.delay_ms + search) / 1000
        reference = self.history.read(
            beginning, len(mic) + round(2 * search * RATE / 1000)
        )
        # Each position describes a possible location of this mic window in
        # the reference. Only strongly correlated far-end audio updates delay.
        # A short, band-limited window tolerates independent clock skew without
        # comparing aliased 4 kHz samples or smearing a whole second of drift.
        kernel = np.ones(8) / 8
        matches = np.abs(
            normalised_matches(
                np.convolve(mic, kernel, mode="same"),
                np.convolve(reference, kernel, mode="same"),
            )
        )
        if not len(matches):
            return
        best = int(np.argmax(matches))
        if matches[best] < 0.65 or best == 0 or best == len(matches) - 1:
            return
        competing = matches.copy()
        radius = round(0.005 * RATE)
        competing[max(0, best - radius) : best + radius + 1] = 0
        if matches[best] < 1.2 * float(np.max(competing)):
            return  # A periodic tone cannot unambiguously locate the delay.
        measured = self.delay_ms + search - best * 1000 / RATE
        self.target_delay_ms += float(
            np.clip((measured - self.target_delay_ms) * 0.25, -10, 10)
        )
        self.target_delay_ms = float(np.clip(self.target_delay_ms, 0, 10000))

    def process(self, audio, received_at=None):
        if not self.native:
            return audio
        output = bytearray()
        for stamp, frame in self.clock.append(
            audio, time.monotonic() if received_at is None else received_at
        ):
            if self.clock.resets != self.clock_resets:
                self.native.reset()
                self.raw.clear()
                self.clock_resets = self.clock.resets
            raw = np.frombuffer(frame, dtype="<i2").astype(np.float64)
            self.raw.append((stamp, raw))
            self._track(stamp)
            # Smooth corrections compensate clock drift without jumping the
            # reference pointer by a whole packet during the user's speech.
            self.delay_ms += float(
                np.clip(self.target_delay_ms - self.delay_ms, -0.02, 0.02)
            )
            bulk_delay = max(0, self.delay_ms - self.s.aec_alignment_margin_ms)
            reference = self.history.read(stamp - bulk_delay / 1000)
            cleaned = self.native.process(frame, pcm(reference))
            output.extend(cleaned)
            if self.s.aec_log_stats and stamp - self.last_stats >= 5:
                self.last_stats = stamp
                raw_level = audioop.rms(frame, 2)
                cleaned_level = audioop.rms(cleaned, 2)
                log.info(
                    "AEC delay=%.1f ms; mic RMS before=%d after=%d (may include user speech)",
                    self.delay_ms,
                    raw_level,
                    cleaned_level,
                )
        return bytes(output)

    def close(self):
        if self.native:
            self.native.close()
            self.native = None
        self.history.frames.clear()
        self.raw.clear()
        self.clock.data.clear()
