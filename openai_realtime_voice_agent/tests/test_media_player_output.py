"""Routing, turn timing, cancellation, and real MP3/HA HTTP contract checks.

Run from the add-on directory: python -m unittest discover -s tests -v
"""

import asyncio
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock

from aiohttp import web
from pipecat.frames.frames import (
    BotStartedSpeakingFrame, BotStoppedSpeakingFrame, OutputAudioRawFrame,
    TTSStartedFrame, TTSStoppedFrame, TTSTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from app.media_player_output import HomeAssistantPlayback, MediaPlayerOutput
from app.phase_emitter import PhaseEmitter

DOWN = FrameDirection.DOWNSTREAM
UP = FrameDirection.UPSTREAM


class ControlledPlayback:
    def __init__(self):
        self.calls = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.stopped = 0

    async def play(self, audio, rate, channels):
        self.calls.append((audio, rate, channels))
        self.started.set()
        await self.release.wait()

    async def stop(self):
        self.stopped += 1

    async def close(self):
        pass


class CapturePhase(PhaseEmitter):
    async def push_frame(self, frame, direction=DOWN):
        pass


class CaptureOutput(MediaPlayerOutput):
    def __init__(self, playback, phase=None):
        super().__init__(playback)
        self.frames = []
        self.phase = phase

    async def push_frame(self, frame, direction=DOWN):
        self.frames.append((frame, direction))
        if direction == UP and self.phase is not None:
            await self.phase.process_frame(frame, direction)


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playback = ControlledPlayback()
        self.output = CaptureOutput(self.playback)

    async def asyncTearDown(self):
        await self.output.interrupt()

    async def send_audio(self, audio=b"\0\0" * 240):
        await self.output.process_frame(TTSStartedFrame(), DOWN)
        await self.output.process_frame(OutputAudioRawFrame(audio, 24000, 1), DOWN)
        await self.output.process_frame(TTSStoppedFrame(), DOWN)
        await asyncio.wait_for(self.playback.started.wait(), 1)

    async def test_original_audio_is_preserved_without_sending_to_device(self):
        audio = bytes(range(256)) * 2
        await self.send_audio(audio)
        self.assertEqual(self.playback.calls, [(audio, 24000, 1)])
        self.assertFalse(any(isinstance(f, OutputAudioRawFrame) for f, _ in self.output.frames))
        self.assertEqual(sum(isinstance(f, BotStartedSpeakingFrame) and d == UP
                             for f, d in self.output.frames), 1)
        self.assertFalse(any(isinstance(f, BotStoppedSpeakingFrame) for f, _ in self.output.frames))
        self.playback.release.set()
        await self.output._worker
        self.assertEqual(sum(isinstance(f, BotStoppedSpeakingFrame) and d == UP
                             for f, d in self.output.frames), 1)

    async def test_phase_stays_replying_until_external_playback_ends(self):
        phases = []

        async def emit(value):
            phases.append(value)

        phase = CapturePhase(emit, idle_debounce_s=0.01)
        self.output.phase = phase
        await self.send_audio()
        await asyncio.sleep(0.02)
        self.assertEqual(phases, ["replying"])
        self.playback.release.set()
        await self.output._worker
        await asyncio.sleep(0.02)
        self.assertEqual(phases, ["replying", "idle"])
        self.output.phase = None
        phase._cancel_pending_idle()

    async def test_segments_are_queued_in_order_without_premature_stop(self):
        await self.send_audio(b"\1\0" * 240)
        await self.send_audio(b"\2\0" * 240)
        self.assertEqual(len(self.playback.calls), 1)
        self.playback.release.set()
        await self.output._worker
        self.assertEqual([x[0][:2] for x in self.playback.calls], [b"\1\0", b"\2\0"])
        self.assertEqual(sum(isinstance(f, BotStoppedSpeakingFrame) and d == UP
                             for f, d in self.output.frames), 1)

    async def test_stop_discards_queued_and_late_audio_until_new_user_turn(self):
        await self.send_audio()
        await self.send_audio()
        await self.output.interrupt()
        self.assertGreaterEqual(self.playback.stopped, 1)
        self.assertEqual(len(self.output._pending), 0)
        await self.output.process_frame(TTSStartedFrame(), DOWN)
        await self.output.process_frame(OutputAudioRawFrame(b"\0\0", 24000, 1), DOWN)
        await self.output.process_frame(TTSStoppedFrame(), DOWN)
        self.assertEqual(len(self.playback.calls), 1)
        self.output.resume()
        self.playback.release.set()
        self.playback.started.clear()
        await self.send_audio()
        await self.output._worker
        self.assertEqual(len(self.playback.calls), 2)

    async def test_stop_during_generation_never_plays_partial_reply(self):
        await self.output.process_frame(TTSStartedFrame(), DOWN)
        await self.output.process_frame(OutputAudioRawFrame(b"\0\0", 24000, 1), DOWN)
        await self.output.interrupt()
        await self.output.process_frame(TTSStoppedFrame(), DOWN)
        self.assertEqual(self.playback.calls, [])

    async def test_transcript_frames_continue_through_pipeline(self):
        frame = TTSTextFrame("The lights are on", aggregated_by="sentence")
        await self.output.process_frame(frame, DOWN)
        self.assertIn((frame, DOWN), self.output.frames)

    async def test_playback_failure_does_not_leave_device_replying(self):
        async def fail(*args):
            raise RuntimeError("player unavailable")

        self.playback.play = fail
        await self.output.process_frame(TTSStartedFrame(), DOWN)
        await self.output.process_frame(OutputAudioRawFrame(b"\0\0", 24000, 1), DOWN)
        await self.output.process_frame(TTSStoppedFrame(), DOWN)
        await self.output._worker
        self.assertFalse(self.output._speaking)
        self.assertTrue(any(isinstance(f, BotStoppedSpeakingFrame) for f, _ in self.output.frames))


class PlaybackHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.media_dir = Path(self.tmp.name)
        self.state = {"state": "idle", "attributes": {}}
        self.calls = []

        async def handle(request):
            self.assertEqual(request.headers.get("Authorization"), "Bearer test-token")
            if request.method == "GET":
                return web.json_response(self.state)
            data = await request.json()
            self.calls.append((request.path, data))
            if request.path.endswith("play_media"):
                filename = data["media_content_id"].rsplit("/", 1)[1]
                self.assertTrue((self.media_dir / filename).is_file())
                self.state = {"state": "playing", "attributes": {"media_content_id": data["media_content_id"]}}
            elif request.path.endswith("media_stop"):
                self.state = {"state": "idle", "attributes": {}}
            return web.json_response([])

        app = web.Application()
        app.router.add_route("*", "/api/{tail:.*}", handle)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        port = self.site._server.sockets[0].getsockname()[1]
        self.playback = HomeAssistantPlayback(
            "media_player.kitchen", "test-token", media_dir=self.media_dir,
            api_url=f"http://127.0.0.1:{port}/api",
        )
        self.playback.POLL_SECONDS = 0.01
        self.playback.FILE_RETENTION_SECONDS = 0.03

    async def asyncTearDown(self):
        await self.playback.close()
        await self.runner.cleanup()
        self.tmp.cleanup()

    async def test_real_mp3_encoding_and_home_assistant_playback_contract(self):
        await self.playback.validate()
        task = asyncio.create_task(self.playback.play(b"\0\0" * 2400, 24000, 1))
        for _ in range(200):
            if self.calls:
                break
            await asyncio.sleep(0.01)
        self.assertTrue(self.calls)
        path, data = self.calls[0]
        self.assertEqual(path, "/api/services/media_player/play_media")
        self.assertEqual(data["entity_id"], "media_player.kitchen")
        self.assertEqual(data["media_content_type"], "audio/mpeg")
        self.assertTrue(data["announce"])
        self.assertTrue(data["media_content_id"].startswith(
            "media-source://media_source/local/openai_realtime_voice_agent/reply-"))
        filename = data["media_content_id"].rsplit("/", 1)[1]
        encoded = self.media_dir / filename
        probe = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "stream=codec_name,sample_rate,channels",
            "-of", "json", str(encoded), stdout=asyncio.subprocess.PIPE)
        stdout, _ = await probe.communicate()
        self.assertIn(b'"codec_name": "mp3"', stdout)
        self.assertIn(b'"sample_rate": "24000"', stdout)
        await asyncio.sleep(0.03)
        self.assertFalse(task.done(), "must wait for actual player completion")
        self.state = {"state": "idle", "attributes": {}}
        await asyncio.wait_for(task, 1)
        await asyncio.sleep(0.05)
        self.assertFalse(encoded.exists(), "temporary reply should be cleaned up")

    async def test_stop_only_targets_our_active_reply(self):
        await self.playback.stop()
        self.assertEqual(self.calls, [])
        self.playback._playback_requested = True
        await self.playback.stop()
        self.assertEqual(self.calls, [("/api/services/media_player/media_stop", {"entity_id": "media_player.kitchen"})])

    async def test_original_wake_chime_decodes_to_non_silent_pcm_and_uses_same_player(self):
        self.playback.play = AsyncMock()
        await self.playback.play_wake_chime()
        args = self.playback.play.call_args
        audio, rate, channels = args.args
        self.assertEqual((rate, channels), (24000, 1))
        self.assertGreater(len(audio), 40000)
        self.assertNotEqual(audio, bytes(len(audio)))
        self.assertEqual(args.kwargs, {"label": "wake chime"})

    async def test_unreported_announcement_uses_duration_guard(self):
        self.playback.START_GRACE_SECONDS = 0.02
        started = time.monotonic()
        await self.playback._wait_until_finished("not-reported.mp3", 0.03, {"state": "playing"})
        self.assertGreaterEqual(time.monotonic() - started, 0.05)

    async def test_blocking_play_service_time_counts_toward_duration_guard(self):
        self.playback.START_GRACE_SECONDS = 0.02
        started = time.monotonic()
        await self.playback._wait_until_finished("not-reported.mp3", 0.03,
                                                {"state": "playing"}, started - 10)
        self.assertLess(time.monotonic() - started, 0.05)

    async def test_resume_of_background_music_marks_announcement_complete(self):
        self.state = {"state": "playing", "attributes": {"media_content_id": "reply-test.mp3"}}
        task = asyncio.create_task(self.playback._wait_until_finished("reply-test.mp3", 60, {"state": "playing"}))
        await asyncio.sleep(0.03)
        self.state = {"state": "playing", "attributes": {"media_content_id": "previous-music"}}
        await asyncio.wait_for(task, 1)

    async def test_invalid_entity_has_clear_error(self):
        with self.assertRaisesRegex(ValueError, "media_player entity ID"):
            HomeAssistantPlayback("sensor.temperature", "test-token")


if __name__ == "__main__":
    unittest.main()
