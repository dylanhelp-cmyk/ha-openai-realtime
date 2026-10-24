"""Calibration exercises the real HTTP stream, captures locally and saves only success."""

import asyncio
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiohttp
import numpy as np
from app.calibration import CalibrationSession
from app.device import Device
from app.echo import CalibrationStore, pcm
from app.live_audio import AudioHTTPServer
from app.settings import Settings
from test_live import Socket, until


class CalibrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.store = CalibrationStore(Path(self.folder.name) / "calibration.json")
        self.server = AudioHTTPServer("http://127.0.0.1:0", 0, "127.0.0.1")
        await self.server.start()
        self.http = aiohttp.ClientSession()
        self.settings = Settings(
            openai_api_key="fake",
            tts_output="media_player",
            tts_media_player="media_player.test",
            microphone_mode="gated",
            tts_stream_prebuffer_ms=40,
            tts_start_allowance_ms=0,
            aec_calibration_max_delay_ms=200,
        )
        self.socket = Socket()
        self.device = Device(self.socket, self.settings)
        self.device.identity = "test-device"
        await self.device.mic(True)
        self.calibration = CalibrationSession(
            self.settings, self.device, self.http, self.server, "fake", self.store
        )
        self.downloads = []
        self.requests = []
        self.timers = []
        self.noise = False
        self.rng = np.random.default_rng(55)
        self.calibration.output._ha = self.ha
        original = self.calibration.reference

        def reference(frame, stamp):
            original(frame, stamp)
            returned = self.calibration.reference_frames[-1][1]
            if self.noise:
                returned = self.rng.normal(0, 200, len(returned))
            else:
                returned = returned * 0.6
            # Return each rendered 20 ms frame after 120 ms of speaker delay,
            # with receive_audio called at the end of the captured block.
            self.timers.append(
                asyncio.get_running_loop().call_later(
                    0.14, self.calibration.receive_audio, pcm(returned)
                )
            )

        self.calibration.output.on_reference = reference

    async def ha(self, method, path, **kwargs):
        if method == "GET":
            return {"attributes": {}}
        self.requests.append(path)
        if path.endswith("play_media"):

            async def download():
                async with self.http.get(
                    kwargs["json"]["media_content_id"] + "?_=wiim"
                ) as response:
                    self.assertEqual(response.status, 200)
                    await response.read()

            self.downloads.append(asyncio.create_task(download()))
        return []

    async def asyncTearDown(self):
        await self.calibration.close()
        for timer in self.timers:
            timer.cancel()
        await asyncio.gather(*self.downloads, return_exceptions=True)
        await self.http.close()
        await self.server.close()
        self.folder.cleanup()

    async def test_measures_delay_over_continuous_http_without_openai(self):
        self.http.ws_connect = AsyncMock(
            side_effect=AssertionError("Calibration must stay local")
        )
        with self.assertLogs("app.calibration", level="INFO") as logs:
            await asyncio.wait_for(self.calibration.run(), 8)
        delay = self.store.load(self.settings, self.device)
        self.assertIsNotNone(delay, "\n".join(logs.output))
        self.assertAlmostEqual(delay, 120.0, delta=8)
        self.http.ws_connect.assert_not_awaited()
        self.assertEqual(sum(path.endswith("play_media") for path in self.requests), 1)
        self.assertTrue(any("SAVED" in line for line in logs.output))
        self.assertFalse(self.device.mic_enabled)
        self.assertFalse(self.server.resources)
        self.assertFalse(self.calibration.reference_frames)
        self.assertFalse(self.calibration.mic_frames)

    async def test_bad_capture_leaves_previous_offset_unchanged(self):
        self.store.save(
            self.settings, self.device, {"delay_ms": 111.0, "confidence": 0.9}
        )
        self.noise = True
        with self.assertLogs("app.calibration", level="WARNING") as logs:
            await asyncio.wait_for(self.calibration.run(), 8)
        self.assertEqual(self.store.load(self.settings, self.device), 111.0)
        self.assertTrue(any("NOT saved" in line for line in logs.output))
        self.assertTrue(any(event["type"] == "error" for event in self.socket.events))

    async def test_diagnostic_decodes_the_real_http_mp3_without_overwriting_aec(self):
        self.store.save(self.settings, self.device, {"delay_ms": 3020.0})
        self.calibration.save_result = False
        self.calibration.end_device = False
        self.calibration.software_loopback = True
        self.http.ws_connect = AsyncMock(side_effect=AssertionError("No OpenAI"))
        with self.assertLogs("app.calibration", "INFO") as logs:
            await asyncio.wait_for(self.calibration.run(), 12)
        self.assertIsNotNone(self.calibration.result, "\n".join(logs.output))
        self.assertIsNotNone(self.calibration.loopback_result, "\n".join(logs.output))
        self.assertAlmostEqual(self.calibration.result["delay_ms"], 120, delta=8)
        self.assertLess(self.calibration.loopback_result["delay_ms"], 500)
        self.assertEqual(self.store.load(self.settings, self.device), 3020.0)
        self.http.ws_connect.assert_not_awaited()
        self.assertEqual(sum(path.endswith("play_media") for path in self.requests), 1)
        self.assertFalse(any(e["type"] == "session_end" for e in self.socket.events))
        self.assertIsNotNone(self.calibration.loopback.process.returncode)
        self.assertFalse(self.server.resources)

    async def test_local_pcm_reference_captures_mic_without_ending_between_trials(self):
        await self.calibration.close()
        self.store.save(self.settings, self.device, {"delay_ms": 3020.0})
        self.socket.events.clear()
        self.calibration = CalibrationSession(
            replace(self.settings, tts_output="voice_pe"),
            self.device,
            self.http,
            None,
            "fake",
            self.store,
            save_result=False,
            end_device=False,
        )
        original = self.calibration.reference

        def reference(frame, stamp):
            original(frame, stamp)
            returned = self.calibration.reference_frames[-1][1] * 0.6
            self.timers.append(
                asyncio.get_running_loop().call_later(
                    0.08, self.calibration.receive_audio, pcm(returned)
                )
            )

        self.calibration.output.on_reference = reference
        with self.assertLogs("app.calibration", "INFO") as logs:
            await asyncio.wait_for(self.calibration.run(), 8)
        self.assertIsNotNone(self.calibration.result, "\n".join(logs.output))
        self.assertAlmostEqual(self.calibration.result["delay_ms"], 60, delta=8)
        self.assertTrue(self.socket.audio)
        self.assertFalse(any(e["type"] == "session_end" for e in self.socket.events))
        self.assertEqual(self.store.load(self.settings, self.device), 3020.0)

    async def test_stop_discards_capture_and_never_saves_a_late_result(self):
        runner = asyncio.create_task(self.calibration.run())
        await until(lambda: self.calibration.output.requested)
        await self.calibration.close()
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
        self.assertIsNone(self.store.load(self.settings, self.device))
        self.assertFalse(self.server.resources)
        self.assertFalse(self.device.mic_enabled)

    async def test_speaker_failure_does_not_save_a_measurement(self):
        self.calibration.output._ha = AsyncMock(side_effect=RuntimeError("offline"))
        with self.assertLogs("app.calibration", level="WARNING"):
            await asyncio.wait_for(self.calibration.run(), 3)
        self.assertFalse(self.store.path.exists())
        self.assertFalse(self.device.mic_enabled)

    async def test_stop_while_measurement_is_computing_cannot_save_later(self):
        import threading

        started, release = threading.Event(), threading.Event()

        def measuring(*args):
            started.set()
            release.wait(5)
            return {"delay_ms": 120.0, "confidence": 0.99, "peak_ratio": 5.0}

        with patch("app.calibration.measure_delay", side_effect=measuring):
            runner = asyncio.create_task(self.calibration.run())
            try:
                async with asyncio.timeout(8):
                    while not started.is_set():
                        await asyncio.sleep(0.02)
                await self.calibration.close()
                release.set()
                await asyncio.wait_for(runner, 3)
                self.assertIsNone(self.store.load(self.settings, self.device))
            finally:
                release.set()
                runner.cancel()
                await asyncio.gather(runner, return_exceptions=True)


class CalibrationRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_calibration_is_selected_once_then_normal_live_conversations_resume(
        self,
    ):
        from aiohttp.test_utils import TestServer
        from app.main import VoiceBridge

        settings = Settings(openai_api_key="fake", aec_calibrate_on_next_wake=True)
        bridge = VoiceBridge(settings)

        class Session:
            def __init__(self, _, device, *args, **kwargs):
                self.device = device
                self.closed = False
                self.run = AsyncMock()

            async def close(self):
                self.closed = True

        with (
            patch("app.main.HomeTools.initialize", new=AsyncMock()),
            patch("app.main.CalibrationSession", side_effect=Session) as calibrate,
            patch("app.main.LiveSession", side_effect=Session) as live,
        ):
            async with TestServer(bridge.application()) as server:
                async with aiohttp.ClientSession() as http:
                    async with http.ws_connect(server.make_url("/")) as socket:
                        await socket.receive_json()
                        await socket.send_json({"type": "wake", "wake_id": 1})
                        await until(lambda: calibrate.call_count == 1)
                        live.assert_not_called()
                        await socket.send_json({"type": "wake", "wake_id": 2})
                        await until(lambda: live.call_count == 1)
                        self.assertEqual(calibrate.call_count, 1)
