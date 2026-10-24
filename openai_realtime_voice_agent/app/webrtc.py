"""Opt-in GPT-Live WebRTC media tracks with the same JSON control events.

No Realtime API/model substitution, sideband audio or automatic paid retries.
"""

import asyncio
import json
import logging
import re
import time
from contextlib import suppress
from fractions import Fraction

import aiohttp
from aiortc import (
    AudioStreamTrack,
    RTCConfiguration,
    RTCPeerConnection,
    RTCSessionDescription,
)
from aiortc.mediastreams import MediaStreamError
from av import AudioFrame, AudioResampler

from app.pcm import RATE, FRAME_SAMPLES, SILENCE

LIVE_CREATE_URL = "https://api.openai.com/v1/live/sessions"
LIVE_ATTACH_URL = "wss://api.openai.com/v1/live/sessions/{session_id}/attach"
log = logging.getLogger(__name__)


class MicrophoneTrack(AudioStreamTrack):
    def __init__(self, source, ready):
        super().__init__()
        self.source, self.ready = source, ready
        self.samples = 0
        self.deadline = None

    async def recv(self):
        if self.readyState != "live":
            raise MediaStreamError
        now = time.monotonic()
        if self.deadline is None or now - self.deadline > 0.1:
            self.deadline = now
        await asyncio.sleep(max(0, self.deadline - now))
        self.deadline += 0.02
        audio = self.source() if self.ready.is_set() else SILENCE
        frame = AudioFrame(format="s16", layout="mono", samples=FRAME_SAMPLES)
        frame.planes[0].update(audio)
        frame.sample_rate, frame.time_base, frame.pts = (
            RATE,
            Fraction(1, RATE),
            self.samples,
        )
        self.samples += FRAME_SAMPLES
        return frame


class LiveWebRTC:
    def __init__(self, http, key, input_source, audio_sink, close_timeout=2):
        self.http, self.key, self.audio_sink = http, key, audio_sink
        self.close_timeout = close_timeout
        self.pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self.channel = self.pc.createDataChannel("oai-events")
        self.ready = asyncio.Event()
        self.channel_ready = asyncio.Event()
        self.final = asyncio.Event()
        self.final_event = None
        self.close_requested = False
        self.closed = False
        self.close_task = None
        self.session_id = None
        self.events = asyncio.Queue(maxsize=512)
        self.tasks = set()
        self.offer_ms = self.http_ms = self.peer_ms = 0
        self.microphone = MicrophoneTrack(input_source, self.ready)
        self.pc.addTrack(self.microphone)

        @self.channel.on("open")
        def opened():
            self.channel_ready.set()

        @self.channel.on("message")
        def message(value):
            try:
                event = json.loads(value)
                if not isinstance(event, dict):
                    raise ValueError("Invalid control event")
                if event.get("type") == "session.closed":
                    self.final_event = event
                    self.final.set()
                self._enqueue(
                    aiohttp.WSMessage(aiohttp.WSMsgType.TEXT, json.dumps(event), "")
                )
            except (ValueError, TypeError):
                self._error("Invalid WebRTC control event")

        @self.channel.on("close")
        def channel_closed():
            if not self.closed and not self.final.is_set():
                self._error("WebRTC control channel closed")

        @self.pc.on("connectionstatechange")
        async def state_changed():
            if self.pc.connectionState in {"failed", "closed"} and not self.closed:
                self._error("WebRTC peer connection failed")

        @self.pc.on("track")
        def track_added(track):
            if track.kind == "audio":
                task = asyncio.create_task(self._audio(track))
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)

    def _enqueue(self, message):
        try:
            self.events.put_nowait(message)
        except asyncio.QueueFull:
            # Fail bounded rather than silently dropping tool calls/session events.
            while not self.events.empty():
                self.events.get_nowait()
            self.events.put_nowait(
                aiohttp.WSMessage(
                    aiohttp.WSMsgType.ERROR,
                    RuntimeError("WebRTC event queue overflow"),
                    "",
                )
            )

    def _error(self, message):
        self._enqueue(
            aiohttp.WSMessage(aiohttp.WSMsgType.ERROR, RuntimeError(message), "")
        )

    async def connect(self, config):
        began = time.monotonic()
        offer = await self.pc.createOffer()
        await self.pc.setLocalDescription(offer)
        self.offer_ms = (time.monotonic() - began) * 1000
        # Negotiated media tracks determine the codec/rate. PCM format belongs
        # only on the primary WebSocket API, not the WebRTC session config.
        config = {
            **config,
            "audio": {
                k: v for k, v in config.get("audio", {}).items() if k != "format"
            },
        }
        request_at = time.monotonic()
        async with self.http.post(
            LIVE_CREATE_URL,
            headers={"Authorization": f"Bearer {self.key}"},
            json={
                "session": config,
                "transport": {"type": "webrtc", "sdp": self.pc.localDescription.sdp},
            },
        ) as response:
            if response.status != 201:
                raise RuntimeError(
                    f"GPT-Live WebRTC creation returned HTTP {response.status}"
                )
            result = await response.json()
        self.http_ms = (time.monotonic() - request_at) * 1000
        self.session_id = result.get("session", {}).get("id")
        if not isinstance(self.session_id, str) or not re.fullmatch(
            r"[a-zA-Z0-9_-]{1,256}", self.session_id
        ):
            raise RuntimeError("GPT-Live WebRTC response has no valid session ID")
        transport = result.get("transport", {})
        if transport.get("type") != "webrtc" or not isinstance(
            transport.get("sdp"), str
        ):
            raise RuntimeError("GPT-Live WebRTC response has no SDP answer")
        remote_at = time.monotonic()
        await self.pc.setRemoteDescription(
            RTCSessionDescription(sdp=transport["sdp"], type="answer")
        )
        await self.channel_ready.wait()
        self.peer_ms = (time.monotonic() - remote_at) * 1000
        return self

    async def _audio(self, track):
        resampler = AudioResampler(format="s16", layout="mono", rate=RATE)
        try:
            while not self.closed:
                frame = await track.recv()
                for output in resampler.resample(frame):
                    await self.audio_sink(bytes(output.planes[0])[: output.samples * 2])
        except asyncio.CancelledError:
            raise
        except Exception:
            if not self.closed and not self.final.is_set():
                self._error("WebRTC audio track ended")

    async def send_json(self, event):
        if self.closed or self.channel.readyState != "open":
            raise RuntimeError("WebRTC control channel is not open")
        if event.get("type") in {"session.start", "session.input_audio.append"}:
            raise ValueError(
                "WebRTC audio/session creation must not use JSON append/start"
            )
        if self.channel.bufferedAmount > 1024 * 1024:
            raise RuntimeError("WebRTC control channel backpressure")
        self.channel.send(json.dumps(event))
        if event.get("type") == "session.close":
            self.close_requested = True

    async def receive(self):
        return await self.events.get()

    def __aiter__(self):
        return self

    async def __anext__(self):
        message = await self.receive()
        if message.type == aiohttp.WSMsgType.CLOSED:
            raise StopAsyncIteration
        return message

    async def close(self):
        if self.close_task is None:
            self.close_task = asyncio.create_task(self._close())
        await asyncio.shield(self.close_task)

    async def _close(self):
        if self.closed:
            return
        try:
            if not self.final.is_set() and self.channel.readyState == "open":
                with suppress(Exception):
                    if not self.close_requested:
                        await self.send_json({"type": "session.close"})
                    await asyncio.wait_for(self.final.wait(), self.close_timeout)
            # A failed peer after paid creation may never open its data channel.
            # Official sideband attaches to the SAME session solely to close it.
            if self.session_id and not self.final.is_set():
                with suppress(Exception):
                    async with asyncio.timeout(self.close_timeout):
                        async with self.http.ws_connect(
                            LIVE_ATTACH_URL.format(session_id=self.session_id),
                            headers={"Authorization": f"Bearer {self.key}"},
                            max_msg_size=4 * 1024 * 1024,
                        ) as sideband:
                            await sideband.send_json({"type": "session.close"})
                            async for message in sideband:
                                if message.type == aiohttp.WSMsgType.TEXT:
                                    event = json.loads(message.data)
                                    if event.get("type") == "session.closed":
                                        self.final_event = event
                                        self.final.set()
                                        break
        finally:
            if self.session_id and not self.final.is_set():
                log.warning(
                    "GPT-Live WebRTC close unconfirmed after bounded control/sideband close attempts"
                )
            self.closed = True
            self.microphone.stop()
            for task in tuple(self.tasks):
                task.cancel()
            await asyncio.gather(*tuple(self.tasks), return_exceptions=True)
            self.tasks.clear()
            await self.pc.close()
            self._enqueue(aiohttp.WSMessage(aiohttp.WSMsgType.CLOSED, None, ""))
