"""Real HTTP clients and ffmpeg: progressive delivery, fresh URLs, timing, stop."""

import asyncio
from pathlib import Path
import tempfile
import time
import unittest

import aiohttp
from aiohttp import web
from pipecat.frames.frames import OutputAudioRawFrame, TTSStartedFrame, TTSStoppedFrame
from pipecat.processors.frame_processor import FrameDirection

from app.live_audio import AudioHTTPServer, LiveMP3
from app.media_player_output import HomeAssistantPlayback, MediaPlayerOutput


class LiveHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = AudioHTTPServer("http://127.0.0.1:0", 0, "127.0.0.1")
        await self.server.start()
        self.client = aiohttp.ClientSession()

    async def asyncTearDown(self):
        await self.client.close()
        await self.server.close()

    async def test_fresh_paths_and_wiim_cache_parameter_do_not_repeat_audio(self):
        first, second = self.server.create(), self.server.create()
        for resource, data in [(first, b"first reply"), (second, b"second reply")]:
            await resource.append(data)
            await resource.finish()
        self.assertNotEqual(self.server.url(first), self.server.url(second))
        async with self.client.head(self.server.url(second)) as response:
            self.assertEqual(response.status, 200)
        self.assertFalse(second.fetched.is_set(), "HEAD probes cannot start the playback clock")
        async with self.client.get(self.server.url(second) + "?_=wiim-cache-buster") as response:
            self.assertEqual(await response.read(), b"second reply")
            self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertTrue(second.fetched.is_set())
        await self.server.remove(second)
        async with self.client.get(self.server.url(second)) as response:
            self.assertEqual(response.status, 404)

    async def test_mp3_bytes_arrive_before_pcm_input_finishes(self):
        stream = await LiveMP3(self.server, 24000, 1, 100).start()
        try:
            await stream.write(b"\x80\0" * 24000)
            await asyncio.wait_for(stream.ready(), 3)
            self.assertFalse(stream.resource.finished.is_set())
            async with self.client.get(self.server.url(stream.resource)) as response:
                first = await asyncio.wait_for(response.content.read(512), 2)
                self.assertTrue(first)
                self.assertFalse(stream.resource.finished.is_set())
                await stream.write(b"\x40\0" * 2400)
                await stream.finish()
                self.assertTrue(await response.read())
            self.assertAlmostEqual(stream.duration, 1.1)
            self.assertEqual(stream.process.returncode, 0)
        finally:
            await stream.cancel()

    async def test_one_byte_probe_does_not_open_the_microphone_clock(self):
        resource = self.server.create()
        await resource.append(b"MP3 audio")
        async with self.client.get(self.server.url(resource), headers={"Range": "bytes=0-0"}) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(await response.read(), b"M")
            self.assertEqual(response.headers["Content-Range"], "bytes 0-0/*")
        self.assertFalse(resource.fetched.is_set())
        await resource.finish()
        async with self.client.get(self.server.url(resource), headers={"Range": "bytes=0-"}) as response:
            self.assertEqual(await response.read(), b"MP3 audio")
        self.assertTrue(resource.fetched.is_set())

    async def test_cancel_ends_encoder_and_waiting_http_consumer(self):
        stream = await LiveMP3(self.server, 24000, 1).start()
        await stream.write(b"\1\0" * 24000)
        await asyncio.wait_for(stream.ready(), 3)
        async with self.client.get(self.server.url(stream.resource)) as response:
            await response.content.read(128)
            await stream.cancel()
            await asyncio.wait_for(response.read(), 2)
        self.assertIsNotNone(stream.process.returncode)

    async def test_previous_assistant_audio_is_never_restored_as_music(self):
        playback = HomeAssistantPlayback("media_player.test", "token", announce=True,
                                         audio_server=self.server)
        old = self.server.create()
        self.assertFalse(playback._announcement_for({"attributes": {
            "media_title": self.server.url(old) + "?_=last-announce"}}))
        self.assertTrue(playback._announcement_for({"attributes": {
            "media_content_id": "http://radio.example/music.mp3"}}))

    async def test_microphone_deadline_starts_at_the_real_fetch_with_adjustable_allowance(self):
        playback = HomeAssistantPlayback("media_player.test", "token", audio_server=self.server,
                                         completion_mode="auto", start_allowance_ms=30)
        resource = self.server.create()
        resource.first_fetch_at = time.monotonic() - 1
        resource.fetched.set()
        started = time.monotonic()
        await playback._wait_until_finished(resource.name, 0.1, {}, started, resource)
        self.assertLess(time.monotonic() - started, 0.1, "already played audio must not wait again")

        resource.first_fetch_at = time.monotonic()
        started = time.monotonic()
        await playback._wait_until_finished(resource.name, 0.03, {}, started, resource)
        self.assertGreaterEqual(time.monotonic() - started, 0.06)
        self.assertLess(time.monotonic() - started, 0.2)


class LivePipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_player_is_requested_before_tts_stops_and_cached_state_does_not_delay_finish(self):
        server = AudioHTTPServer("http://127.0.0.1:0", 0, "127.0.0.1")
        await server.start()
        received = asyncio.Event()
        downloads, requests = [], []
        client = aiohttp.ClientSession()

        async def fetch(url):
            async with client.get(url) as response:
                await response.content.read(128)
                received.set()
                return await response.read()

        async def ha(request):
            if request.method == "POST":
                data = await request.json()
                requests.append(data)
                if request.path.endswith("play_media"):
                    downloads.append(asyncio.create_task(fetch(data["media_content_id"])))
                return web.json_response([])
            # Deliberately stale HA state: a device polling interval must not
            # add five seconds to a one-second reply's microphone handover.
            return web.json_response({"state": "idle", "attributes": {}})

        app = web.Application()
        app.router.add_route("*", "/api/{tail:.*}", ha)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        with tempfile.TemporaryDirectory() as tmp:
            playback = HomeAssistantPlayback(
                "media_player.test", "token", media_dir=Path(tmp), audio_server=server,
                delivery="stream", start_allowance_ms=20,
                api_url=f"http://127.0.0.1:{port}/api")
            output = MediaPlayerOutput(playback)
            # Capture only the output transport boundary for this contract test.
            async def push(*args):
                pass
            output.push_frame = push
            down = FrameDirection.DOWNSTREAM
            try:
                await output.process_frame(TTSStartedFrame(), down)
                await output.process_frame(OutputAudioRawFrame(b"\1\0" * 24000, 24000, 1), down)
                await asyncio.wait_for(received.wait(), 3)
                self.assertTrue(output._capturing)
                self.assertIsNotNone(output._live_stream)
                self.assertFalse(output._live_stream.input_finished)
                started = time.monotonic()
                await output.process_frame(TTSStoppedFrame(), down)
                await asyncio.wait_for(output._worker, 2)
                self.assertLess(time.monotonic() - started, 1.5)
                self.assertFalse(output._speaking)
                self.assertEqual(len(requests), 1)
                self.assertNotIn("authSig", requests[0]["media_content_id"])
            finally:
                await output.interrupt()
                await playback.close()
                await asyncio.gather(*downloads, return_exceptions=True)
        await client.close()
        await runner.cleanup()


if __name__ == "__main__":
    unittest.main()
