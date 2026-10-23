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
from app.live_audio import LiveMP3
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
    """Play Realtime audio through HA, using temporary HTTP or media-source URLs.

    With Audio HTTP configured, the real speaker fetch anchors completion
    timing. Otherwise the shared /media path and bounded HA state polling are
    used. Both paths preserve the original OpenAI voice.
    """

    POLL_SECONDS = 0.25
    START_GRACE_SECONDS = 0.3
    MAX_FINISH_GRACE_SECONDS = 1.5
    FILE_RETENTION_SECONDS = 60.0

    def __init__(
        self,
        entity_id: str,
        token: str,
        announce: bool = True,
        api_url: str = "http://supervisor/core/api",
        media_dir: Path = Path("/media/openai_realtime_voice_agent"),
        audio_server=None,
        delivery="buffered",
        completion_mode="auto",
        start_allowance_ms=300,
        finish_grace_ms=1500,
        state_poll_ms=100,
        state_timeout_ms=500,
        stream_prebuffer_ms=120,
        stream_start_timeout_s=10,
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
        if delivery not in {"buffered", "stream"} or completion_mode not in {"auto", "duration", "state"}:
            raise ValueError("Invalid media delivery or completion mode")
        if delivery == "stream" and audio_server is None:
            raise ValueError("Streaming requires tts_stream_base_url")
        self.audio_server = audio_server
        self.delivery = delivery
        self.completion_mode = completion_mode
        self.START_GRACE_SECONDS = max(0, start_allowance_ms) / 1000
        self.MAX_FINISH_GRACE_SECONDS = max(0, finish_grace_ms) / 1000
        self.POLL_SECONDS = max(20, state_poll_ms) / 1000
        self.state_timeout_s = max(100, state_timeout_ms) / 1000
        self.stream_prebuffer_ms = stream_prebuffer_ms
        self.stream_start_timeout_s = stream_start_timeout_s
        self._resources = set()
        self._streams = set()
        self._retiring_resources = set()

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

    def _delete_resource_later(self, resource):
        if resource in self._retiring_resources:
            return
        self._retiring_resources.add(resource)
        async def remove():
            try:
                await asyncio.sleep(self.FILE_RETENTION_SECONDS)
            finally:
                await self.audio_server.remove(resource)
                self._resources.discard(resource)
                self._retiring_resources.discard(resource)
        task = asyncio.create_task(remove())
        self._deletions.add(task)
        task.add_done_callback(self._deletions.discard)

    def _announcement_for(self, baseline):
        # Resuming a previous assistant URL can replay yesterday's/last turn's
        # message. Only unrelated music should be restored after announcements.
        attrs = baseline.get("attributes", {})
        previous = str(attrs.get("media_content_id") or attrs.get("media_title") or "")
        own_prefixes = ["media-source://media_source/local/openai_realtime_voice_agent/",
                        "/media/local/openai_realtime_voice_agent/"]
        if self.audio_server:
            own_prefixes.append(f"{self.audio_server.public_base_url}/audio/")
        return self.announce and not any(prefix in previous for prefix in own_prefixes)

    async def _request_play(self, content_id, baseline):
        self._playback_requested = True
        await self._request(
            "POST", "services/media_player/play_media",
            json={"entity_id": self.entity_id, "media_content_id": content_id,
                  "media_content_type": "audio/mpeg", "announce": self._announcement_for(baseline)},
        )

    async def start_stream(self, sample_rate, channels):
        stream = LiveMP3(self.audio_server, sample_rate, channels, self.stream_prebuffer_ms)
        self._streams.add(stream)
        self._resources.add(stream.resource)
        try:
            return await stream.start()
        except BaseException:
            await stream.cancel()
            self._streams.discard(stream)
            self._delete_resource_later(stream.resource)
            raise

    async def play_stream(self, stream):
        resource = stream.resource
        try:
            async with asyncio.timeout(self.stream_start_timeout_s):
                await stream.ready()
            baseline = await self._request("GET", f"states/{self.entity_id}")
            requested_at = time.monotonic()
            await self._request_play(self.audio_server.url(resource), baseline)
            logger.info("Requested live reply playback on %s", self.entity_id)
            async with asyncio.timeout(310):
                await resource.finished.wait()
            if resource.error:
                raise resource.error
            await self._wait_until_finished(resource.name, stream.duration, baseline,
                                            requested_at, resource)
        except BaseException:
            await stream.cancel()
            raise
        finally:
            self._streams.discard(stream)
            self._delete_resource_later(resource)

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
        resource = None
        try:
            baseline = await self._request("GET", f"states/{self.entity_id}")
            requested_at = time.monotonic()
            content_id = f"media-source://media_source/local/openai_realtime_voice_agent/{path.name}"
            if self.audio_server is not None:
                resource = self.audio_server.create()
                self._resources.add(resource)
                await resource.append(await asyncio.to_thread(path.read_bytes))
                await resource.finish()
                content_id = self.audio_server.url(resource)
            await self._request_play(content_id, baseline)
            logger.info("Requested %s playback on %s (%.1fs)", label, self.entity_id, duration)
            await self._wait_until_finished(resource.name if resource else path.name,
                                            duration, baseline, requested_at, resource)
        finally:
            # Keep a brief grace period for players that fetch the file late.
            self._delete_later(path)
            if resource is not None:
                self._delete_resource_later(resource)

    async def _wait_until_finished(self, filename, duration, baseline, requested_at=None, resource=None):
        started_at = requested_at if requested_at is not None else time.monotonic()
        if resource is not None:
            # Observe the speaker fetching THIS URL. HA's cached state may lag
            # several seconds and must not add that lag to every chime/reply.
            try:
                async with asyncio.timeout(self.stream_start_timeout_s):
                    await resource.fetched.wait()
            except TimeoutError:
                raise RuntimeError("Speaker did not fetch audio; check tts_stream_base_url and port") from None
            started_at = resource.first_fetch_at
        if self.completion_mode == "duration" or (resource is not None and self.completion_mode == "auto"):
            await asyncio.sleep(max(0, started_at + duration + self.START_GRACE_SECONDS - time.monotonic()))
            self._playback_requested = False
            return
        saw_playing = False
        matched_reply = False
        while True:
            await asyncio.sleep(self.POLL_SECONDS)
            elapsed = time.monotonic() - started_at
            try:
                state = await self._request("GET", f"states/{self.entity_id}",
                                            timeout=aiohttp.ClientTimeout(total=self.state_timeout_s))
            except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError):
                state = {}
            status = state.get("state")
            attrs = state.get("attributes", {})
            content_id = str(attrs.get("media_content_id") or attrs.get("media_title") or "")
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

    async def cancel_streams(self):
        for stream in list(self._streams):
            await stream.cancel()
            self._delete_resource_later(stream.resource)
        self._streams.clear()

    async def close(self):
        await self.stop()
        for stream in list(self._streams):
            await stream.cancel()
        self._streams.clear()
        tasks = list(self._deletions)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for path in list(self._files):
            path.unlink(missing_ok=True)
        self._files.clear()
        if self.audio_server is not None:
            await self.audio_server.close()
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
        self._live_stream = None

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
        self._live_stream = None
        self._pending.clear()
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
        cancel_streams = getattr(self.playback, "cancel_streams", None)
        if cancel_streams is not None:
            await cancel_streams()
        await self.playback.stop()
        await self._speaking_finished()

    async def _play_pending(self):
        try:
            while self._pending:
                item = self._pending.popleft()
                try:
                    if isinstance(item, LiveMP3):
                        await self.playback.play_stream(item)
                    else:
                        audio, rate, channels = item
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
                    if getattr(self.playback, "delivery", "buffered") == "stream":
                        try:
                            if self._live_stream is None:
                                self._live_stream = await self.playback.start_stream(*audio_format)
                                self._pending.append(self._live_stream)
                                if self._worker is None or self._worker.done():
                                    self._worker = asyncio.create_task(self._play_pending())
                            await self._live_stream.write(frame.audio)
                        except Exception as exc:
                            logger.error("Live speech encoding failed: %s", exc)
                            await self.interrupt()
                            return
                        if self._live_stream.duration > self.MAX_AUDIO_SECONDS:
                            await self.interrupt()
                            logger.error("TTS reply exceeded the five-minute audio limit; discarded")
                        return
                    self._audio.extend(frame.audio)
                    if len(self._audio) > frame.sample_rate * frame.num_channels * 2 * self.MAX_AUDIO_SECONDS:
                        await self.interrupt()
                        logger.error("TTS reply exceeded the five-minute audio limit; discarded")
                return  # never send this audio to the Voice PE as well
            if isinstance(frame, TTSStoppedFrame):
                self._capturing = False
                if self._live_stream is not None:
                    stream, self._live_stream = self._live_stream, None
                    self._format = None
                    try:
                        await stream.finish()
                    except Exception as exc:
                        logger.error("Live speech encoding failed: %s", exc)
                        await self.interrupt()
                    return
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
