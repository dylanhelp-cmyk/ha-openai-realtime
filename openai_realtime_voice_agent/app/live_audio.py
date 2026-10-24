"""Bounded, session-long HTTP audio streams and one progressive MP3 encoder."""

import asyncio
import secrets
import time
from contextlib import suppress
from urllib.parse import urlsplit

from aiohttp import web


class AudioResource:
    def __init__(self, name, max_bytes=80000):
        self.name = name
        self.max_bytes = max_bytes
        self.data = bytearray()
        self.start_offset = 0
        self.total_bytes = 0
        self.changed = asyncio.Condition()
        self.finished = asyncio.Event()
        self.fetched = asyncio.Event()
        self.first_fetch_at = None
        self.readers = 0
        self.last_reader_left_at = None
        self.aborted = False
        self.error = None

    async def append(self, data):
        async with self.changed:
            self.data.extend(data)
            self.total_bytes += len(data)
            excess = max(0, len(self.data) - self.max_bytes)
            if excess:
                del self.data[:excess]
                self.start_offset += excess
            self.changed.notify_all()

    async def finish(self, error=None):
        async with self.changed:
            self.error = error
            self.finished.set()
            self.changed.notify_all()

    async def abort(self):
        self.aborted = True
        await self.finish()


class AudioHTTPServer:
    """Serve live audio under unpredictable URLs that expire with the session.

    No HA tokens or signed query strings reach the player. WiiM Audio can add
    its announcement cache parameter without invalidating authorization. Every
    conversation gets one path; speech and silence share that same stream.
    """

    def __init__(self, public_base_url, port=18082, host="0.0.0.0", buffer_seconds=5):
        parsed = urlsplit(public_base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError(
                "tts_stream_base_url must be a speaker-reachable HTTP base URL"
            )
        self.public_base_url = public_base_url.rstrip("/")
        self.port, self.host = port, host
        self.buffer_bytes = int(buffer_seconds * 16000)
        self.resources = {}
        self.runner = None

    async def start(self):
        app = web.Application()
        app.router.add_get("/audio/{name}", self._serve)
        self.runner = web.AppRunner(app, access_log=None, shutdown_timeout=2)
        await self.runner.setup()
        try:
            site = web.TCPSite(self.runner, self.host, self.port)
            await site.start()
        except BaseException:
            await self.runner.cleanup()
            self.runner = None
            raise
        if self.port == 0:  # ephemeral loopback port for contract tests
            self.port = site._server.sockets[0].getsockname()[1]
            self.public_base_url = self.public_base_url.replace(":0", f":{self.port}")

    def create(self):
        resource = AudioResource(f"{secrets.token_urlsafe(32)}.mp3", self.buffer_bytes)
        self.resources[resource.name] = resource
        return resource

    def url(self, resource):
        return f"{self.public_base_url}/audio/{resource.name}"

    async def remove(self, resource):
        self.resources.pop(resource.name, None)
        await resource.abort()

    async def _serve(self, request):
        resource = self.resources.get(request.match_info["name"])
        if resource is None or resource.aborted:
            raise web.HTTPNotFound()
        headers = {
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Content-Type": "audio/mpeg",
            "Accept-Ranges": "none",
        }
        if request.method == "HEAD":
            return web.Response(headers=headers)
        # Some players make a one-byte probe before their real streaming GET.
        # Do not mistake that probe for the start of audible playback.
        if request.headers.get("Range") == "bytes=0-0":
            async with asyncio.timeout(10):
                async with resource.changed:
                    await resource.changed.wait_for(
                        lambda: bool(resource.data) or resource.finished.is_set()
                    )
            if resource.aborted or not resource.data:
                raise web.HTTPNotFound()
            headers["Content-Range"] = "bytes 0-0/*"
            return web.Response(
                status=206, body=bytes(resource.data[:1]), headers=headers
            )
        # Live MP3 is not seekable. A reconnect joins the live edge with a small
        # decode cushion, rather than replaying earlier replies. MP3 decoders
        # resynchronise on the next frame header; there is no repeated ID3 tag.
        offset = resource.start_offset
        if resource.fetched.is_set():
            offset = max(offset, resource.total_bytes - 4096)
        response = web.StreamResponse(headers=headers)
        await response.prepare(request)
        resource.readers += 1
        try:
            while True:
                async with resource.changed:
                    await resource.changed.wait_for(
                        lambda offset=offset: (
                            resource.total_bytes > offset or resource.finished.is_set()
                        )
                    )
                    if offset < resource.start_offset:
                        # A stalled consumer fell outside the bounded window.
                        # Close it so the player can reconnect at the live edge.
                        break
                    chunk = bytes(resource.data[offset - resource.start_offset :])
                    done = resource.finished.is_set()
                if resource.aborted:
                    break
                if chunk:
                    if resource.first_fetch_at is None:
                        resource.first_fetch_at = time.monotonic()
                        resource.fetched.set()
                    async with asyncio.timeout(5):
                        await response.write(chunk)
                    offset += len(chunk)
                if done:
                    break
            await response.write_eof()
        except (ConnectionError, asyncio.TimeoutError):
            pass
        finally:
            resource.readers -= 1
            if not resource.readers:
                resource.last_reader_left_at = time.monotonic()
        return response

    async def close(self):
        for resource in list(self.resources.values()):
            await self.remove(resource)
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None


class LiveMP3:
    """One encoder survives speech, silence and subsequent replies until Stop."""

    def __init__(self, server, sample_rate, channels, prebuffer_ms=120):
        self.server, self.sample_rate, self.channels = server, sample_rate, channels
        self.resource = server.create()
        self.prebuffer_ms = prebuffer_ms
        self.pcm_bytes = 0
        self.process = None
        self.reader = None
        self.input_finished = False

    @property
    def duration(self):
        return self.pcm_bytes / (self.sample_rate * self.channels * 2)

    async def start(self):
        spawning = asyncio.create_task(
            asyncio.create_subprocess_exec(
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "s16le",
                "-ar",
                str(self.sample_rate),
                "-ac",
                str(self.channels),
                "-probesize",
                "32",
                "-analyzeduration",
                "0",
                "-i",
                "pipe:0",
                "-codec:a",
                "libmp3lame",
                "-b:a",
                "128k",
                "-write_xing",
                "0",
                "-id3v2_version",
                "0",
                "-flush_packets",
                "1",
                "-f",
                "mp3",
                "pipe:1",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        )
        try:
            # Retain the process handle even if Stop arrives during creation.
            self.process = await asyncio.shield(spawning)
        except asyncio.CancelledError:
            self.process = await spawning
            await self.cancel()
            raise
        self.reader = asyncio.create_task(self._read())
        return self

    async def _read(self):
        try:
            while chunk := await self.process.stdout.read(4096):
                await self.resource.append(chunk)
            await self.process.wait()
            error = (
                RuntimeError("Live MP3 encoder failed")
                if self.process.returncode
                else None
            )
            await self.resource.finish(error)
        except Exception as exc:
            await self.resource.finish(exc)

    async def write(self, audio):
        if self.input_finished:
            raise RuntimeError("Live audio input is already closed")
        self.pcm_bytes += len(audio)
        self.process.stdin.write(audio)
        async with asyncio.timeout(5):
            await self.process.stdin.drain()

    async def ready(self):
        # Encoded bytes provide a real ready signal; raw PCM duration alone
        # does not prove that ffmpeg has flushed its first playable packet.
        threshold = max(1, int(16000 * self.prebuffer_ms / 1000))
        async with self.resource.changed:
            await self.resource.changed.wait_for(
                lambda: (
                    self.resource.total_bytes >= threshold
                    or self.resource.finished.is_set()
                )
            )
        if self.resource.error:
            raise self.resource.error
        if not self.resource.data:
            raise RuntimeError("Live MP3 encoder produced no audio")

    async def finish(self):
        if not self.input_finished:
            self.input_finished = True
            self.process.stdin.close()
        async with asyncio.timeout(10):
            await self.reader
        if self.resource.error:
            raise self.resource.error

    async def cancel(self):
        self.input_finished = True
        await self.resource.abort()
        if self.process is not None and self.process.returncode is None:
            with suppress(ProcessLookupError):
                self.process.kill()
            await self.process.wait()
        if self.reader is not None:
            self.reader.cancel()
            with suppress(asyncio.CancelledError):
                await self.reader
