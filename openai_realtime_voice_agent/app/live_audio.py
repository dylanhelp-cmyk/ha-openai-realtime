"""Short-lived HTTP audio URLs and progressive MP3 encoding for network speakers."""

import asyncio
from contextlib import suppress
import secrets
import time
from urllib.parse import urlsplit

from aiohttp import web


class AudioResource:
    MAX_BYTES = 16 * 1024 * 1024

    def __init__(self, name):
        self.name = name
        self.data = bytearray()
        self.changed = asyncio.Condition()
        self.finished = asyncio.Event()
        self.fetched = asyncio.Event()
        self.first_fetch_at = None
        self.aborted = False
        self.error = None

    async def append(self, data):
        async with self.changed:
            if len(self.data) + len(data) > self.MAX_BYTES:
                raise RuntimeError("Network audio exceeded the size limit")
            self.data.extend(data)
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
    """Serve only generated audio under unpredictable, expiring per-turn URLs.

    No HA tokens or signed query strings reach the player. WiiM Audio can add
    its announcement cache parameter without invalidating authorization. Every
    response gets a different path, including repeated greetings.
    """

    def __init__(self, public_base_url, port=18082, host="0.0.0.0"):
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
        resource = AudioResource(f"{secrets.token_urlsafe(32)}.mp3")
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
            if resource.finished.is_set():
                headers["Content-Length"] = str(len(resource.data))
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
            total = str(len(resource.data)) if resource.finished.is_set() else "*"
            headers["Content-Range"] = f"bytes 0-0/{total}"
            return web.Response(
                status=206, body=bytes(resource.data[:1]), headers=headers
            )
        # Live MP3 has no seekable length. Ignore other Range headers and send
        # the complete stream with 200, as permitted for non-range resources.
        if resource.finished.is_set():
            if resource.first_fetch_at is None:
                resource.first_fetch_at = time.monotonic()
                resource.fetched.set()
            return web.Response(body=bytes(resource.data), headers=headers)
        response = web.StreamResponse(headers=headers)
        await response.prepare(request)
        offset = 0
        try:
            async with asyncio.timeout(330):
                while True:
                    async with resource.changed:
                        await resource.changed.wait_for(
                            lambda: (
                                len(resource.data) > offset
                                or resource.finished.is_set()
                            )
                        )
                        chunk = bytes(resource.data[offset:])
                        done = resource.finished.is_set()
                    if resource.aborted:
                        break
                    if chunk:
                        if resource.first_fetch_at is None:
                            resource.first_fetch_at = time.monotonic()
                            resource.fetched.set()
                        await response.write(chunk)
                        offset += len(chunk)
                    if done:
                        break
            await response.write_eof()
        except (ConnectionError, asyncio.TimeoutError):
            pass
        return response

    async def close(self):
        for resource in list(self.resources.values()):
            await self.remove(resource)
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None


class LiveMP3:
    """Encode and expose PCM as it arrives, without waiting for TTSStoppedFrame."""

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
        self.process = await asyncio.create_subprocess_exec(
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
            "-flush_packets",
            "1",
            "-f",
            "mp3",
            "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
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
                    len(self.resource.data) >= threshold
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
