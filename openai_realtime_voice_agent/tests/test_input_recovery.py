import random
import struct
import unittest
from unittest.mock import Mock, patch

from app.input_recovery import InputRecovery
from app.pcm import FRAME_BYTES, InputAudio, SILENCE
from app.settings import Settings


def frame(level):
    return struct.pack("<h", level) * 480


class RecoveryTests(unittest.TestCase):
    def classifier(self):
        recovery = InputRecovery()
        # Deterministic classification for byte-exact ordering/guard tests.
        recovery.vad = Mock()
        recovery.vad.is_speech.side_effect = lambda audio, rate: (
            audio[:2] == b"\x64\x00"
        )
        return recovery

    def test_noise_prefix_is_shortened_with_100_ms_before_speech(self):
        recovery = self.classifier()
        quiet, speech = frame(10), frame(100)
        audio = quiet * 20 + speech * 5
        recovery.append(audio)
        remove = recovery.removable_prefix(len(audio))
        self.assertEqual(remove, 15 * FRAME_BYTES)
        self.assertEqual(audio[remove:], quiet * 5 + speech * 5)

    def test_front_speech_and_short_pauses_are_never_skipped(self):
        recovery = self.classifier()
        audio = frame(100) * 5 + SILENCE * 25
        recovery.append(audio)
        self.assertEqual(recovery.removable_prefix(len(audio)), 0)
        recovery.consume(5 * FRAME_BYTES)
        # Explicit hangover still protects the end of the utterance.
        self.assertEqual(recovery.removable_prefix(25 * FRAME_BYTES), 0)
        recovery.consume(5 * FRAME_BYTES)
        self.assertGreater(recovery.removable_prefix(20 * FRAME_BYTES), 0)
        recovery = self.classifier()
        recovery.append(SILENCE * 14 + frame(100))
        self.assertEqual(recovery.removable_prefix(15 * FRAME_BYTES), 0)

    def test_loud_unvoiced_audio_is_guarded_even_if_vad_rejects_it(self):
        recovery = self.classifier()
        recovery.append(frame(1000) * 30)
        self.assertEqual(recovery.removable_prefix(30 * FRAME_BYTES), 0)

    def test_small_queue_is_not_trimmed_and_partial_samples_are_retained(self):
        recovery = self.classifier()
        recovery.append(SILENCE * 8 + b"\x00\x00")
        self.assertEqual(recovery.removable_prefix(8 * FRAME_BYTES + 2), 0)
        recovery.append(SILENCE * 22)
        recovery.consume(2)
        remove = recovery.removable_prefix(30 * FRAME_BYTES)
        self.assertEqual((remove + 2) % FRAME_BYTES, 0)
        self.assertGreaterEqual(30 * FRAME_BYTES - remove, 5 * FRAME_BYTES)

    def test_detector_failure_protects_all_audio_and_is_logged_once(self):
        recovery = self.classifier()
        recovery.vad.is_speech.side_effect = RuntimeError("private error text")
        with self.assertLogs("app.input_recovery", "WARNING") as logs:
            recovery.append(SILENCE * 30)
        self.assertEqual(len(logs.output), 1)
        self.assertNotIn("private", logs.output[0])
        self.assertEqual(recovery.removable_prefix(30 * FRAME_BYTES), 0)
        self.assertTrue(all(protected for _, protected in recovery.frames))

    def test_detector_initialization_failure_does_not_stop_microphone(self):
        with (
            patch("app.input_recovery.webrtcvad.Vad", side_effect=RuntimeError()),
            self.assertLogs("app.input_recovery", "WARNING"),
        ):
            buffer = InputAudio(600, recover_backlog=True)
        buffer.append(bytes(640) * 30)
        self.assertFalse(buffer.recover_backlog())
        self.assertEqual(len(buffer.frame()), FRAME_BYTES)

    def test_real_vad_catches_up_on_room_noise_that_old_trim_would_keep(self):
        rng = random.Random(2)
        room = struct.pack("<320h", *(rng.randint(-220, 220) for _ in range(320)))
        buffer = InputAudio(600, True, True)
        for _ in range(100):
            buffer.append(room)
        self.assertEqual(buffer.trim_startup_silence(), 0)
        before = bytes(buffer.data)
        lost = buffer.dropped
        removed = buffer.recover_backlog()
        self.assertGreaterEqual(removed, 450)
        self.assertLessEqual(len(buffer.data) / 48, 120)
        self.assertEqual(buffer.data, before[round(removed * 48) :])
        self.assertEqual(buffer.dropped, lost)  # Quiet removal != overflow loss.
        self.assertEqual(buffer.quiet_removed_ms, removed)
        self.assertEqual(sum(n for n, _ in buffer.arrivals), len(buffer.data))
        self.assertFalse(buffer.recover_backlog())

    def test_clear_resets_classifier_alignment_and_partial_data(self):
        buffer = InputAudio(600, True, True)
        buffer.append(frame(1000) * 20 + b"\x00")
        self.assertTrue(buffer.recovery.frames)
        buffer.clear()
        self.assertFalse(buffer.recovery.frames)
        self.assertFalse(buffer.recovery.partial)
        self.assertEqual(buffer.recovery.consumed, 0)
        self.assertEqual(buffer.recovery.framed, 0)
        buffer.append(bytes(640) * 30)
        self.assertGreater(buffer.recover_backlog(), 450)

    def test_classifier_runs_once_per_frame_not_on_every_recovery_attempt(self):
        recovery = self.classifier()
        recovery.append(SILENCE * 30)
        for _ in range(30):
            recovery.removable_prefix(30 * FRAME_BYTES)
        self.assertEqual(recovery.vad.is_speech.call_count, 30)

    def test_opt_out_keeps_original_audio_and_boolean_validation(self):
        buffer = InputAudio(600)
        buffer.append(bytes(640) * 30)
        before = bytes(buffer.data)
        self.assertEqual(buffer.recover_backlog(), 0)
        self.assertEqual(buffer.data, before)
        self.assertTrue(Settings().input_backlog_recovery)
        with self.assertRaises(ValueError):
            Settings(openai_api_key="fake", input_backlog_recovery="true").validate()

    def test_recovery_metadata_does_not_change_resampling_or_frame_order(self):
        plain, observed = InputAudio(600), InputAudio(600, True, True)
        audio = frame(1000) * 35 + b"\x01"
        with patch("app.input_recovery.webrtcvad.Vad") as vad:
            vad.return_value.is_speech.return_value = True
            observed.clear()
            for start in range(0, len(audio), 333):
                chunk = audio[start : start + 333]
                plain.append(chunk)
                observed.append(chunk)
                self.assertFalse(observed.recover_backlog())
                self.assertEqual(observed.data, plain.data)
            while observed.data:
                self.assertEqual(observed.frame(), plain.frame())
            self.assertFalse(observed.arrivals)
            self.assertLess(len(observed.recovery.partial), FRAME_BYTES)
