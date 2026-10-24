"""Real SpeexDSP quality tests and signal-based calibration accuracy tests."""

import audioop
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from app.calibration import calibration_probe
from app.echo import (
    CalibrationStore,
    ClockedFrames,
    EchoProcessor,
    ReferenceHistory,
    SpeexEcho,
    measure_delay,
    normalised_matches,
    pcm,
)
from app.settings import Settings


def probe16():
    data, _ = audioop.ratecv(calibration_probe(0.2), 2, 1, 24000, 16000, None)
    return np.frombuffer(data, dtype="<i2").astype(float)


def records(data, start=100.0):
    return [
        (start + i / 16000, data[i : i + 320]) for i in range(0, len(data) - 319, 320)
    ]


def colour(noise):
    return np.convolve(noise, [0.2, 0.6, 0.2], mode="same")


class MeasurementTests(unittest.TestCase):
    def test_spectral_calibration_survives_clock_skew_that_breaks_waveform_match(self):
        reference = np.concatenate((np.zeros(16000), probe16(), np.zeros(96000)))
        positions = np.arange(len(reference))
        for ppm in (-500, 50, 100, 250, 500, 1000):
            with self.subTest(ppm=ppm):
                microphone = (
                    np.interp(
                        (positions - 12320) * (1 + ppm / 1e6),
                        positions,
                        reference,
                        left=0,
                        right=0,
                    )
                    * 0.5
                )
                microphone += np.random.default_rng(42).normal(0, 35, len(reference))
                result = measure_delay(records(reference), records(microphone))
                self.assertAlmostEqual(result["delay_ms"], 770, delta=4)
                self.assertGreater(result["spectral_confidence"], 0.8)
                self.assertLessEqual(result["delay_spread_ms"], 4)
                if ppm == 250:
                    self.assertLess(result["waveform_confidence"], 0.25)
                    self.assertEqual(result["method"], "spectral")

    def test_spectral_calibration_handles_dispersive_room_and_gain_changes(self):
        rng = np.random.default_rng(20)
        reference = np.concatenate((np.zeros(16000), probe16(), np.zeros(96000)))
        # A long, dispersive impulse response smears phase across frequencies.
        kernel = rng.normal(0, 1, 1280) * np.exp(-np.arange(1280) / 300)
        kernel /= np.linalg.norm(kernel)
        microphone = np.convolve(reference, kernel)[: len(reference)]
        microphone = np.concatenate((np.zeros(12320), microphone[:-12320]))
        # Dynamic microphone AGC, polarity reversal and a quiet room floor.
        microphone *= -0.4 * np.linspace(0.5, 1.5, len(reference))
        microphone += rng.normal(0, 20, len(reference))
        result = measure_delay(records(reference), records(microphone))
        self.assertAlmostEqual(result["delay_ms"], 770, delta=20)
        self.assertGreater(result["spectral_confidence"], 0.65)

    def test_frame_timestamp_corrections_do_not_destroy_calibration(self):
        reference = np.concatenate((np.zeros(16000), probe16(), np.zeros(96000)))
        microphone = np.concatenate((np.zeros(12320), reference[:-12320])) * 0.5
        frames = [
            (stamp + np.sin(index / 10) * 0.001, samples)
            for index, (stamp, samples) in enumerate(records(microphone))
        ]
        result = measure_delay(records(reference), frames)
        self.assertAlmostEqual(result["delay_ms"], 770, delta=8)
        self.assertGreater(result["spectral_confidence"], 0.7)

    def test_one_sweep_and_constant_tone_are_not_enough_to_calibrate(self):
        reference = np.concatenate((probe16(), np.zeros(96000)))
        one_sweep = np.zeros_like(reference)
        one_sweep[6400 : 6400 + 8000] = reference[:8000] * 0.5
        tone = np.sin(np.arange(len(reference)) * 2 * np.pi * 1000 / 16000) * 1000
        for microphone in (one_sweep, tone):
            with self.assertRaisesRegex(ValueError, "Unreliable"):
                measure_delay(records(reference), records(microphone))

    def test_changing_delay_between_sweeps_is_rejected(self):
        reference = np.concatenate((probe16(), np.zeros(96000)))
        microphone = np.zeros_like(reference)
        for index, delay in enumerate((6400, 8000, 9600)):
            first = index * 11200
            microphone[first + delay : first + delay + 8000] = (
                reference[first : first + 8000] * 0.5
            )
        with self.assertRaisesRegex(ValueError, r"spread=.*mic_rms=.*mic_peak="):
            measure_delay(records(reference), records(microphone))

    def test_two_equal_echo_paths_are_ambiguous(self):
        reference = np.concatenate((probe16(), np.zeros(96000)))
        microphone = (
            np.concatenate((np.zeros(6400), reference[:-6400])) * 0.4
            + np.concatenate((np.zeros(14400), reference[:-14400])) * 0.4
        )
        with self.assertRaisesRegex(ValueError, "Unreliable"):
            measure_delay(records(reference), records(microphone))

    def test_echo_outside_search_range_is_rejected(self):
        reference = np.concatenate((probe16(), np.zeros(96000)))
        microphone = np.concatenate((np.zeros(9600), reference[:-9600])) * 0.5
        with self.assertRaisesRegex(ValueError, "Unreliable"):
            measure_delay(records(reference), records(microphone), max_delay_ms=200)

    def test_known_delay_with_room_reflections_noise_and_different_gain(self):
        rng = np.random.default_rng(42)
        reference = np.concatenate((np.zeros(16000), probe16(), np.zeros(16000 * 6)))
        delay = 12320  # 770 ms, including playback and capture path.
        microphone = np.concatenate((np.zeros(delay), reference[:-delay])) * -0.55
        reflection = (
            np.concatenate((np.zeros(delay + 480), reference[: -delay - 480])) * 0.17
        )
        microphone += reflection + rng.normal(0, 35, len(reference))
        result = measure_delay(records(reference), records(microphone))
        self.assertAlmostEqual(result["delay_ms"], 770.0, delta=0.5)
        self.assertGreater(result["confidence"], 0.85)

    def test_uncorrelated_room_noise_does_not_produce_a_saved_delay(self):
        rng = np.random.default_rng(43)
        reference = np.concatenate((probe16(), np.zeros(16000 * 6)))
        microphone = rng.normal(0, 300, len(reference))
        with self.assertRaisesRegex(ValueError, "Unreliable"):
            measure_delay(records(reference), records(microphone))

    def test_clipped_capture_is_rejected(self):
        reference = np.concatenate((probe16(), np.zeros(16000)))
        microphone = np.clip(reference * 1000, -32768, 32767)
        with self.assertRaisesRegex(ValueError, "Unreliable"):
            measure_delay(records(reference), records(microphone), 500)

    def test_silent_reference_cannot_calibrate(self):
        frames = records(np.zeros(16000))
        with self.assertRaises(ValueError):
            measure_delay(frames, frames)

    def test_capture_and_reference_clocks_tolerate_packet_jitter(self):
        framer = ClockedFrames()
        chunks = []
        for index in range(50):
            jitter = 0.002 if index % 2 else -0.002
            chunks += framer.append(bytes(640), 10 + (index + 1) * 0.02 + jitter)
        differences = np.diff([stamp for stamp, _ in chunks])
        self.assertTrue(np.all(np.abs(differences - 0.02) < 0.0001))
        self.assertEqual(framer.resets, 0)
        framer.append(bytes(640), 13.0)
        self.assertEqual(framer.resets, 1)
        self.assertLess(len(framer.data), 640)

    def test_reference_history_is_bounded_and_handles_missing_intervals(self):
        history = ReferenceHistory(seconds=1)
        for i in range(200):
            history.append(bytes(960), 100 + i * 0.02)
        self.assertLessEqual(len(history.frames), 52)
        self.assertFalse(np.any(history.read(90)))


class NativeEchoTests(unittest.TestCase):
    def test_echo_is_reduced_after_lossy_mp3_encoding_and_decoding(self):
        import subprocess

        rng = np.random.default_rng(41)
        reference24 = pcm(colour(rng.normal(0, 4500, 24000 * 8)))
        encoded = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "s16le",
                "-ar",
                "24000",
                "-ac",
                "1",
                "-i",
                "pipe:0",
                "-codec:a",
                "libmp3lame",
                "-b:a",
                "128k",
                "-write_xing",
                "0",
                "-id3v2_version",
                "0",
                "-f",
                "mp3",
                "pipe:1",
            ],
            input=reference24,
            capture_output=True,
            check=True,
        ).stdout
        decoded = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "mp3",
                "-i",
                "pipe:0",
                "-ar",
                "16000",
                "-ac",
                "1",
                "-f",
                "s16le",
                "pipe:1",
            ],
            input=encoded,
            capture_output=True,
            check=True,
        ).stdout
        reference16, _ = audioop.ratecv(reference24, 2, 1, 24000, 16000, None)
        reference = np.frombuffer(reference16, dtype="<i2").astype(float)
        microphone = (
            np.frombuffer(decoded, dtype="<i2").astype(float)[: len(reference)] * 0.6
        )
        native = SpeexEcho(suppress_db=0, active_db=0)
        try:
            cleaned = np.concatenate(
                [
                    np.frombuffer(
                        native.process(
                            pcm(microphone[i : i + 320]), pcm(reference[i : i + 320])
                        ),
                        dtype="<i2",
                    )
                    for i in range(0, len(reference), 320)
                ]
            ).astype(float)
        finally:
            native.close()
        erle = 10 * np.log10(
            np.mean(microphone[-32000:] ** 2) / np.mean(cleaned[-32000:] ** 2)
        )
        self.assertGreater(erle, 8, f"Only {erle:.1f} dB echo reduction after MP3")

    def test_real_native_filter_reduces_echo_with_a_room_tail(self):
        rng = np.random.default_rng(4)
        reference = colour(rng.normal(0, 4500, 16000 * 8))
        microphone = np.concatenate((np.zeros(320), reference[:-320])) * 0.6
        microphone += np.concatenate((np.zeros(800), reference[:-800])) * 0.2
        native = SpeexEcho(suppress_db=0, active_db=0)
        try:
            output = np.concatenate(
                [
                    np.frombuffer(
                        native.process(
                            pcm(microphone[i : i + 320]), pcm(reference[i : i + 320])
                        ),
                        dtype="<i2",
                    )
                    for i in range(0, len(reference), 320)
                ]
            ).astype(float)
        finally:
            native.close()
        erle = 10 * np.log10(
            np.mean(microphone[-32000:] ** 2) / np.mean(output[-32000:] ** 2)
        )
        self.assertGreater(erle, 18, f"Only {erle:.1f} dB echo reduction")

    def test_simultaneous_user_speech_is_preserved(self):
        rng = np.random.default_rng(7)
        reference = colour(rng.normal(0, 3500, 16000 * 8))
        near = colour(rng.normal(0, 3000, len(reference)))
        near[: 16000 * 4] = 0
        microphone = np.concatenate((np.zeros(320), reference[:-320])) * 0.6 + near
        native = SpeexEcho(suppress_db=-25, active_db=-8)
        try:
            output = np.concatenate(
                [
                    np.frombuffer(
                        native.process(
                            pcm(microphone[i : i + 320]), pcm(reference[i : i + 320])
                        ),
                        dtype="<i2",
                    )
                    for i in range(0, len(reference), 320)
                ]
            ).astype(float)
        finally:
            native.close()
        # Account for the preprocessor's analysis-window latency when comparing
        # the preserved near-end signal, rather than assuming zero DSP latency.
        matches = normalised_matches(
            near[16000 * 5 : 16000 * 7], output[16000 * 5 : 16000 * 7 + 640]
        )
        self.assertGreater(float(np.max(matches)), 0.75)
        self.assertGreater(np.std(output[-16000:]) / np.std(near[-16000:]), 0.4)

    def test_manual_offset_aligns_the_real_processor_not_just_a_delay_detector(self):
        rng = np.random.default_rng(8)
        reference24 = colour(rng.normal(0, 4500, 24000 * 8))
        raw24 = pcm(reference24)
        raw16, _ = audioop.ratecv(raw24, 2, 1, 24000, 16000, None)
        reference16 = np.frombuffer(raw16, dtype="<i2").astype(float)
        microphone = np.concatenate((np.zeros(6400), reference16[:-6400])) * 0.6
        settings = Settings(
            tts_output="media_player",
            aec_delay_ms=400,
            aec_auto_delay=False,
            aec_suppression_db=0,
            aec_suppression_active_db=0,
        )
        processor = EchoProcessor(settings, SimpleNamespace())
        output = []
        try:
            for i in range(len(reference16) // 320):
                processor.reference(raw24[i * 960 : (i + 1) * 960], 100 + i * 0.02)
                output.append(
                    np.frombuffer(
                        processor.process(
                            pcm(microphone[i * 320 : (i + 1) * 320]),
                            100 + (i + 1) * 0.02,
                        ),
                        dtype="<i2",
                    )
                )
        finally:
            processor.close()
        output = np.concatenate(output).astype(float)
        erle = 10 * np.log10(
            np.mean(microphone[-32000:] ** 2) / np.mean(output[-32000:] ** 2)
        )
        self.assertGreater(erle, 15)

    def test_delay_tracking_follows_independent_clock_drift(self):
        rng = np.random.default_rng(9)
        duration = 24
        source24 = pcm(colour(rng.normal(0, 4000, 24000 * duration)))
        data16, _ = audioop.ratecv(source24, 2, 1, 24000, 16000, None)
        reference = np.frombuffer(data16, dtype="<i2").astype(float)
        positions = np.arange(len(reference))
        # Deliberately severe 1000 ppm drift: delay changes by 24 ms.
        microphone = (
            np.interp(positions * 1.001 - 6400, positions, reference, left=0, right=0)
            * 0.6
        )
        processor = EchoProcessor(
            Settings(tts_output="media_player", aec_delay_ms=400), SimpleNamespace()
        )
        try:
            for i in range(len(reference) // 320):
                processor.reference(source24[i * 960 : (i + 1) * 960], 100 + i * 0.02)
                processor.process(
                    pcm(microphone[i * 320 : (i + 1) * 320]), 100 + (i + 1) * 0.02
                )
            self.assertAlmostEqual(processor.delay_ms, 400 - duration, delta=8)
        finally:
            processor.close()

    def test_unrelated_user_speech_does_not_realign_the_reference(self):
        rng = np.random.default_rng(10)
        processor = EchoProcessor(
            Settings(tts_output="media_player", aec_delay_ms=400), SimpleNamespace()
        )
        try:
            for i in range(250):
                processor.reference(pcm(rng.normal(0, 2000, 480)), 100 + i * 0.02)
                processor.process(pcm(rng.normal(0, 3000, 320)), 100 + (i + 1) * 0.02)
            self.assertAlmostEqual(processor.delay_ms, 400, delta=0.001)
        finally:
            processor.close()

    def test_bypass_preserves_original_chunks_and_local_hardware_path(self):
        for settings in (
            Settings(aec_enabled=False, tts_output="media_player", aec_delay_ms=400),
            Settings(tts_output="voice_pe", aec_delay_ms=400),
            Settings(tts_output="media_player", aec_delay_ms=-1),
        ):
            processor = EchoProcessor(settings, SimpleNamespace())
            try:
                data = b"\1\0" * 73
                self.assertEqual(processor.process(data), data)
                self.assertIsNone(processor.native)
            finally:
                processor.close()


class CalibrationStorageTests(unittest.TestCase):
    def test_saved_delay_is_scoped_to_speaker_microphone_and_stream_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            store = CalibrationStore(Path(directory) / "calibration.json")
            settings = Settings(tts_media_player="media_player.lounge")
            device = SimpleNamespace(identity="192.168.1.247")
            store.save(settings, device, {"delay_ms": 413.0, "confidence": 0.9})
            self.assertEqual(store.load(settings, device), 413.0)
            settings.tts_media_player = "media_player.bedroom"
            self.assertIsNone(store.load(settings, device))
            settings.tts_media_player = "media_player.lounge"
            device.identity = "192.168.1.248"
            self.assertIsNone(store.load(settings, device))
            self.assertNotIn("audio", store.path.read_text())

    def test_corrupt_or_nonfinite_calibration_does_not_activate_aec(self):
        with tempfile.TemporaryDirectory() as directory:
            store = CalibrationStore(Path(directory) / "calibration.json")
            settings, device = Settings(), SimpleNamespace()
            key = store.key(settings, device)
            for value in (
                "broken",
                {key: "not an object"},
                {key: {"delay_ms": float("nan")}},
                {key: {"delay_ms": True}},
            ):
                store.path.write_text(
                    value if isinstance(value, str) else json.dumps(value)
                )
                self.assertIsNone(store.load(settings, device))

    def test_auto_mode_uses_the_measurement_and_manual_mode_overrides_it(self):
        store = SimpleNamespace(load=lambda *_: 413.0)
        processor = EchoProcessor(
            Settings(tts_output="media_player"), SimpleNamespace(), store
        )
        try:
            self.assertEqual(processor.delay_ms, 413.0)
        finally:
            processor.close()
        processor = EchoProcessor(
            Settings(tts_output="media_player", aec_delay_ms=500),
            SimpleNamespace(),
            store,
        )
        try:
            self.assertEqual(processor.delay_ms, 500.0)
        finally:
            processor.close()

    def test_invalid_aec_configuration_is_rejected(self):
        for overrides in (
            {"aec_delay_ms": -0.5},
            {"aec_alignment_margin_ms": 100, "aec_filter_ms": 80},
            {"aec_calibrate_on_next_wake": True},
            {"aec_enabled": "true"},
        ):
            with self.assertRaises(ValueError):
                Settings(openai_api_key="fake", **overrides).validate()
