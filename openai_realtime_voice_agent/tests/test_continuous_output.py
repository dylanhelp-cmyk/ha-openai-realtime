"""Exercise one actual encoder and HTTP connection across a whole conversation."""

import asyncio
import json
import math
import struct
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
from app.device import Device
from app.live_audio import AudioHTTPServer, AudioResource
from app.live_session import LiveSession
from app.output import SpeechOutput
from app.settings import Settings
from test_live import TONE, Socket, until


def tone(hz=440):
    return struct.pack(
        "<480h",
        *(int(6000 * math.sin(2 * math.pi * hz * n / 24000)) for n in range(480)),
    )


class ContinuousOutputTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = AudioHTTPServer("http://127.0.0.1:0", 0, "127.0.0.1")
        await self.server.start()
        self.http = aiohttp.ClientSession()
        self.s = Settings(
            microphone_mode="full_duplex",
            tts_stream_prebuffer_ms=40,
            tts_start_allowance_ms=0,
            output_silence_ms=100,
        )
        self.requests = []
        self.downloads = []
        self.received = bytearray()
        self.fetched = asyncio.Event()
        self.first_speech, self.end_speech = AsyncMock(), AsyncMock()
        self.error = AsyncMock()
        self.out = SpeechOutput(
            self.s,
            Device(Socket(), self.s),
            self.http,
            self.server,
            self.first_speech,
            self.end_speech,
            self.error,
            "fake",
        )
        self.out._ha = self.ha
        self.block_announcement_until_eof = False

    async def download(self, url):
        async with self.http.get(url + "?_=wiim-cache") as response:
            self.assertEqual(response.status, 200)
            self.assertNotIn("Content-Length", response.headers)
            async for block in response.content.iter_any():
                self.received.extend(block)
                self.fetched.set()

    async def ha(self, method, path, **kwargs):
        if method == "GET":
            return {"state": "playing", "attributes": {"media_title": "Spotify"}}
        self.requests.append((path, kwargs["json"]))
        if path.endswith("play_media"):
            task = asyncio.create_task(
                self.download(kwargs["json"]["media_content_id"])
            )
            self.downloads.append(task)
            if self.block_announcement_until_eof:
                await asyncio.shield(task)
        return []

    async def asyncTearDown(self):
        await self.out.close()
        await asyncio.gather(*self.downloads, return_exceptions=True)
        await self.http.close()
        await self.server.close()

    async def test_one_url_encoder_and_http_response_survive_speech_silence_and_followup(
        self,
    ):
        # The player connects at wake, before any output speech exists.
        await asyncio.wait_for(self.fetched.wait(), 3)
        encoder = self.out.encoder
        resource = encoder.resource
        self.assertFalse(self.out.active)
        await self.out.write(tone() * 15)
        await until(lambda: self.first_speech.await_count == 1)
        await until(lambda: self.end_speech.await_count == 1)
        size = len(self.received)
        await asyncio.sleep(0.3)
        self.assertGreater(len(self.received), size)
        self.assertFalse(self.downloads[0].done())
        self.assertFalse(resource.finished.is_set())
        await self.out.write(tone(880) * 15)
        await until(lambda: self.first_speech.await_count == 2)
        await until(lambda: self.end_speech.await_count == 2)
        self.assertIs(self.out.encoder, encoder)
        self.assertEqual(len(self.downloads), 1)
        self.assertEqual(len(self.server.resources), 1)
        plays = [body for path, body in self.requests if path.endswith("play_media")]
        self.assertEqual(len(plays), 1)
        self.assertTrue(plays[0]["announce"])
        self.error.assert_not_awaited()
        await self.out.close()
        await asyncio.gather(*self.downloads)
        self.assertFalse(self.server.resources)
        self.assertIsNotNone(encoder.process.returncode)
        self.assertEqual(sum(p.endswith("media_stop") for p, _ in self.requests), 1)
        # Decode the actual MP3, checking for both audible phrases separated
        # by a quiet interval on the same continuous timeline.
        decoder = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "mp3",
            "-i",
            "pipe:0",
            "-f",
            "s16le",
            "-ac",
            "1",
            "-ar",
            "24000",
            "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        pcm, errors = await decoder.communicate(bytes(self.received))
        self.assertEqual(decoder.returncode, 0, errors)
        samples = struct.unpack("<%dh" % (len(pcm) // 2), pcm)
        loud = [
            max(abs(x) for x in samples[i : i + 480]) > 1500
            for i in range(0, len(samples) - 480, 480)
        ]
        islands = sum(x and (i == 0 or not loud[i - 1]) for i, x in enumerate(loud))
        self.assertEqual(islands, 2)

    async def test_announcement_service_waiting_for_eof_does_not_block_audio(self):
        self.block_announcement_until_eof = True
        await asyncio.wait_for(self.fetched.wait(), 3)
        await self.out.write(tone() * 5)
        await until(lambda: self.first_speech.await_count == 1)
        await until(lambda: self.end_speech.await_count == 1)
        self.assertFalse(self.out.command.done())
        self.error.assert_not_awaited()

    async def test_speaker_failure_is_reported_without_reissuing_playback(self):
        self.out._ha = AsyncMock(side_effect=RuntimeError("offline"))
        await until(lambda: self.error.await_count == 1)
        self.assertTrue(self.out.worker.done())
        self.assertEqual(self.out._ha.await_count, 1)

    async def test_disconnected_player_fails_without_restarting_playback(self):
        await asyncio.wait_for(self.fetched.wait(), 3)
        self.s.tts_stream_start_timeout_s = 0.05
        self.downloads[0].cancel()
        await asyncio.gather(*self.downloads, return_exceptions=True)
        await until(lambda: self.error.await_count == 1)
        self.assertEqual(sum(p.endswith("play_media") for p, _ in self.requests), 1)

    async def test_expired_conversation_url_cannot_replay_previous_reply(self):
        await asyncio.wait_for(self.fetched.wait(), 3)
        url = self.server.url(self.out.encoder.resource)
        await self.out.close()
        async with self.http.get(url) as response:
            self.assertEqual(response.status, 404)

    async def test_encoded_memory_is_bounded_and_reconnect_joins_live_edge(self):
        await asyncio.wait_for(self.fetched.wait(), 3)
        resource = self.out.encoder.resource
        async with resource.changed:
            resource.data.clear()
            resource.start_offset = resource.total_bytes
        await resource.append(b"A" * 200000)
        await resource.append(b"B" * 10000)
        self.assertLessEqual(len(resource.data), resource.max_bytes)
        async with self.http.get(self.server.url(resource)) as response:
            self.assertEqual(await response.content.readexactly(100), b"B" * 100)

    async def test_output_backlog_is_bounded_instead_of_accumulating_delayed_speech(
        self,
    ):
        self.s.output_buffer_ms = 100
        with self.assertRaisesRegex(RuntimeError, "output_buffer_ms"):
            await self.out.write(TONE * 6)

    async def test_long_packet_gap_keeps_the_same_transport_open(self):
        await asyncio.wait_for(self.fetched.wait(), 3)
        url = self.server.url(self.out.encoder.resource)
        await self.out.write(tone() * 5)
        await until(lambda: self.end_speech.await_count == 1)
        await asyncio.sleep(0.8)  # Longer than the former packet-gap boundary.
        await self.out.write(tone() * 5)
        await until(lambda: self.end_speech.await_count == 2)
        self.assertEqual(self.server.url(self.out.encoder.resource), url)
        self.assertEqual(len(self.downloads), 1)

    async def test_idle_closes_paid_session_even_while_http_keepalive_is_streaming(
        self,
    ):
        await self.out.close()
        self.s.idle_timeout_seconds = 0.4
        self.s.opening_greeting = ""
        device = Device(Socket(), self.s)
        await device.mic(True)
        live = LiveSession(
            self.s,
            device,
            self.http,
            SimpleNamespace(catalog="", tools=[]),
            self.server,
            "fake",
        )
        self.out = live.output
        self.out._ha = self.ha
        events = []

        async def endpoint(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.receive_json()
            await ws.send_json({"type": "session.started"})
            async for message in ws:
                event = json.loads(message.data)
                events.append(event["type"])
                if event["type"] == "session.close":
                    await ws.send_json(
                        {"type": "session.closed", "usage": {"seconds": 0.4}}
                    )
                    break
            await ws.close()
            return ws

        app = web.Application()
        app.router.add_get("/live", endpoint)
        try:
            async with TestServer(app) as server:
                with patch("app.live_session.LIVE_URL", str(server.make_url("/live"))):
                    await asyncio.wait_for(live.run(), 3)
            self.assertTrue(self.fetched.is_set())
            self.assertIn("session.input_audio.append", events)
            self.assertIn("session.close", events)
            self.assertTrue(live.usage_confirmed)
            self.assertFalse(device.mic_enabled)
            self.assertFalse(self.server.resources)
            self.assertEqual(len(self.downloads), 1)
        finally:
            await live.close()


class RingBufferTests(unittest.IsolatedAsyncioTestCase):
    async def test_append_keeps_absolute_stream_offsets_with_bounded_memory(self):
        resource = AudioResource("test", max_bytes=100)
        await resource.append(b"a" * 80)
        await resource.append(b"b" * 80)
        self.assertEqual(resource.start_offset, 60)
        self.assertEqual(resource.total_bytes, 160)
        self.assertEqual(resource.data, b"a" * 20 + b"b" * 80)
