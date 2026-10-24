"""One-shot, local speaker-to-microphone calibration using the real live stream."""

import asyncio
import logging
import time

import numpy as np

from app.echo import ClockedFrames, ReferenceHistory, measure_delay, pcm
from app.output import SpeechOutput

log = logging.getLogger(__name__)


def calibration_probe(level=0.15):
    """Three different voice-band sweeps, with quiet gaps and soft edges."""
    pieces = []
    rate = 24000
    duration = 0.5
    t = np.arange(round(rate * duration)) / rate
    envelope = np.sin(np.pi * t / duration) ** 2
    for index, (low, high) in enumerate(((450, 3200), (2900, 650), (800, 2800))):
        phase = 2 * np.pi * (low * t + (high - low) * t * t / (2 * duration))
        pieces.append(np.sin(phase) * envelope * level * 32767)
        if index < 2:
            pieces.append(np.zeros(round(0.2 * rate)))
    return pcm(np.concatenate(pieces))


class CalibrationSession:
    """Same device/microphone/output lifecycle as LiveSession; no OpenAI connection."""

    def __init__(self, settings, device, http, audio_server, token, store):
        self.s, self.device, self.store = settings, device, store
        self.closed = False
        self.failed = asyncio.Event()
        self.failure = ""
        self.clock = ClockedFrames()
        self.history = ReferenceHistory()
        self.reference_frames = []
        self.mic_frames = []
        self.output = SpeechOutput(
            settings,
            device,
            http,
            audio_server,
            self.speaking,
            self.listening,
            self.fail,
            token,
        )
        self.output.on_reference = self.reference

    def reference(self, frame, stamp):
        self.reference_frames.append(self.history.append(frame, stamp))
        if len(self.reference_frames) > 2250:
            self.failure = "Calibration exceeded its 45 second capture limit"
            self.failed.set()

    def receive_audio(self, audio):
        if self.closed or self.failed.is_set() or not self.device.mic_enabled:
            return
        for stamp, frame in self.clock.append(audio, time.monotonic()):
            self.mic_frames.append(
                (stamp, np.frombuffer(frame, dtype="<i2").astype(np.float64))
            )
        if len(self.mic_frames) > 2250:
            self.failure = "Calibration exceeded its 45 second capture limit"
            self.failed.set()

    async def speaking(self):
        await self.device.set_phase("replying")

    async def listening(self):
        await self.device.set_phase("listening")

    async def fail(self, message):
        self.failure = message
        self.failed.set()

    async def pause(self, seconds):
        if self.failed.is_set():
            raise RuntimeError(self.failure)
        try:
            await asyncio.wait_for(self.failed.wait(), seconds)
        except TimeoutError:
            return
        raise RuntimeError(self.failure)

    async def run(self):
        try:
            log.info(
                "Echo calibration armed: keep quiet; three short tones will play through %s. No audio is sent to OpenAI.",
                self.s.tts_media_player,
            )
            await self.device.mic(True)  # Calibration always captures through playback.
            async with asyncio.timeout(self.s.tts_stream_start_timeout_s + 1):
                while (
                    not self.output.encoder
                    or not self.output.encoder.resource.fetched.is_set()
                ):
                    await self.pause(0.02)
            await self.pause(0.3)
            probe = calibration_probe(self.s.aec_calibration_level)
            began = time.monotonic()
            for index, offset in enumerate(range(0, len(probe), 960)):
                await self.output.write(probe[offset : offset + 960])
                await self.pause(
                    max(0.001, began + (index + 1) * 0.02 - time.monotonic())
                )
            # Let the last probe leave both the PCM queue and the physical
            # speaker buffer before measuring, including the longest searched lag.
            await self.pause(self.s.aec_calibration_max_delay_ms / 1000 + 0.5)
            await self.device.mic(False)
            await self.output.close()
            if self.failed.is_set():
                raise RuntimeError(self.failure)
            result = await asyncio.to_thread(
                measure_delay,
                tuple(self.reference_frames),
                tuple(self.mic_frames),
                self.s.aec_calibration_max_delay_ms,
                self.s.aec_calibration_min_confidence,
            )
            if self.closed:
                return  # Stop/new wake cannot persist a stale measurement.
            self.store.save(self.s, self.device, result)
            log.info(
                "Echo calibration SAVED: delay=%.2f ms, confidence=%.3f, peak_ratio=%.2f, method=%s, waveform=%.3f, spectral=%.3f, sweep_min=%.3f, spread=%.0f ms, mic_rms=%.0f, mic_peak=%.0f. aec_delay_ms: -1 uses this on the next conversation.",
                result["delay_ms"],
                result["confidence"],
                result["peak_ratio"],
                result["method"],
                result["waveform_confidence"],
                result["spectral_confidence"],
                result["sweep_min_confidence"],
                result["delay_spread_ms"],
                result["mic_rms"],
                result["mic_peak"],
            )
            if self.s.aec_delay_ms >= 0:
                log.info(
                    "Manual aec_delay_ms remains %.1f; set -1 to use the measured delay, or copy %.2f.",
                    self.s.aec_delay_ms,
                    result["delay_ms"],
                )
            log.info(
                "Next wake starts a normal conversation. Turn aec_calibrate_on_next_wake off before future add-on restarts unless another test is wanted."
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Echo calibration NOT saved: %s", exc)
            await self.device.send(
                {
                    "type": "error",
                    "message": "Echo calibration failed; see add-on log. Previous calibration is unchanged.",
                }
            )
        finally:
            await self.close()

    async def close(self):
        if self.closed:
            return
        self.closed = True
        await self.output.close()
        await self.device.end()
        self.reference_frames.clear()
        self.mic_frames.clear()
        self.history.frames.clear()
        self.clock.data.clear()
