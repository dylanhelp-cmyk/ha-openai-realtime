"""Segment continuous Live audio by measured silence and stream speech immediately."""

import asyncio
from collections import deque
from contextlib import suppress
from dataclasses import dataclass
import logging
import time

from app.live_audio import AudioResource, LiveMP3
from app.pcm import FRAME_BYTES, RATE, dbfs

log = logging.getLogger(__name__)


@dataclass
class Segment:
    resource: AudioResource
    encoder: object = None
    duration: float = 0


class SpeechOutput:
    def __init__(
        self, settings, device, http, audio_server, on_start, on_end, on_error, token
    ):
        self.s, self.device, self.http, self.server = (
            settings,
            device,
            http,
            audio_server,
        )
        self.on_start, self.on_end, self.on_error = on_start, on_end, on_error
        self.token = token
        self.lock = asyncio.Lock()
        self.current = None
        self.pending = asyncio.Queue()
        self.buffer = bytearray()
        self.preroll = deque(maxlen=4)
        self.quiet_ms = 0
        self.last_packet = 0
        self.playing = False
        self.requested = False
        self.closed = False
        self.worker = asyncio.create_task(self._worker())
        self.reaper = set()

    @property
    def active(self):
        return self.current is not None or self.playing or not self.pending.empty()

    async def write(self, audio):
        async with self.lock:
            await self._write(audio)

    async def _write(self, audio):
        if self.closed:
            return
        self.last_packet = time.monotonic()
        self.buffer.extend(audio)
        while len(self.buffer) >= FRAME_BYTES:
            if self.closed:
                return
            frame = bytes(self.buffer[:FRAME_BYTES])
            del self.buffer[:FRAME_BYTES]
            loud = dbfs(frame) > self.s.output_silence_threshold_db
            if self.current is None:
                if not loud:
                    self.preroll.append(frame)
                    continue
                if self.server:
                    encoder = await LiveMP3(
                        self.server, RATE, 1, self.s.tts_stream_prebuffer_ms
                    ).start()
                    if self.closed:
                        await encoder.cancel()
                        await self.server.remove(encoder.resource)
                        return
                    segment = Segment(encoder.resource, encoder)
                else:
                    segment = Segment(AudioResource("pcm"))
                self.current = segment
                self.quiet_ms = 0
                await self.on_start()
                await self.pending.put(segment)
                for previous in self.preroll:
                    await self._append(previous)
                self.preroll.clear()
            await self._append(frame)
            self.quiet_ms = 0 if loud else self.quiet_ms + 20
            if self.quiet_ms >= self.s.output_silence_ms:
                await self.finish_segment()

    async def _append(self, frame):
        segment = self.current
        segment.duration += len(frame) / (RATE * 2)
        if segment.duration > 300:
            raise RuntimeError("Speech exceeded the five-minute segment limit")
        if segment.encoder:
            await segment.encoder.write(frame)
        else:
            await segment.resource.append(frame)

    async def finish_segment(self):
        if not self.current:
            return
        segment, self.current = self.current, None
        if segment.encoder:
            await segment.encoder.finish()
        else:
            await segment.resource.finish()

    async def tick(self):
        async with self.lock:
            if (
                self.current
                and not self.closed
                and (time.monotonic() - self.last_packet) * 1000
                >= self.s.output_gap_timeout_ms
            ):
                # No response-audio-done event exists in Live. Quiet PCM is the
                # primary boundary; an inter-packet gap is the fallback.
                await self.finish_segment()

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

    async def _external(self, segment):
        async with asyncio.timeout(self.s.tts_stream_start_timeout_s):
            await segment.encoder.ready()
        baseline = await self._ha("GET", f"states/{self.s.tts_media_player}")
        self.requested = True
        await self._ha(
            "POST",
            "services/media_player/play_media",
            json={
                "entity_id": self.s.tts_media_player,
                "media_content_id": self.server.url(segment.resource),
                "media_content_type": "audio/mpeg",
                "announce": self._announce(baseline),
            },
        )
        async with asyncio.timeout(self.s.tts_stream_start_timeout_s):
            await segment.resource.fetched.wait()
        async with asyncio.timeout(310):
            await segment.resource.finished.wait()
        if segment.resource.error:
            raise segment.resource.error
        deadline = (
            segment.resource.first_fetch_at
            + segment.duration
            + self.s.tts_start_allowance_ms / 1000
        )
        await asyncio.sleep(max(0, deadline - time.monotonic()))
        self.requested = False

    async def _local(self, segment):
        offset = 0
        deadline = time.monotonic()
        while True:
            async with segment.resource.changed:
                await segment.resource.changed.wait_for(
                    lambda: (
                        len(segment.resource.data) > offset
                        or segment.resource.finished.is_set()
                    )
                )
                frame = bytes(segment.resource.data[offset : offset + FRAME_BYTES])
                done = segment.resource.finished.is_set() and offset + len(
                    frame
                ) >= len(segment.resource.data)
            if frame:
                if self.device.ws.closed:
                    return
                await self.device.audio(frame)
                offset += len(frame)
                deadline += len(frame) / (RATE * 2)
                await asyncio.sleep(max(0, deadline - time.monotonic()))
            if done:
                break
        await asyncio.sleep(self.s.tts_start_allowance_ms / 1000)

    def _retire(self, segment):
        async def retire():
            try:
                await asyncio.sleep(self.s.audio_retention_seconds)
            finally:
                if self.server:
                    await self.server.remove(segment.resource)

        task = asyncio.create_task(retire())
        self.reaper.add(task)
        task.add_done_callback(self.reaper.discard)

    async def _worker(self):
        while True:
            segment = await self.pending.get()
            self.playing = True
            completed = False
            try:
                if segment.encoder:
                    await self._external(segment)
                else:
                    await self._local(segment)
                completed = True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("Audio playback failed (%s)", type(exc).__name__)
                await self.on_error(
                    "Speaker playback failed; check audio URL and speaker access"
                )
            finally:
                if segment.encoder and not completed:
                    await segment.encoder.cancel()
                self.playing = False
                self.pending.task_done()
                if self.server:
                    self._retire(segment)
            if not self.active and not self.closed:
                await self.on_end()

    async def close(self):
        if self.closed:
            return
        self.closed = True
        self.worker.cancel()
        await asyncio.gather(self.worker, return_exceptions=True)
        if self.current:
            if self.current.encoder:
                await self.current.encoder.cancel()
            else:
                await self.current.resource.abort()
            if self.server:
                await self.server.remove(self.current.resource)
            self.current = None
        while not self.pending.empty():
            segment = self.pending.get_nowait()
            if segment.encoder:
                await segment.encoder.cancel()
            if self.server:
                await self.server.remove(segment.resource)
            self.pending.task_done()
        if self.requested:
            with suppress(Exception):
                async with asyncio.timeout(2):
                    await self._ha(
                        "POST",
                        "services/media_player/media_stop",
                        json={"entity_id": self.s.tts_media_player},
                    )
            self.requested = False
        for task in self.reaper:
            task.cancel()
        await asyncio.gather(*list(self.reaper), return_exceptions=True)
