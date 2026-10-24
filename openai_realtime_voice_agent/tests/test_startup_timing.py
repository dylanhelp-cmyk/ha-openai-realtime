"""Startup diagnostics separate overlapping phases without changing audio flow."""

import asyncio
import base64
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp
from app.device import Device
from app.live_session import LiveSession
from app.output import SpeechOutput
from app.pcm import SILENCE
from app.settings import Settings
from test_live import TONE, Socket, until


class Clock:
    def __init__(self):
        self.now = 100.0

    def monotonic(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class StartupTimingTests(unittest.IsolatedAsyncioTestCase):
    async def test_transport_session_and_first_nonempty_audio_have_separate_timings(
        self,
    ):
        clock = Clock()
        settings = Settings(openai_api_key="fake-key")
        device = Device(Socket(), settings)
        device.wake_id = 77
        sent = []

        class LiveSocket:
            closed = False

            async def send_json(self, event):
                sent.append(event)
                if event["type"] == "session.start":
                    clock.advance(0.07)

            async def receive(self):
                clock.advance(1.6)
                return SimpleNamespace(
                    type=aiohttp.WSMsgType.TEXT,
                    data=json.dumps({"type": "session.started"}),
                )

            def __aiter__(self):
                return self.events()

            async def events(self):
                for delay, audio in ((0.2, b""), (0.7, SILENCE), (0.4, TONE)):
                    clock.advance(delay)
                    yield SimpleNamespace(
                        type=aiohttp.WSMsgType.TEXT,
                        data=json.dumps(
                            {
                                "type": "session.output_audio.delta",
                                "delta": base64.b64encode(audio).decode(),
                            }
                        ),
                    )

            async def close(self):
                self.closed = True

        async def connect(*args, **kwargs):
            clock.advance(0.4)
            return LiveSocket()

        http = SimpleNamespace(ws_connect=AsyncMock(side_effect=connect))
        with (
            patch("app.live_session.time", clock),
            patch("app.output.time", clock),
            self.assertLogs("app.live_session", "INFO") as logs,
        ):
            live = LiveSession(
                settings,
                device,
                http,
                SimpleNamespace(catalog="", tools=[]),
                None,
                "fake-token",
                wake_started_at=99.5,
            )
            original = live.session_config

            def config():
                clock.advance(0.03)
                return original()

            live.session_config = config
            await live.run()
        messages = "\n".join(logs.output)
        self.assertIn("WebSocket connected in 400 ms", messages)
        self.assertIn("wake=77, before_connect=500 ms, since_wake=900 ms", messages)
        self.assertIn(
            "config=30 ms, session_init=1670 ms, since_wake=2600 ms", messages
        )
        self.assertIn(
            "first audio received in 3500 ms (wake=77, after_ready=900 ms", messages
        )
        self.assertEqual(messages.count("first audio received"), 1)
        self.assertNotIn("fake-key", messages)
        self.assertNotIn("fake-token", messages)
        self.assertEqual([e["type"] for e in sent].count("session.start"), 1)
        self.assertEqual(
            [e["type"] for e in sent].count("session.commentary.append"), 1
        )

    async def test_first_speech_handoff_excludes_keepalive_silence_and_logs_once(self):
        clock = Clock()
        settings = Settings()
        device = Device(Socket(), settings)
        device.wake_id = 78
        audio = []

        async def deliver(frame):
            audio.append(frame)
            if frame == TONE:
                clock.advance(0.25)

        device.audio = deliver
        with (
            patch("app.output.time", clock),
            self.assertLogs("app.output", "INFO") as logs,
        ):
            out = SpeechOutput(
                settings,
                device,
                None,
                None,
                AsyncMock(),
                AsyncMock(),
                AsyncMock(),
                "fake",
                wake_started_at=99.0,
            )
            try:
                await out.write(SILENCE + TONE * 2)
                await until(lambda: audio.count(TONE) == 2)
            finally:
                await out.close()
        messages = "\n".join(logs.output)
        self.assertIn("first speech handed to Voice PE in 1250 ms", messages)
        self.assertIn("wake=78, output_queue=250 ms", messages)
        self.assertIn("not audible playback", messages)
        self.assertEqual(messages.count("first speech handed"), 1)
        self.assertEqual(audio[:3], [SILENCE, TONE, TONE])

    async def test_speaker_fetch_breakdown_uses_the_http_fetch_timestamp(self):
        clock = Clock()
        settings = Settings(tts_output="media_player")
        device = Device(Socket(), settings)
        device.wake_id = 79
        resource = SimpleNamespace(
            fetched=asyncio.Event(),
            finished=asyncio.Event(),
            first_fetch_at=None,
            error=None,
        )
        resource.finished.set()

        async def ready():
            clock.advance(0.12)

        async def ha(method, path, **kwargs):
            if method == "GET":
                clock.advance(0.04)
                return {"attributes": {}}
            if path.endswith("play_media"):
                clock.advance(0.3)
                resource.first_fetch_at = clock.monotonic()
                resource.fetched.set()
                clock.advance(0.2)  # Scheduling after fetch must not inflate its time.
            return []

        server = SimpleNamespace(
            public_base_url="http://speaker.test",
            url=lambda _: "http://speaker.test/audio/test.mp3",
            remove=AsyncMock(),
        )
        with (
            patch("app.output.time", clock),
            self.assertLogs("app.output", "INFO") as logs,
        ):
            out = SpeechOutput(
                settings,
                device,
                None,
                server,
                AsyncMock(),
                AsyncMock(),
                AsyncMock(),
                "fake",
                wake_started_at=99.5,
            )
            out.worker.cancel()
            await asyncio.gather(out.worker, return_exceptions=True)
            out.encoder = SimpleNamespace(
                resource=resource, ready=ready, cancel=AsyncMock()
            )
            out._ha = ha
            try:
                await out._external()
            finally:
                await out.close()
        messages = "\n".join(logs.output)
        self.assertIn("speaker stream fetched in 460 ms", messages)
        self.assertIn(
            "wake=79, encoder_ready=120 ms, player_state=40 ms, play_to_fetch=300 ms, since_wake=960 ms",
            messages,
        )
        self.assertIn("HTTP fetch, not audible playback", messages)
