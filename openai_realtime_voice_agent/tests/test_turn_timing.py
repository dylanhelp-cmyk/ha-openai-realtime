import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.device import Device
from app.live_session import LiveSession
from app.output import SpeechOutput
from app.pcm import InputAudio, SILENCE
from app.settings import Settings
from app.turn_timing import TurnTiming
from test_live import Socket, TONE, until
from test_startup_timing import Clock


class TurnTimingTests(unittest.TestCase):
    def test_local_pause_receive_and_handoff_are_separate_and_greeting_unpaired(self):
        with self.assertLogs("app.turn_timing", "INFO") as logs:
            timing = TurnTiming(Settings(), 12, 100)
            greeting = timing.output_received(True, 100.5, 0, 600)
            timing.output_handed(greeting, 100.6)
            timing.input_received(TONE, 102, 600, 0)
            timing.input_received(TONE, 102.2, 600, 0)
            timing.input_received(SILENCE, 102.51, 600, 0)
            timing.input_received(SILENCE, 102.6, 600, 0)
            timing.input_dequeued(TONE, 102.8, 600, 600, 0, 0)
            reply = timing.output_received(True, 103.1, 120, 600)
            timing.output_handed(reply, 103.24)
        messages = "\n".join(logs.output)
        self.assertIn("reply=1 user_burst=n/a", messages)
        self.assertEqual(messages.count("USER_PAUSE"), 1)
        self.assertIn("end_since_wake_ms=2200", messages)
        self.assertIn("pause_to_reply_ms=900", messages)
        self.assertIn("mic_last_speech_residence_ms=600", messages)
        self.assertIn("output_queue_wait_ms=140 pause_to_handoff_ms=1040", messages)
        self.assertIn("NOT audible playback", messages)

    def test_received_pcm_burst_is_reported_once_and_overlap_is_labelled(self):
        with self.assertLogs("app.turn_timing", "INFO") as logs:
            timing = TurnTiming(Settings(), 1, 100)
            timing.input_received(TONE, 101, 0, 0)
            first = timing.output_received(True, 101.1, 0, 0)
            self.assertIsNotNone(first)
            for i in range(20):
                self.assertIsNone(timing.output_received(True, 101.1, i * 20, 0))
            for i in range(18):
                timing.output_received(False, 102, i * 20, 0)
            second = timing.output_received(True, 102, 360, 0)
            self.assertIsNone(second.user_number)
        messages = "\n".join(logs.output)
        self.assertEqual(messages.count("REPLY_RECEIVED"), 2)
        self.assertIn("overlap_or_short_pause=True", messages)

    def test_backend_and_tools_have_independent_durations_without_payloads(self):
        with self.assertLogs("app.turn_timing", "INFO") as logs:
            timing = TurnTiming(Settings(), 2, 100)
            timing.backend_started("private-id", 101)
            timing.backend_finished("private-id", 101.8, "completed")
            timing.backend_finished("missing-id", 102, "failed")
            timing.tool_finished(102, 102.3)
            for i in range(40):
                timing.backend_started(str(i), 103)
        self.assertLessEqual(len(timing.backends), 32)
        messages = "\n".join(logs.output)
        self.assertIn("response_ms=800", messages)
        self.assertIn("response_ms=n/a", messages)
        self.assertIn("tool_ms=300", messages)
        self.assertNotIn("private-id", messages)

    def test_input_residence_tracks_overflow_trim_and_clear_without_audio_changes(self):
        clock = Clock()
        plain, observed = InputAudio(600), InputAudio(600, True)
        with patch("app.pcm.time", clock):
            for i in range(40):
                audio = SILENCE if i < 32 else TONE
                plain.append(audio)
                observed.append(audio)
                clock.advance(0.02)
            self.assertEqual(observed.data, plain.data)
            self.assertEqual(observed.dropped, plain.dropped)
            self.assertEqual(
                observed.trim_startup_silence(), plain.trim_startup_silence()
            )
            self.assertEqual(
                sum(size for size, _ in observed.arrivals), len(observed.data)
            )
            self.assertEqual(observed.frame(), plain.frame())
            self.assertGreater(observed.last_frame_residence_ms, 0)
            self.assertEqual(
                sum(size for size, _ in observed.arrivals), len(observed.data)
            )
            observed.clear()
            self.assertFalse(observed.arrivals)
            self.assertEqual(observed.frame(), SILENCE)
            self.assertIsNone(observed.last_frame_residence_ms)

    def test_new_setting_is_opt_in_and_strict_boolean(self):
        self.assertFalse(Settings().turn_timing_diagnostic)
        with self.assertRaises(ValueError):
            Settings(openai_api_key="fake", turn_timing_diagnostic="true").validate()


class TurnTimingIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_output_diagnostic_preserves_bytes_order_and_marks_each_burst(self):
        async def render(enabled):
            settings = Settings()
            device = Device(Socket(), settings)
            delivered = []
            device.audio = AsyncMock(side_effect=delivered.append)
            out = SpeechOutput(
                settings,
                device,
                None,
                None,
                AsyncMock(),
                AsyncMock(),
                AsyncMock(),
                "fake",
            )
            marker = SimpleNamespace(received=0)
            timing = SimpleNamespace(
                output_received=lambda loud, *args: marker if loud else None,
                output_handed=unittest.mock.Mock(),
            )
            if enabled:
                out.timing = timing
            try:
                await out.write(SILENCE[:100])
                await out.write(SILENCE[100:] + TONE * 2 + SILENCE)
                await until(lambda: len(delivered) >= 4)
                if enabled:
                    self.assertEqual(timing.output_handed.call_count, 2)
            finally:
                await out.close()
            return delivered[:4]

        self.assertEqual(await render(False), await render(True))

    async def test_live_input_and_tool_observations_use_existing_path(self):
        settings = Settings(turn_timing_diagnostic=True)
        device = Device(Socket(), settings)
        tools = SimpleNamespace(
            catalog="", tools=[], call=AsyncMock(return_value={"ok": True})
        )
        with self.assertLogs("app.turn_timing", "INFO") as logs:
            live = LiveSession(settings, device, None, tools, None, "fake")
            live.echo.process = lambda audio: audio
            live.send = AsyncMock()
            try:
                live.receive_audio(TONE)
                data = bytes(live.input.data)
                self.assertEqual(live.input_frame()[: len(data)], data)
                await live.backend_event(
                    {"type": "response.created", "response": {"id": "r"}}
                )
                await live.backend_event(
                    {"type": "response.completed", "response": {"id": "r"}}
                )
                await live._complete_tools(
                    [
                        {
                            "name": "secret-name",
                            "arguments": '{"secret":"hidden"}',
                            "call_id": "c",
                        }
                    ]
                )
            finally:
                await live.close()
        messages = "\n".join(logs.output)
        self.assertIn("QUEUES", messages)
        self.assertIn("BACKEND_DONE", messages)
        self.assertIn("TOOL_DONE", messages)
        self.assertNotIn("secret-name", messages)
        self.assertNotIn("hidden", messages)
        tools.call.assert_awaited_once_with("secret-name", {"secret": "hidden"})
