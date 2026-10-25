"""One paced audio stream for an entire Live conversation, including quiet gaps."""

import asyncio
import logging
import time
from collections import deque
from contextlib import suppress

import aiohttp

from app.live_audio import LiveMP3
from app.airplay import AirPlayOutput
from app.pcm import FRAME_BYTES, RATE, SILENCE, dbfs

log = logging.getLogger(__name__)


class SpeechOutput:
    def __init__(
        self,
        settings,
        device,
        http,
        audio_server,
        on_start,
        on_end,
        on_error,
        token,
        *,
        wake_started_at=None,
        wake_cue=None,
        on_cue_handoff=None,
    ):
        self.s, self.device, self.http, self.server = (
            settings,
            device,
            http,
            audio_server,
        )
        self.on_start, self.on_end, self.on_error = on_start, on_end, on_error
        cue_audio = wake_cue or b""
        self.cue_frames = deque(
            cue_audio[i : i + FRAME_BYTES].ljust(FRAME_BYTES, b"\0")
            for i in range(0, len(cue_audio), FRAME_BYTES)
        )
        self.cue_done = asyncio.Event()
        self.cue_started = False
        self.cue_sending = False
        self.on_cue_handoff = on_cue_handoff
        if not self.cue_frames:
            self.cue_done.set()
        self.token = token
        self.frames = deque()
        self.buffer = bytearray()
        self.pending_speech = 0
        self.speaking = False
        self.quiet_ms = 0
        self.last_speech_at = 0
        self.closed = False
        self.requested = False
        self.encoder = None
        self.airplay = None
        self.ready = asyncio.Event()
        self.playback = None
        self.command = None
        self.tail = None
        self.started = time.monotonic()
        self.wake_started_at = (
            self.started if wake_started_at is None else wake_started_at
        )
        self.wake_id = device.wake_id
        self.first_speech_queued_at = None
        self.first_speech_sent = False
        self.on_reference = None
        self.timing = None
        self.mic_queue_ms = lambda: 0
        self.worker = asyncio.create_task(self._run())

    @property
    def active(self):
        # Keepalive silence must not keep the paid conversation alive forever.
        return (
            self.speaking
            or self.pending_speech > 0
            or bool(self.cue_frames)
            or self.cue_sending
        )

    @property
    def queued_audio_ms(self):
        return (len(self.cue_frames) + len(self.frames)) * 20

    async def write(self, audio):
        if self.closed:
            return
        self.buffer.extend(audio)
        while len(self.buffer) >= FRAME_BYTES:
            frame = bytes(self.buffer[:FRAME_BYTES])
            del self.buffer[:FRAME_BYTES]
            loud = dbfs(frame) > self.s.output_silence_threshold_db
            if loud and self.first_speech_queued_at is None:
                self.first_speech_queued_at = time.monotonic()
            marker = (
                self.timing.output_received(
                    loud,
                    time.monotonic(),
                    self.queued_audio_ms,
                    self.mic_queue_ms(),
                    self.pending_speech * 20,
                )
                if self.timing
                else None
            )
            self.frames.append((frame, loud, marker))
            self.pending_speech += int(loud)
        limit = max(1, self.s.output_buffer_ms // 20)
        # A network burst of silence can be shortened. Never silently discard
        # spoken words or build an unbounded, increasingly delayed speech queue.
        while len(self.frames) > limit and not self.frames[0][1]:
            self.frames.popleft()
        if len(self.frames) > limit:
            raise RuntimeError("Live speaker audio exceeded output_buffer_ms")

    async def _ha(self, method, path, **kwargs):
        async with self.http.request(
            method,
            f"http://supervisor/core/api/{path}",
            headers={"Authorization": f"Bearer {self.token}"},
            **kwargs,
        ) as response:
            if response.status >= 400:
                raise RuntimeError(
                    f"HA playback request returned HTTP {response.status}"
                )
            return await response.json()

    def _announce(self, state):
        attrs = state.get("attributes", {})
        previous = str(attrs.get("media_content_id") or attrs.get("media_title") or "")
        own = (
            self.server and f"{self.server.public_base_url}/audio/" in previous
        ) or "/openai_realtime_voice_agent/" in previous
        return self.s.tts_announce and not own

    async def _external(self):
        resource = self.encoder.resource
        async with asyncio.timeout(self.s.tts_stream_start_timeout_s):
            await self.encoder.ready()
            encoder_ready_at = time.monotonic()
            baseline = await self._ha("GET", f"states/{self.s.tts_media_player}")
            play_requested_at = time.monotonic()
            self.requested = True
            # Some announcement services wait for playback to end. Do not wait
            # for that service response before allowing this live stream to run.
            self.command = asyncio.create_task(
                self._ha(
                    "POST",
                    "services/media_player/play_media",
                    json={
                        "entity_id": self.s.tts_media_player,
                        "media_content_id": self.server.url(resource),
                        "media_content_type": "audio/mpeg",
                        "announce": self._announce(baseline),
                    },
                    timeout=aiohttp.ClientTimeout(
                        total=self.s.max_session_seconds
                        + self.s.session_start_timeout_s
                        + 5,
                        connect=5,
                    ),
                )
            )
            fetched = asyncio.create_task(resource.fetched.wait())
            try:
                done, _ = await asyncio.wait(
                    (self.command, fetched), return_when=asyncio.FIRST_COMPLETED
                )
                if self.command in done:
                    self.command.result()
                await fetched
            finally:
                fetched.cancel()
                await asyncio.gather(fetched, return_exceptions=True)
        fetched_at = resource.first_fetch_at
        if fetched_at is None:
            fetched_at = time.monotonic()
        log.info(
            "Conversation speaker stream fetched in %.0f ms (wake=%d, encoder_ready=%.0f ms, player_state=%.0f ms, play_to_fetch=%.0f ms, since_wake=%.0f ms; HTTP fetch, not audible playback)",
            (fetched_at - self.started) * 1000,
            self.wake_id,
            (encoder_ready_at - self.started) * 1000,
            (play_requested_at - encoder_ready_at) * 1000,
            (fetched_at - play_requested_at) * 1000,
            (fetched_at - self.wake_started_at) * 1000,
        )
        self.ready.set()
        await self.command
        await resource.finished.wait()
        if resource.error:
            raise resource.error

    async def _activity(self, loud):
        now = time.monotonic()
        if loud:
            self.last_speech_at = now
            self.quiet_ms = 0
            if not self.speaking:
                self.speaking = True
                if self.tail:
                    self.tail.cancel()
                await self.on_start()
        elif self.speaking:
            self.quiet_ms += 20
            margin = self.s.tts_start_allowance_ms
            if self.airplay:
                margin += self.s.airplay_latency_ms
            else:
                margin += (
                    self.s.tts_stream_prebuffer_ms
                    if self.server
                    else self.s.playback_prebuffer_ms
                )
            if (
                not self.pending_speech
                and self.quiet_ms >= self.s.output_silence_ms
                and (now - self.last_speech_at) * 1000 >= margin
            ):
                self.speaking = False
                # Echo guards must never block the audio clock or keepalive.
                self.tail = asyncio.create_task(self.on_end())

    async def _run(self):
        try:
            if self.device.wake_id != self.wake_id:
                return
            if self.s.tts_output == "airplay":
                self.airplay = AirPlayOutput(self.s)
                await self.airplay.start()
                if self.closed:
                    return
                self.ready.set()
                log.info(
                    "Conversation AirPlay sender ready in %.0f ms (wake=%d; sender acknowledgement, not audible playback)",
                    (time.monotonic() - self.wake_started_at) * 1000,
                    self.wake_id,
                )
            elif self.server:
                self.encoder = LiveMP3(
                    self.server, RATE, 1, self.s.tts_stream_prebuffer_ms
                )
                await self.encoder.start()
                if self.closed:
                    return
                self.playback = asyncio.create_task(self._external())
            else:
                self.ready.set()
            deadline = time.monotonic()
            while not self.closed:
                if self.device.wake_id != self.wake_id:
                    return
                if self.airplay:
                    self.airplay.check()
                if self.playback and self.playback.done():
                    self.playback.result()
                    raise RuntimeError("Conversation speaker stream ended early")
                if self.encoder and self.encoder.resource.error:
                    raise self.encoder.resource.error
                if self.encoder:
                    resource = self.encoder.resource
                    if (
                        resource.fetched.is_set()
                        and not resource.readers
                        and resource.last_reader_left_at is not None
                        and time.monotonic() - resource.last_reader_left_at
                        > self.s.tts_stream_start_timeout_s
                    ):
                        raise RuntimeError("Conversation speaker disconnected")
                # Warm the external player with silence immediately at wake.
                # Retain the greeting until the player has actually fetched.
                available = not self.encoder or self.encoder.resource.fetched.is_set()
                is_cue = available and bool(self.cue_frames)
                if is_cue:
                    frame, loud, marker = self.cue_frames.popleft(), False, None
                    self.cue_sending = True
                elif available and self.frames:
                    frame, loud, marker = self.frames.popleft()
                    self.pending_speech -= int(loud)
                else:
                    frame, loud, marker = SILENCE, False, None
                await self._activity(loud)
                if self.on_reference:
                    self.on_reference(frame, time.monotonic())
                if self.airplay:
                    await self.airplay.write(frame)
                elif self.encoder:
                    await self.encoder.write(frame)
                else:
                    await self.device.audio(frame)
                if is_cue:
                    first, last = not self.cue_started, not self.cue_frames
                    self.cue_started = True
                    if self.on_cue_handoff:
                        self.on_cue_handoff(first, last)
                    if last:
                        self.cue_sending = False
                        self.cue_done.set()
                if marker is not None:
                    self.timing.output_handed(marker, time.monotonic())
                if loud and not self.first_speech_sent:
                    self.first_speech_sent = True
                    sent_at = time.monotonic()
                    log.info(
                        "Conversation first speech handed to %s in %.0f ms (wake=%d, output_queue=%.0f ms; handoff, not audible playback)",
                        "AirPlay sender"
                        if self.airplay
                        else "MP3 encoder"
                        if self.encoder
                        else "Voice PE",
                        (sent_at - self.wake_started_at) * 1000,
                        self.wake_id,
                        (sent_at - self.first_speech_queued_at) * 1000,
                    )
                deadline += 0.02
                if time.monotonic() - deadline > 0.1:
                    deadline = time.monotonic()
                await asyncio.sleep(max(0, deadline - time.monotonic()))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self.s.tts_output == "airplay":
                log.error(
                    "Direct AirPlay failed (%s): %s",
                    type(exc).__name__,
                    self.airplay.error
                    if self.airplay and self.airplay.error
                    else "check amp IP, AirPlay port/protocol, access restrictions and LAN connectivity",
                )
            else:
                log.error("Conversation audio failed (%s)", type(exc).__name__)
            await self.on_error(
                "Direct AirPlay failed; see add-on log"
                if self.s.tts_output == "airplay"
                else "Speaker stream failed; check URL, buffering and access"
            )

    async def close(self):
        if self.closed:
            return
        self.closed = True
        for task in (self.worker, self.playback, self.command, self.tail):
            if task:
                task.cancel()
        await asyncio.gather(
            *(t for t in (self.worker, self.playback, self.command, self.tail) if t),
            return_exceptions=True,
        )
        if self.airplay:
            await self.airplay.close()
        # Stop before closing the URL, never after an announcement could have
        # completed and restored another source.
        if self.requested:
            with suppress(Exception):
                async with asyncio.timeout(2):
                    await self._ha(
                        "POST",
                        "services/media_player/media_stop",
                        json={"entity_id": self.s.tts_media_player},
                    )
            self.requested = False
        if self.encoder:
            await self.encoder.cancel()
            await self.server.remove(self.encoder.resource)
        self.frames.clear()
        self.cue_frames.clear()
        self.cue_sending = False
        self.cue_done.set()
        self.buffer.clear()
        self.pending_speech = 0
        self.speaking = False
