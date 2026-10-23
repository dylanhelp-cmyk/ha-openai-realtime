"""Play the original Realtime audio through a Home Assistant media player."""

import asyncio
from collections import deque
from contextlib import suppress
import logging
from pathlib import Path
import re
import time
from uuid import uuid4

import aiohttp
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    CancelFrame,
    EndFrame,
    Frame,
    InterruptionFrame,
    OutputAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    UserStartedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

logger = logging.getLogger(__name__)


class HomeAssistantPlayback:
    """Encode PCM as MP3 and play it using HA's authenticated media source.

    The add-on and Core share /media. No extra public HTTP server, firmware
    change, or separate TTS service is needed. Playback state keeps the device
    microphone gated until the reply ends; integrations that do not report
    announcement state use the audio duration plus a conservative start delay.
    """

    POLL_SECONDS = 0.25
    START_GRACE_SECONDS = 3.0
    MAX_FINISH_GRACE_SECONDS = 15.0
    FILE_RETENTION_SECONDS = 60.0

    def __init__(
        self,
        entity_id: str,
        token: str,
        announce: bool = True,
        api_url: str = "http://supervisor/core/api",
        media_dir: Path = Path("/media/openai_realtime_voice_agent"),
    ):
        if not re.fullmatch(r"media_player\.[a-z0-9_]+", entity_id):
            raise ValueError("tts_media_player must be a media_player entity ID")
        if not token:
            raise ValueError("Media-player output requires a Home Assistant API token")
        self.entity_id = entity_id
        self.announce = announce
        self.api_url = api_url.rstrip("/")
        self.media_dir = media_dir
        self._token = token
        self._session = None
        self._playback_requested = False
        self._files = set()
        self._deletions = set()
        self._wake_pcm = None

    async def _http(self):
        if self._session is None:
            self._session = aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=aiohttp.ClientTimeout(total=30, connect=5),
            )
        return self._session

    async def _request(self, method, path, **kwargs):
        session = await self._http()
        async with session.request(method, f"{self.api_url}/{path}", **kwargs) as response:
            if response.status >= 400:
                # Do not log signed media URLs, access tokens, or response bodies.
                raise RuntimeError(f"Home Assistant {method} {path}: HTTP {response.status}")
            return await response.json()

    async def validate(self):
        """Fail clearly on a bad target or missing media mount before listening."""
        self.media_dir.mkdir(parents=True, exist_ok=True)
        state = await self._request("GET", f"states/{self.entity_id}")
        if state.get("state") in {"unavailable", "unknown"}:
            logger.warning("TTS media player %s is %s", self.entity_id, state["state"])
        # Only remove this add-on's own abandoned reply files after a crash.
        for path in self.media_dir.glob("reply-*.mp3"):
            if path.stat().st_mtime < time.time() - 600:
                path.unlink(missing_ok=True)

    async def _encode(self, audio, sample_rate, num_channels):
        path = self.media_dir / f"reply-{uuid4().hex}.mp3"
        self._files.add(path)
        process = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "s16le", "-ar", str(sample_rate), "-ac", str(num_channels),
            "-i", "pipe:0", "-codec:a", "libmp3lame", "-b:a", "128k", str(path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            await asyncio.wait_for(process.communicate(audio), timeout=30)
            if process.returncode:
                raise RuntimeError("Could not encode Realtime reply as MP3")
            return path
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.wait()
            path.unlink(missing_ok=True)
            self._files.discard(path)
            raise

    def _delete_later(self, path):
        async def remove():
            try:
                await asyncio.sleep(self.FILE_RETENTION_SECONDS)
            finally:
                path.unlink(missing_ok=True)
                self._files.discard(path)

        task = asyncio.create_task(remove())
        self._deletions.add(task)
        task.add_done_callback(self._deletions.discard)

    async def play_wake_chime(self):
        if self._wake_pcm is None:
            asset = Path(__file__).parent / "assets" / "wake_word_triggered.flac"
            process = await asyncio.create_subprocess_exec(
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(asset),
                "-f", "s16le", "-ar", "24000", "-ac", "1", "pipe:1",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                audio, _ = await asyncio.wait_for(process.communicate(), 10)
                if process.returncode or not audio:
                    raise RuntimeError("Could not decode the wake chime")
                self._wake_pcm = audio
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
        await self.play(self._wake_pcm, 24000, 1, label="wake chime")

    async def play(self, audio: bytes, sample_rate: int, num_channels: int, label="reply"):
        path = await self._encode(audio, sample_rate, num_channels)
        duration = len(audio) / (sample_rate * num_channels * 2)
        try:
            baseline = await self._request("GET", f"states/{self.entity_id}")
            self._playback_requested = True  # includes a cancelled in-flight POST
            requested_at = time.monotonic()
            await self._request(
                "POST", "services/media_player/play_media",
                json={
                    "entity_id": self.entity_id,
                    "media_content_id": f"media-source://media_source/local/openai_realtime_voice_agent/{path.name}",
                    "media_content_type": "audio/mpeg",
                    "announce": self.announce,
                },
            )
            logger.info("Requested %s playback on %s (%.1fs)", label, self.entity_id, duration)
            await self._wait_until_finished(path.name, duration, baseline, requested_at)
        finally:
            # Keep a brief grace period for players that fetch the file late.
            self._delete_later(path)

    async def _wait_until_finished(self, filename, duration, baseline, requested_at=None):
        started_at = requested_at if requested_at is not None else time.monotonic()
        saw_playing = False
        matched_reply = False
        while True:
            await asyncio.sleep(self.POLL_SECONDS)
            elapsed = time.monotonic() - started_at
            try:
                state = await self._request("GET", f"states/{self.entity_id}",
                                            timeout=aiohttp.ClientTimeout(total=5))
            except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError):
                state = {}
            status = state.get("state")
            content_id = str(state.get("attributes", {}).get("media_content_id", ""))
            matches = filename in content_id
            if matches:
                matched_reply = True
            if status == "playing" and (matches or baseline.get("state") != "playing"):
                saw_playing = True
            if saw_playing and status in {"idle", "off", "paused", "unavailable"}:
                break
            # An announcement may restore the music that was playing before it.
            if matched_reply and content_id and not matches and status == "playing":
                break
            if not saw_playing and elapsed >= duration + self.START_GRACE_SECONDS:
                logger.info("Player did not expose reply playback state; using audio duration")
                break
            if elapsed >= duration + self.MAX_FINISH_GRACE_SECONDS:
                logger.warning("Player did not report reply completion; releasing microphone")
                break
        self._playback_requested = False

    async def stop(self):
        if not self._playback_requested:
            return  # do not stop unrelated music while the assistant is idle
        try:
            await self._request("POST", "services/media_player/media_stop",
                                json={"entity_id": self.entity_id},
                                timeout=aiohttp.ClientTimeout(total=5))
        except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as exc:
            logger.warning("Could not stop TTS player %s: %s", self.entity_id, exc)
        finally:
            self._playback_requested = False

    async def close(self):
        await self.stop()
        tasks = list(self._deletions)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for path in list(self._files):
            path.unlink(missing_ok=True)
        self._files.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None


class MediaPlayerOutput(FrameProcessor):
    """Consume speaker audio, queue external replies, and emit speaking frames.

    Place AFTER PhaseEmitter and the output recorder, BEFORE transport.output().
    Speaking frames travel upstream as well as downstream, just like Pipecat's
    normal output transport. This keeps LED/microphone/follow-up timing and the
    Realtime service's audio bookkeeping tied to external playback completion.
    """

    MAX_AUDIO_SECONDS = 300

    def __init__(self, playback: HomeAssistantPlayback, **kwargs):
        super().__init__(**kwargs)
        self.playback = playback
        self._audio = bytearray()
        self._format = None
        self._pending = deque()
        self._worker = None
        self._capturing = False
        self._speaking = False
        self._discard_audio = False

    def resume(self):
        """A genuine new user turn clears the post-stop audio gate."""
        self._discard_audio = False

    @property
    def is_active(self):
        return self._capturing or self._speaking or bool(self._pending)

    async def _speaking_started(self):
        if not self._speaking:
            self._speaking = True
            await self.broadcast_frame(BotStartedSpeakingFrame)

    async def _speaking_finished(self):
        if self._speaking:
            self._speaking = False
            await self.broadcast_frame(BotStoppedSpeakingFrame)

    async def interrupt(self):
        self._discard_audio = True
        self._audio.clear()
        self._format = None
        self._capturing = False
        self._pending.clear()
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
        await self.playback.stop()
        await self._speaking_finished()

    async def _play_pending(self):
        try:
            while self._pending:
                audio, rate, channels = self._pending.popleft()
                try:
                    await self.playback.play(audio, rate, channels)
                except Exception as exc:
                    logger.error("TTS media-player playback failed: %s", exc)
                    await self.playback.stop()
        finally:
            if not self._capturing and not self._pending:
                await self._speaking_finished()

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, (InterruptionFrame, CancelFrame, EndFrame)):
            await self.interrupt()
        elif isinstance(frame, UserStartedSpeakingFrame) and direction == FrameDirection.DOWNSTREAM:
            # Resume HERE, after this processor handled the preceding VAD
            # interruption. Resuming from an upstream PhaseEmitter callback
            # races the interruption while it is still queued on this stage,
            # leaving every following audio frame muted.
            self.resume()
        if direction == FrameDirection.DOWNSTREAM:
            if isinstance(frame, TTSStartedFrame):
                if not self._discard_audio:
                    self._capturing = True
                    await self._speaking_started()
                return
            if isinstance(frame, OutputAudioRawFrame):
                if not self._discard_audio:
                    audio_format = (frame.sample_rate, frame.num_channels)
                    if self._format is not None and self._format != audio_format:
                        await self.interrupt()
                        logger.error("TTS audio format changed within a reply; discarded")
                        return
                    self._format = audio_format
                    self._capturing = True
                    await self._speaking_started()
                    self._audio.extend(frame.audio)
                    if len(self._audio) > frame.sample_rate * frame.num_channels * 2 * self.MAX_AUDIO_SECONDS:
                        await self.interrupt()
                        logger.error("TTS reply exceeded the five-minute audio limit; discarded")
                return  # never send this audio to the Voice PE as well
            if isinstance(frame, TTSStoppedFrame):
                self._capturing = False
                if self._audio and not self._discard_audio:
                    self._pending.append((bytes(self._audio), *self._format))
                    self._audio.clear()
                    self._format = None
                    if self._worker is None or self._worker.done():
                        self._worker = asyncio.create_task(self._play_pending())
                elif not self._pending and (self._worker is None or self._worker.done()):
                    await self._speaking_finished()
                return
        await self.push_frame(frame, direction)

    async def cleanup(self):
        await self.interrupt()
        await self.playback.close()
        await super().cleanup()
