"""One direct AirPlay sender and persistent PCM input per conversation.

Uses the pinned, standalone cliairplay executable; no Music Assistant server,
HA media_player service, HTTP audio URL or per-phrase reconnect is involved.
"""

import asyncio
import audioop
import logging
import os
import re
import tempfile
import time
from contextlib import suppress
from pathlib import Path

from app.pcm import RATE, SILENCE

log = logging.getLogger(__name__)
AIRPLAY_BINARY = "/usr/local/bin/cliairplay"
AIRPLAY_RATE = 44100


class AirPlayOutput:
    def __init__(self, settings):
        self.s = settings
        self.process = None
        self.spawning = None
        self.tasks = []
        self.folder = None
        self.command_fd = None
        self.closed = False
        self.error = None
        self.failed = asyncio.Event()
        self.connected = asyncio.Event()
        self.audio = asyncio.Event()
        self.started = asyncio.Event()
        self.ready = asyncio.Event()
        self.resampler = None
        self.connected_at = None
        self.scheduled_at_ms = None
        self.cleanup_lock = asyncio.Lock()
        self.stage = "spawn"

    def _fail(self, message):
        if not self.closed and self.error is None:
            self.error = RuntimeError(message)
            self.failed.set()

    def check(self):
        if self.error:
            raise self.error
        if self.closed:
            raise RuntimeError("AirPlay output closed")
        if self.process and self.process.returncode is not None:
            self._fail(
                f"AirPlay sender exited (code={self.process.returncode}, stage={self.stage})"
            )
            raise self.error

    async def _wait(self, event):
        self.check()
        waits = [asyncio.create_task(e.wait()) for e in (event, self.failed)]
        try:
            await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
            self.check()
        finally:
            for task in waits:
                task.cancel()
            await asyncio.gather(*waits, return_exceptions=True)

    def _command(self, value):
        self.check()
        data = value.encode("ascii")
        if os.write(self.command_fd, data) != len(data):
            raise RuntimeError("AirPlay control pipe short write")

    async def _read_status(self, reader):
        try:
            while line := await reader.readline():
                # Never forward arbitrary native output: it can contain device
                # responses or credentials. Log only allowlisted numeric/status data.
                text = line.decode("utf-8", "replace").strip()
                if not text.startswith("[STATUS] "):
                    continue
                status = text[9:]
                kind = status.split(" ", 1)[0]
                if kind == "connected":
                    self.connected_at = time.monotonic()
                    self.connected.set()
                elif kind == "audio":
                    self.audio.set()
                elif kind == "started":
                    match = re.search(r"\bat_unix_ms=(\d+)\b", status)
                    self.scheduled_at_ms = int(match[1]) if match else None
                    self.started.set()
                elif kind == "error":
                    match = re.search(r"\bcode=([a-z0-9_]+)\b", status)
                    code = match[1] if match else "unspecified"
                    self._fail(f"AirPlay receiver/sender error: {code}")
                elif kind in {"eof", "stopped", "paused"}:
                    self._fail(f"AirPlay sender reported {kind}")
                elif kind in {
                    "route",
                    "latency",
                    "clock_ready",
                    "anchor_corrected",
                    "REANCHOR",
                    "content_cut",
                }:
                    fields = re.findall(
                        r"\b([a-z_]+)=(-?\d+|[a-z][a-z0-9_-]*)\b", status
                    )
                    safe = " ".join(f"{key}={value}" for key, value in fields[:12])
                    log.info("AirPlay sender %s %s", kind, safe)
        except Exception:
            self._fail("AirPlay sender status reader failed")

    async def _watch(self):
        code = await self.process.wait()
        self._fail(f"AirPlay sender exited (code={code})")

    async def start(self):
        began = time.monotonic()
        try:
            self.folder = tempfile.TemporaryDirectory(prefix="gpt-live-airplay-")
            fifo = str(Path(self.folder.name) / "control")
            os.mkfifo(fifo, 0o600)
            # RDWR + NONBLOCK avoids blocking the event loop while the sender
            # opens its end. Commands are short atomic writes, never shell input.
            self.command_fd = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
            arguments = [
                AIRPLAY_BINARY,
                "--protocol",
                self.s.airplay_protocol,
                "--port",
                str(self.s.airplay_port),
                "--volume",
                str(self.s.airplay_volume),
                "--latency",
                str(self.s.airplay_latency_ms),
                "--samplerate",
                str(AIRPLAY_RATE),
                "--bitdepth",
                "16",
                "--channels",
                "2",
                "--debug",
                "0",
                "--cmdpipe",
                fifo,
            ]
            if self.s.airplay_protocol == "airplay2":
                # One amp, no multiroom clock server or privileged PTP ports.
                arguments += ["--timing", "ntp"]
            if self.s.airplay_interface:
                arguments += ["--if", self.s.airplay_interface]
            if self.s.airplay_protocol == "raop":
                # Without discovery, explicitly choose classic RAOP rather than
                # libraop's default auth-setup/FairPlay-compatible negotiation.
                # --encrypt alone is ineffective unless et advertises RSA (1).
                arguments += ["--et", "0,1" if self.s.airplay_encryption else "0"]
                if self.s.airplay_encryption:
                    arguments.append("--encrypt")
            arguments.append(self.s.airplay_host)
            self.spawning = asyncio.create_task(
                asyncio.create_subprocess_exec(
                    *arguments,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=16384,
                )
            )
            self.process = await asyncio.shield(self.spawning)
            self.check()
            self.tasks = [
                asyncio.create_task(self._read_status(self.process.stdout)),
                asyncio.create_task(self._read_status(self.process.stderr)),
                asyncio.create_task(self._watch()),
            ]
            async with asyncio.timeout(self.s.tts_stream_start_timeout_s):
                self.stage = "connect"
                await self._wait(self.connected)
                # Prime just one silence frame AFTER connect. Do not queue
                # seconds of silence while authentication/connection runs.
                await self.write(SILENCE)
                self.stage = "initial_audio"
                await self._wait(self.audio)
                self.stage = "start_acknowledgement"
                self._command("START_UNIX_MS=0\nACTION=START\n")
                await self._wait(self.started)
            self.stage = "streaming"
            self.ready.set()
            lead = (
                max(0, self.scheduled_at_ms - time.time() * 1000)
                if self.scheduled_at_ms is not None
                else 0
            )
            log.info(
                "Direct AirPlay ready in %.0f ms (connect=%.0f ms, start=%.0f ms, scheduled_lead=%.0f ms, requested_buffer=%d ms; sender acknowledgement, not acoustic playback)",
                (time.monotonic() - began) * 1000,
                (self.connected_at - began) * 1000,
                (time.monotonic() - self.connected_at) * 1000,
                lead,
                self.s.airplay_latency_ms,
            )
        except asyncio.CancelledError:
            # Shielded spawn must be collected even when Stop races creation.
            try:
                if self.spawning:
                    self.process = await self.spawning
            finally:
                await self.close()
            raise
        except TimeoutError:
            self._fail(f"AirPlay startup timed out (stage={self.stage})")
            await self.close()
            raise
        except FileNotFoundError:
            self._fail("AirPlay sender executable is unavailable")
            await self.close()
            raise
        except BaseException:
            await self.close()
            raise

    async def write(self, frame):
        self.check()
        converted, self.resampler = audioop.ratecv(
            frame, 2, 1, RATE, AIRPLAY_RATE, self.resampler
        )
        self.process.stdin.write(audioop.tostereo(converted, 2, 1, 1))
        # The upstream sender has its own bounded ring; bound pipe backpressure
        # as well so a hung sender cannot stall Stop or accumulate live speech.
        try:
            async with asyncio.timeout(2):
                await self.process.stdin.drain()
        except TimeoutError:
            self._fail("AirPlay PCM input stalled (pipe backpressure)")
            raise
        self.check()

    async def close(self):
        self.closed = True
        self.failed.set()
        async with self.cleanup_lock:
            if self.process:
                if self.process.returncode is None:
                    if self.command_fd is not None:
                        with suppress(OSError):
                            os.write(self.command_fd, b"ACTION=STOP\n")
                    try:
                        async with asyncio.timeout(1):
                            await self.process.wait()
                    except TimeoutError:
                        with suppress(ProcessLookupError):
                            self.process.kill()
                        await self.process.wait()
                for task in self.tasks:
                    task.cancel()
                await asyncio.gather(*self.tasks, return_exceptions=True)
                self.tasks.clear()
            if self.command_fd is not None:
                os.close(self.command_fd)
                self.command_fd = None
            if self.folder:
                self.folder.cleanup()
                self.folder = None
