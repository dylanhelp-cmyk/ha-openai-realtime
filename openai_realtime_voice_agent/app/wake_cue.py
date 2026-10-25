"""One cached, stock-voice acknowledgment; never record a user's microphone.

Vesper is a Live voice, not an audio/speech TTS voice. Prepare its clip once in
a bounded, isolated Live session, then reuse locally without per-wake API work.
"""

import asyncio
import base64
import json
import logging
import os
import re
import time
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import uuid4

import aiohttp

from app.pcm import FRAME_BYTES, SILENCE, dbfs

log = logging.getLogger(__name__)
TEXT = "Yes sir?"
STYLE = "Calm, precise British assistant; restrained, lightly dry delivery. Do not imitate a real person's voice."
LIVE_URL = "wss://api.openai.com/v1/live/sessions"


def normalized(text):
    return re.sub(r"[^a-z]", "", text.lower())


def trim_clip(audio):
    voiced = [
        i
        for i in range(0, len(audio) - FRAME_BYTES + 1, FRAME_BYTES)
        if dbfs(audio[i : i + FRAME_BYTES]) > -50
    ]
    if not voiced:
        raise ValueError("No cue speech")
    first = max(0, voiced[0] - 2 * FRAME_BYTES)
    last = min(len(audio), voiced[-1] + 7 * FRAME_BYTES)
    clip = audio[first:last]
    if not 0.2 <= len(clip) / 48000 <= 2.5:
        raise ValueError("Unexpected cue duration")
    return bytes(clip) + bytes((-len(clip)) % FRAME_BYTES)


class WakeCueCache:
    def __init__(self, settings, http, directory="/data"):
        self.s, self.http = settings, http
        key = sha256(
            json.dumps([1, settings.openai_voice, TEXT, STYLE]).encode()
        ).hexdigest()[:16]
        self.path = Path(directory) / f"wake-cue-{key}.json"
        self.audio = None
        self.task = None

    def load(self):
        try:
            if self.path.stat().st_size > 200000:
                return False
            data = json.loads(self.path.read_text())
            audio = base64.b64decode(data["pcm"], validate=True)
            if (
                data["voice"] != self.s.openai_voice
                or data["text"] != TEXT
                or data["sha256"] != sha256(audio).hexdigest()
                or len(audio) % FRAME_BYTES
                or not 9600 <= len(audio) <= 120000
            ):
                return False
            self.audio = audio
            return True
        except (OSError, ValueError, KeyError, TypeError):
            return False

    async def prepare(self):
        if self.load():
            log.info(
                "Wake cue cache ready: voice=%s duration_ms=%.0f (local reuse; no generation session)",
                self.s.openai_voice,
                len(self.audio) / 48,
            )
            return
        log.info(
            "Preparing wake cue once: voice=%s text=%r (one short paid Live session; no microphone/tools)",
            self.s.openai_voice,
            TEXT,
        )
        try:
            audio = await self.generate()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = None
            try:
                with NamedTemporaryFile(
                    "w", dir=self.path.parent, delete=False
                ) as file:
                    temp = file.name
                    json.dump(
                        {
                            "voice": self.s.openai_voice,
                            "text": TEXT,
                            "sha256": sha256(audio).hexdigest(),
                            "pcm": base64.b64encode(audio).decode(),
                        },
                        file,
                    )
                os.replace(temp, self.path)
            finally:
                if temp and Path(temp).exists():
                    Path(temp).unlink()
            self.audio = audio
            log.info(
                "Wake cue cache ready: voice=%s duration_ms=%.0f (stored assistant cue only)",
                self.s.openai_voice,
                len(audio) / 48,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # No automatic paid retry at each wake; never log API payloads.
            log.warning(
                "Wake cue preparation failed (%s); no automatic retry until add-on restart",
                type(exc).__name__,
            )

    async def generate(self):
        ws = None
        pump = None
        final = False
        usage = None

        async def send(event):
            await ws.send_json({"event_id": uuid4().hex, **event})

        async def silence():
            deadline = time.monotonic()
            while True:
                await send(
                    {
                        "type": "session.input_audio.append",
                        "audio": base64.b64encode(SILENCE).decode(),
                    }
                )
                deadline = max(deadline + 0.02, time.monotonic())
                await asyncio.sleep(max(0, deadline - time.monotonic()))

        try:
            async with asyncio.timeout(20):
                ws = await self.http.ws_connect(
                    LIVE_URL,
                    headers={"Authorization": f"Bearer {self.s.openai_api_key}"},
                    heartbeat=20,
                    max_msg_size=512 * 1024,
                )
                prompt = f"{STYLE} Say exactly {TEXT!r} once, then remain silent. No other words or delegation."
                await send(
                    {
                        "type": "session.start",
                        "session": {
                            "model": "gpt-live-1",
                            "instructions": prompt,
                            "audio": {
                                "format": {"type": "audio/pcm", "rate": 24000},
                                "output": {"voice": self.s.openai_voice},
                            },
                            "delegation": {"type": "client"},
                            "store": False,
                            "input": [
                                {
                                    "type": "message",
                                    "role": "developer",
                                    "content": [{"type": "input_text", "text": prompt}],
                                }
                            ],
                        },
                    }
                )
                first = await ws.receive()
                if (
                    first.type != aiohttp.WSMsgType.TEXT
                    or json.loads(first.data).get("type") != "session.started"
                ):
                    raise ValueError("Cue session not ready")
                pump = asyncio.create_task(silence())
                await send(
                    {
                        "type": "session.commentary.append",
                        "delegation_id": None,
                        "content": prompt,
                    }
                )
                audio, transcript = bytearray(), ""
                last_audio = None
                quiet_frames = 0
                pending = bytearray()
                spoken = False
                while True:
                    try:
                        async with asyncio.timeout(1):
                            message = await ws.receive()
                    except TimeoutError:
                        if (
                            last_audio
                            and time.monotonic() - last_audio >= 1
                            and normalized(transcript) == "yessir"
                        ):
                            break
                        continue
                    if message.type != aiohttp.WSMsgType.TEXT:
                        raise ValueError("Cue connection closed")
                    event = json.loads(message.data)
                    kind = event.get("type")
                    if kind == "session.output_audio.delta":
                        chunk = base64.b64decode(event.get("delta", ""), validate=True)
                        if len(chunk) % 2:
                            raise ValueError("Cue PCM alignment")
                        audio.extend(chunk)
                        pending.extend(chunk)
                        last_audio = time.monotonic()
                        if len(audio) > 6 * 48000:
                            raise ValueError("Cue audio limit")
                        while len(pending) >= FRAME_BYTES:
                            loud = dbfs(pending[:FRAME_BYTES]) > -50
                            del pending[:FRAME_BYTES]
                            spoken |= loud
                            quiet_frames = 0 if loud else quiet_frames + 1
                    elif kind == "session.output_transcript.delta":
                        transcript += event.get("delta", "")
                        if len(transcript) > 100 or not "yessir".startswith(
                            normalized(transcript)
                        ):
                            raise ValueError("Cue wording did not match")
                    elif kind == "session.usage.updated":
                        usage = event.get("usage", {}).get("seconds")
                    elif kind == "session.closed":
                        final = True
                        usage = event.get("usage", {}).get("seconds", usage)
                        raise ValueError("Cue unexpectedly ended")
                    elif kind in {"error", "session.delegation.created"}:
                        raise ValueError("Cue rejected or unexpectedly ended")
                    if (
                        spoken
                        and quiet_frames >= 20
                        and normalized(transcript) == "yessir"
                    ):
                        break
                if normalized(transcript) != "yessir":
                    raise ValueError("Cue wording did not match")
                return trim_clip(audio)
        finally:
            if pump:
                pump.cancel()
                await asyncio.gather(pump, return_exceptions=True)
            if ws:
                try:
                    async with asyncio.timeout(2):
                        if not ws.closed and not final:
                            await send({"type": "session.close"})
                            while True:
                                message = await ws.receive()
                                if message.type != aiohttp.WSMsgType.TEXT:
                                    break
                                event = json.loads(message.data)
                                if event.get("type") == "session.closed":
                                    final = True
                                    usage = event.get("usage", {}).get("seconds", usage)
                                    break
                except Exception:
                    pass
                finally:
                    try:
                        async with asyncio.timeout(2):
                            await ws.close()
                    except TimeoutError:
                        pass
                log.info(
                    "Wake cue generation closed: usage_seconds=%s final_confirmed=%s",
                    usage,
                    final,
                )

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
