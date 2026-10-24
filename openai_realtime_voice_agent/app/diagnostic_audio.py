"""Bounded, local HTTP/MP3 reference decoder used only by playback diagnostics."""

import asyncio
import audioop
import time
from contextlib import suppress

import aiohttp
import numpy as np

from app.echo import RATE, SAMPLES, measure_delay


def _records(samples):
    return [
        (offset / RATE, samples[offset : offset + SAMPLES])
        for offset in range(0, len(samples) - SAMPLES + 1, SAMPLES)
    ]


def _position(probe, samples, confidence):
    # Match in media-sample coordinates, independently of decoder read bursts.
    return round(
        measure_delay(_records(probe), _records(samples), 40000, confidence)["delay_ms"]
        * RATE
        / 1000
    )


def measure_loopback(probe24, reference_frames, decoded_chunks, confidence):
    """Locate the same probe in both streams, then map anchors to wall clocks.

    A decoded chunk's timestamp means all of its samples were available by
    that instant. We deliberately do not invent a real-time clock for buffered
    decoder reads. Chunk completion adds at most one 20 ms read block under
    steady delivery; excessive variation is reported as an unreliable result.
    """
    if not reference_frames or not decoded_chunks:
        raise ValueError("No reference decoder audio")
    raw, _ = audioop.ratecv(probe24, 2, 1, 24000, RATE, None)
    probe = np.frombuffer(raw, dtype="<i2").astype(float)
    reference = np.concatenate([samples for _, samples in reference_frames])
    decoded = np.concatenate([samples for _, samples in decoded_chunks])
    ref_offset = _position(probe, reference, confidence)
    out_offset = _position(probe, decoded, confidence)

    def stamp(frames, position, completion=False):
        for when, samples in frames:
            if position < len(samples):
                return when if completion else when + position / RATE
            position -= len(samples)
        raise ValueError("Reference decoder did not capture the complete probe")

    delays = []
    for seconds in (0.25, 0.95, 1.65):
        anchor = round(seconds * RATE)
        emitted = stamp(reference_frames, ref_offset + anchor)
        available = stamp(decoded_chunks, out_offset + anchor, completion=True)
        delays.append((available - emitted) * 1000)
    spread = max(delays) - min(delays)
    if min(delays) < -20 or max(delays) > 10000 or spread > 100:
        raise ValueError(f"Unstable reference decoder timing: {delays!r}")
    return {"delay_ms": round(float(np.median(delays))), "spread_ms": round(spread)}


class MP3Loopback:
    """Decode the real HTTP stream without issuing another speaker command."""

    def __init__(self, http, server, resource):
        self.http, self.server, self.resource = http, server, resource
        self.process = None
        self.tasks = []
        self.chunks = []
        self.ready = asyncio.Event()
        self.failed = asyncio.Event()
        self.error = None
        self.stopped = False

    async def start(self):
        spawning = asyncio.create_task(
            asyncio.create_subprocess_exec(
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "mp3",
                "-probesize",
                "32",
                "-analyzeduration",
                "0",
                "-i",
                "pipe:0",
                "-f",
                "s16le",
                "-ac",
                "1",
                "-ar",
                str(RATE),
                "-flush_packets",
                "1",
                "pipe:1",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        )
        try:
            self.process = await asyncio.shield(spawning)
        except asyncio.CancelledError:
            self.process = await spawning
            await self.stop()
            raise
        if self.stopped:
            await self.stop()
            raise asyncio.CancelledError
        self.tasks = [
            asyncio.create_task(self._feed()),
            asyncio.create_task(self._read()),
        ]
        async with asyncio.timeout(5):
            while not self.ready.is_set():
                if self.failed.is_set():
                    raise RuntimeError(self.error)
                await asyncio.sleep(0.02)

    def fail(self, exc):
        self.error = type(exc).__name__
        self.failed.set()

    async def _feed(self):
        try:
            url = f"http://127.0.0.1:{self.server.port}/audio/{self.resource.name}"
            async with self.http.get(
                url,
                headers={"X-Playback-Diagnostic": "loopback"},
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=3, sock_read=3),
            ) as response:
                response.raise_for_status()
                async for chunk in response.content.iter_any():
                    self.process.stdin.write(chunk)
                    await self.process.stdin.drain()
        except Exception as exc:
            self.fail(exc)

    async def _read(self):
        try:
            while True:
                raw = await self.process.stdout.readexactly(SAMPLES * 2)
                self.chunks.append(
                    (time.monotonic(), np.frombuffer(raw, dtype="<i2").astype(float))
                )
                if len(self.chunks) > 2250:
                    raise RuntimeError("Reference decoder exceeded 45 seconds")
                self.ready.set()
        except Exception as exc:
            self.fail(exc)

    async def stop(self):
        if not self.stopped:
            self.stopped = True
            for task in self.tasks:
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.process and self.process.returncode is None:
            with suppress(ProcessLookupError):
                self.process.kill()
            await self.process.wait()
