import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
import numpy as np
from app.device import Device
from app.diagnostic_audio import MP3Loopback, measure_loopback
from app.live_audio import AudioHTTPServer
from app.playback_diagnostic import PlaybackDiagnosticSession
from app.settings import Settings
from test_echo import probe16, records
from app.calibration import calibration_probe
from test_live import Socket


class LoopbackTimingTests(unittest.TestCase):
    def test_decoder_media_offset_is_separate_from_chunk_delivery_time(self):
        probe = probe16()
        reference = np.concatenate((np.zeros(8000), probe, np.zeros(16000)))
        decoded = np.concatenate((np.zeros(3200), probe * 0.6, np.zeros(16000)))
        # Decoder joined 300 ms later in the stream; 90 ms processing plus
        # a completed 20 ms block gives about 110 ms measured availability.
        chunks = [(stamp + 0.3 + 0.09 + 0.02, data) for stamp, data in records(decoded)]
        result = measure_loopback(
            calibration_probe(0.2), records(reference), chunks, 0.35
        )
        self.assertAlmostEqual(result["delay_ms"], 110, delta=20)
        self.assertLessEqual(result["spread_ms"], 20)

    def test_decoder_stalls_are_reported_instead_of_hidden_by_a_fake_audio_clock(self):
        audio = np.concatenate((np.zeros(8000), probe16(), np.zeros(16000)))
        chunks = [
            (stamp + 0.1 + index * 0.002, data)
            for index, (stamp, data) in enumerate(records(audio))
        ]
        with self.assertRaisesRegex(ValueError, "Unstable"):
            measure_loopback(calibration_probe(0.2), records(audio), chunks, 0.35)


class PlaybackDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_during_decoder_creation_reaps_the_eventual_process(self):
        entered, release = asyncio.Event(), asyncio.Event()
        process = SimpleNamespace(returncode=None, wait=AsyncMock())
        process.kill = Mock(side_effect=lambda: setattr(process, "returncode", -9))

        async def spawn(*args, **kwargs):
            entered.set()
            await release.wait()
            return process

        decoder = MP3Loopback(None, None, None)
        with patch(
            "app.diagnostic_audio.asyncio.create_subprocess_exec", side_effect=spawn
        ):
            task = asyncio.create_task(decoder.start())
            await asyncio.wait_for(entered.wait(), 1)
            await decoder.stop()
            task.cancel()
            release.set()
            await asyncio.gather(task, return_exceptions=True)
        process.kill.assert_called_once()
        process.wait.assert_awaited_once()
        self.assertFalse(decoder.tasks)

    async def test_diagnostic_routes_once_then_next_wake_is_a_normal_conversation(self):
        from aiohttp.test_utils import TestServer
        from app.main import VoiceBridge

        settings = Settings(
            openai_api_key="fake", playback_diagnostic_on_next_wake=True
        )
        bridge = VoiceBridge(settings)

        class Session:
            def __init__(self, _, device, *args, **kwargs):
                self.device, self.closed = device, False
                self.run = AsyncMock()

            async def close(self):
                self.closed = True

        with (
            patch("app.main.HomeTools.initialize", new=AsyncMock()),
            patch(
                "app.main.PlaybackDiagnosticSession", side_effect=Session
            ) as diagnostic,
            patch("app.main.LiveSession", side_effect=Session) as live,
        ):
            async with TestServer(bridge.application()) as server:
                async with aiohttp.ClientSession() as http:
                    async with http.ws_connect(server.make_url("/")) as socket:
                        await socket.receive_json()
                        await socket.send_json({"type": "wake", "wake_id": 1})
                        async with asyncio.timeout(1):
                            while diagnostic.call_count != 1:
                                await asyncio.sleep(0.01)
                        live.assert_not_called()
                        await socket.send_json({"type": "wake", "wake_id": 2})
                        async with asyncio.timeout(1):
                            while live.call_count != 1:
                                await asyncio.sleep(0.01)
                        self.assertEqual(diagnostic.call_count, 1)

    async def test_comparison_keeps_both_trials_in_one_device_session_and_never_saves(
        self,
    ):
        settings = Settings(tts_output="media_player")
        socket = Socket()
        device = Device(socket, settings)
        store = SimpleNamespace(save=AsyncMock())
        trials = []

        class Trial:
            def __init__(self, config, dev, *args, **kwargs):
                trials.append(self)
                self.config, self.kwargs = config, kwargs
                self.result = (
                    {"delay_ms": 3020} if config.tts_output == "media_player" else None
                )
                self.loopback_result = {"delay_ms": 120}
                self.close = AsyncMock()
                self.receive_audio = lambda audio: None

            async def run(self):
                await device.mic(True)
                await device.mic(False)

        with (
            patch("app.playback_diagnostic.CalibrationSession", Trial),
            self.assertLogs("app.playback_diagnostic", "INFO") as logs,
        ):
            session = PlaybackDiagnosticSession(
                settings, device, None, None, "fake", store
            )
            await session.run()
        self.assertEqual(
            [t.config.tts_output for t in trials], ["media_player", "voice_pe"]
        )
        self.assertTrue(
            all(
                t.kwargs["save_result"] is False and t.kwargs["end_device"] is False
                for t in trials
            )
        )
        self.assertTrue(trials[0].kwargs["software_loopback"])
        self.assertFalse(trials[1].kwargs["software_loopback"])
        self.assertEqual(sum(e["type"] == "session_end" for e in socket.events), 1)
        self.assertIn("external_minus_software_ms=2900", "\n".join(logs.output))
        self.assertIn("voice_pe_roundtrip_ms=unavailable", "\n".join(logs.output))
        store.save.assert_not_called()
        self.assertTrue(session.closed)

    async def test_stop_during_external_trial_cannot_start_the_local_speaker(self):
        started, released = asyncio.Event(), asyncio.Event()
        device = Device(Socket(), Settings())
        trial = SimpleNamespace(
            result=None,
            loopback_result=None,
            close=AsyncMock(),
            receive_audio=lambda _: None,
        )

        async def run():
            started.set()
            await released.wait()

        trial.run = run
        with patch(
            "app.playback_diagnostic.CalibrationSession", return_value=trial
        ) as factory:
            session = PlaybackDiagnosticSession(
                Settings(), device, None, None, "fake", None
            )
            task = asyncio.create_task(session.run())
            await asyncio.wait_for(started.wait(), 1)
            await session.close()
            released.set()
            await asyncio.wait_for(task, 1)
        self.assertEqual(factory.call_count, 1)
        self.assertTrue(session.closed)

    async def test_internal_observer_cannot_claim_or_hold_the_real_speaker_fetch(self):
        server = AudioHTTPServer("http://127.0.0.1:0", 0, "127.0.0.1")
        await server.start()
        resource = server.create()
        await resource.append(b"test data")
        try:
            async with aiohttp.ClientSession() as http:
                async with http.get(
                    server.url(resource), headers={"X-Playback-Diagnostic": "loopback"}
                ) as response:
                    self.assertEqual(
                        await response.content.readexactly(9), b"test data"
                    )
                    self.assertFalse(resource.fetched.is_set())
                    self.assertEqual(resource.readers, 0)
                    self.assertIsNone(resource.last_reader_left_at)
                    await resource.finish()
                    await response.read()
        finally:
            await server.close()

    async def test_arming_modes_are_mutually_exclusive_and_external_only(self):
        for settings in (
            Settings(openai_api_key="fake", playback_diagnostic_on_next_wake=True),
            Settings(
                openai_api_key="fake",
                tts_output="media_player",
                tts_media_player="media_player.test",
                tts_stream_base_url="http://localhost:18082",
                aec_calibrate_on_next_wake=True,
                playback_diagnostic_on_next_wake=True,
            ),
        ):
            with self.assertRaises(ValueError):
                settings.validate()
