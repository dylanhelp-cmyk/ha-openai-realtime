"""Cached greeting tests: fake Live peer only; no real OpenAI calls."""

import asyncio
import base64
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import aiohttp

from app.device import Device
from app.live_session import LiveSession
from app.main import VoiceBridge
from app.pcm import FRAME_BYTES, InputAudio, SILENCE
from app.settings import Settings
from app.wake_cue import WakeCueCache, TEXT, trim_clip
from test_live import Socket, TONE, until


class CueSocket:
    def __init__(self, wording="Yes sir?", fail=False):
        self.closed = False
        self.commands = []
        self.events = asyncio.Queue()
        self.wording, self.fail = wording, fail

    async def send_json(self, event):
        self.commands.append(event)
        kind = event["type"]
        if kind == "session.start":
            self.events.put_nowait({"type": "session.started"})
        elif kind == "session.commentary.append":
            if self.fail:
                self.events.put_nowait(
                    {"type": "error", "error": {"message": "private-key"}}
                )
            else:
                self.events.put_nowait(
                    {"type": "session.output_transcript.delta", "delta": self.wording}
                )
                self.events.put_nowait(
                    {
                        "type": "session.output_audio.delta",
                        "delta": base64.b64encode(
                            SILENCE * 5 + TONE * 20 + SILENCE * 25
                        ).decode(),
                    }
                )
            await asyncio.sleep(0)
        elif kind == "session.close":
            self.events.put_nowait({"type": "session.closed", "usage": {"seconds": 3}})

    async def receive(self):
        event = await self.events.get()
        return SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(event))

    async def close(self):
        self.closed = True


class CueCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_generation_uses_vesper_once_checks_words_and_closes_paid_session(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            socket = CueSocket()
            http = SimpleNamespace(ws_connect=AsyncMock(return_value=socket))
            settings = Settings(openai_api_key="fake", openai_voice="vesper")
            cache = WakeCueCache(settings, http, directory)
            with self.assertLogs("app.wake_cue", "INFO") as logs:
                await cache.prepare()
                await cache.prepare()
            http.ws_connect.assert_awaited_once()
            self.assertTrue(socket.closed)
            session = socket.commands[0]["session"]
            self.assertEqual(session["audio"]["output"]["voice"], "vesper")
            self.assertEqual(session["delegation"], {"type": "client"})
            self.assertFalse(session["store"])
            self.assertIn(
                "session.input_audio.append", [e["type"] for e in socket.commands]
            )
            self.assertNotIn("tools", json.dumps(session))
            self.assertEqual(
                [e["type"] for e in socket.commands].count("session.close"), 1
            )
            self.assertGreater(len(cache.audio), len(TONE) * 20)
            self.assertIn("final_confirmed=True", "\n".join(logs.output))
            saved = json.loads(cache.path.read_text())
            self.assertEqual(saved["text"], TEXT)
            self.assertNotIn("fake", cache.path.read_text())
            self.assertNotIn("fake", "\n".join(logs.output))

    async def test_error_and_wrong_wording_do_not_cache_or_retry_at_wake(self):
        for wording, fail in (("Yes sir. Anything else?", False), ("", True)):
            with tempfile.TemporaryDirectory() as directory:
                socket = CueSocket(wording, fail)
                http = SimpleNamespace(ws_connect=AsyncMock(return_value=socket))
                cache = WakeCueCache(Settings(), http, directory)
                with self.assertLogs("app.wake_cue", "INFO") as logs:
                    await cache.prepare()
                self.assertIsNone(cache.audio)
                self.assertFalse(cache.path.exists())
                self.assertTrue(socket.closed)
                http.ws_connect.assert_awaited_once()
                self.assertNotIn("private-key", "\n".join(logs.output))

    async def test_prepare_cancellation_sends_close_and_reaps_silence_pump(self):
        with tempfile.TemporaryDirectory() as directory:
            socket = CueSocket()

            async def send(event):
                socket.commands.append(event)
                if event["type"] == "session.start":
                    socket.events.put_nowait({"type": "session.started"})
                elif event["type"] == "session.close":
                    socket.events.put_nowait(
                        {"type": "session.closed", "usage": {"seconds": 1}}
                    )

            socket.send_json = send
            cache = WakeCueCache(
                Settings(),
                SimpleNamespace(ws_connect=AsyncMock(return_value=socket)),
                directory,
            )
            cache.task = asyncio.create_task(cache.prepare())
            await until(
                lambda: any(
                    e["type"] == "session.commentary.append" for e in socket.commands
                )
            )
            await cache.close()
            self.assertTrue(socket.closed)
            self.assertTrue(cache.task.done())
            self.assertIsNone(cache.audio)
            count = len(socket.commands)
            await asyncio.sleep(0.04)
            self.assertEqual(len(socket.commands), count)

    async def test_corrupt_cache_rejected_and_voice_has_distinct_cache_key(self):
        with tempfile.TemporaryDirectory() as directory:
            a = WakeCueCache(Settings(openai_voice="vesper"), None, directory)
            b = WakeCueCache(Settings(openai_voice="cedar"), None, directory)
            self.assertNotEqual(a.path, b.path)
            a.path.write_text('{"voice":"vesper","text":"Yes sir?","pcm":"bad"}')
            self.assertFalse(a.load())


class CueSessionTests(unittest.IsolatedAsyncioTestCase):
    def session(self, **options):
        settings = Settings(aec_enabled=False, **options)
        socket = Socket()
        device = Device(socket, settings)
        device.wake_id = 88
        device.mic_enabled = True
        live = LiveSession(
            settings,
            device,
            None,
            SimpleNamespace(catalog="", tools=[]),
            None,
            "fake",
            wake_cue=TONE * 5,
        )
        return live, socket

    async def test_cue_suppresses_cloud_greeting_and_policy_applies_to_custom_prompt(
        self,
    ):
        live, socket = self.session(
            instructions="Custom personality", opening_greeting="Old greeting"
        )
        try:
            config, _ = live.session_config()
            self.assertNotIn("input", config)
            self.assertIn("Do not greet or repeat", config["instructions"])
            self.assertIn("do not say okay", config["instructions"])
            self.assertIn("outcome is uncertain", config["instructions"])
            self.assertIn(
                "Answer information questions normally",
                config["delegation"]["responses"]["instructions"],
            )
            self.assertEqual(config["audio"]["output"]["voice"], "vesper")
            self.assertEqual(
                config["delegation"]["responses"]["reasoning"], {"effort": "none"}
            )
        finally:
            await live.close()

    async def test_cue_plays_while_connect_waits_and_stop_cancels_remaining_frames(
        self,
    ):
        live, socket = self.session()
        connecting = asyncio.Event()

        async def connect(*args, **kwargs):
            connecting.set()
            await asyncio.Event().wait()

        live.http = SimpleNamespace(ws_connect=connect)
        runner = asyncio.create_task(live.run())
        await connecting.wait()
        await until(lambda: bool(socket.audio))
        self.assertFalse(live.ready.is_set())
        self.assertTrue(live.device.mic_enabled)
        await live.close()
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
        count = len(socket.audio)
        await asyncio.sleep(0.06)
        self.assertEqual(len(socket.audio), count)
        self.assertTrue(
            all(frame[:4] == (88).to_bytes(4, "little") for frame in socket.audio)
        )
        self.assertTrue(all(frame[4:] == TONE for frame in socket.audio))

    async def test_local_cue_and_local_model_output_are_serial_not_interleaved(self):
        live, socket = self.session()
        try:
            await live.output.write(bytes([200, 0]) * 480)
            await asyncio.sleep(0.02)
            self.assertFalse(socket.audio)
            await live.play_wake_cue()
            await until(lambda: len(socket.audio) > 5)
            self.assertEqual([p[4:] for p in socket.audio[:5]], [TONE] * 5)
            self.assertEqual(socket.audio[5][4:], bytes([200, 0]) * 480)
        finally:
            await live.close()

    async def test_stale_wake_does_not_receive_old_cue(self):
        live, socket = self.session()
        live.device.wake_id = 89
        try:
            await live.play_wake_cue()
            self.assertFalse(socket.audio)
        finally:
            await live.close()


class CueBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_paid_cue_preparation_when_a_diagnostic_is_armed(self):
        for flag in (
            "aec_calibrate_on_next_wake",
            "playback_diagnostic_on_next_wake",
            "render_lead_diagnostic_on_next_wake",
        ):
            settings = Settings(wake_cue_enabled=True, **{flag: True})
            cache = SimpleNamespace(
                audio=None,
                task=None,
                load=Mock(return_value=False),
                prepare=AsyncMock(),
                close=AsyncMock(),
            )
            with (
                patch("app.main.WakeCueCache", return_value=cache),
                patch("app.main.HomeTools.initialize", new_callable=AsyncMock),
            ):
                bridge = VoiceBridge(settings)
                try:
                    await bridge.start(None)
                    cache.load.assert_called_once()
                    cache.prepare.assert_not_awaited()
                    self.assertIsNone(cache.task)
                finally:
                    await bridge.close(None)

    async def test_disabled_cue_never_constructs_or_prepares_generation(self):
        with (
            patch("app.main.WakeCueCache") as cue,
            patch("app.main.HomeTools.initialize", new_callable=AsyncMock),
        ):
            bridge = VoiceBridge(Settings(wake_cue_enabled=False))
            try:
                await bridge.start(None)
                cue.assert_not_called()
            finally:
                await bridge.close(None)


class StartupBufferTests(unittest.TestCase):
    def test_large_startup_speech_buffer_is_preserved_when_entering_steady_mode(self):
        buffer = InputAudio(13000, recover_backlog=True)
        speech = TONE * 100
        buffer.append(speech)
        expected = bytes(buffer.data)
        self.assertEqual(buffer.dropped, 0)
        self.assertGreater(len(buffer.data), 600 * 48)
        buffer.limit_when_drained(600)
        self.assertEqual(buffer.data, expected)
        self.assertEqual(buffer.limit, 13000 * 48)
        result = bytearray()
        while buffer.data:
            result.extend(buffer.frame())
        self.assertEqual(result[: len(expected)], expected)
        self.assertEqual(buffer.limit, 600 * 48)
        self.assertEqual(buffer.dropped, 0)

    def test_clip_trimming_rejects_silence_and_preserves_phrase(self):
        with self.assertRaises(ValueError):
            trim_clip(SILENCE * 100)
        clip = trim_clip(SILENCE * 10 + TONE * 20 + SILENCE * 30)
        self.assertIn(TONE * 20, clip)
        self.assertEqual(len(clip) % FRAME_BYTES, 0)
        self.assertLess(len(clip), len(SILENCE) * 60)
