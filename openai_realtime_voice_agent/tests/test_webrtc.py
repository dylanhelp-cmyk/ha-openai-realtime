"""Local WebRTC peer/HTTP simulator: no OpenAI calls, credentials or paid usage."""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription

from app.webrtc import LiveWebRTC, MicrophoneTrack
from app.pcm import FRAME_BYTES, RATE, SILENCE
from app.settings import Settings
from app.live_session import LiveSession
from app.device import Device
from types import SimpleNamespace
from test_live import Socket


class WebRTCTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # The synthetic peer uses loopback only; no host adapter enumeration,
        # LAN discovery or external network access belongs in this test.
        self.addresses = patch(
            "aioice.ice.get_host_addresses", return_value=["127.0.0.1"]
        )
        self.addresses.start()
        self.addCleanup(self.addresses.stop)

    async def test_microphone_track_is_paced_and_never_reads_pre_start_audio(self):
        ready = asyncio.Event()
        calls = []
        track = MicrophoneTrack(
            lambda: calls.append(True) or bytes([1, 0]) * 480, ready
        )
        try:
            first = await track.recv()
            self.assertFalse(calls)
            self.assertEqual(first.sample_rate, RATE)
            self.assertEqual(first.pts, 0)
            self.assertEqual(bytes(first.planes[0]), SILENCE)
            ready.set()
            second = await track.recv()
            self.assertEqual(len(calls), 1)
            self.assertEqual(second.pts, 480)
            self.assertEqual(len(bytes(second.planes[0])), FRAME_BYTES)
        finally:
            track.stop()

    async def test_real_local_peer_carries_audio_and_json_and_closes_gracefully(self):
        peers, post_bodies, commands, received = [], [], [], []
        media_arrived = asyncio.Event()
        server_tasks = []

        async def create(request):
            body = await request.json()
            post_bodies.append(body)
            peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
            peers.append(peer)
            ready = asyncio.Event()
            ready.set()
            peer.addTrack(MicrophoneTrack(lambda: bytes([232, 3]) * 480, ready))

            @peer.on("datachannel")
            def datachannel(channel):
                channel.send(
                    json.dumps(
                        {"type": "session.started", "session": {"id": "live_test"}}
                    )
                )

                @channel.on("message")
                def command(data):
                    event = json.loads(data)
                    commands.append(event)
                    if event.get("type") == "session.close":
                        channel.send(
                            json.dumps(
                                {"type": "session.closed", "usage": {"seconds": 2.5}}
                            )
                        )

            @peer.on("track")
            def track(track):
                async def drain():
                    try:
                        while True:
                            await track.recv()
                    except Exception:
                        pass

                server_tasks.append(asyncio.create_task(drain()))

            await peer.setRemoteDescription(
                RTCSessionDescription(sdp=body["transport"]["sdp"], type="offer")
            )
            answer = await peer.createAnswer()
            await peer.setLocalDescription(answer)
            return web.json_response(
                {
                    "session": {"id": "live_test"},
                    "transport": {"type": "webrtc", "sdp": peer.localDescription.sdp},
                },
                status=201,
            )

        app = web.Application()
        app.router.add_post("/create", create)
        input_calls = []

        async def audio(frame):
            received.append(frame)
            media_arrived.set()

        async with TestServer(app) as server, aiohttp.ClientSession() as http:
            http.ws_connect = AsyncMock(
                side_effect=AssertionError("No sideband needed")
            )
            client = LiveWebRTC(
                http,
                "fake",
                lambda: input_calls.append(True) or SILENCE,
                audio,
                close_timeout=1,
            )
            try:
                with patch(
                    "app.webrtc.LIVE_CREATE_URL", str(server.make_url("/create"))
                ):
                    await asyncio.wait_for(
                        client.connect(
                            {
                                "model": "gpt-live-1",
                                "audio": {
                                    "format": {"type": "audio/pcm", "rate": 24000},
                                    "output": {"voice": "cedar"},
                                },
                                "delegation": {
                                    "type": "responses",
                                    "responses": {"model": "gpt-6-luna"},
                                },
                            }
                        ),
                        5,
                    )
                self.assertTrue(client.ready.is_set())
                async with asyncio.timeout(2):
                    while not input_calls:
                        await asyncio.sleep(.02)
                event = await asyncio.wait_for(client.receive(), 2)
                self.assertEqual(json.loads(event.data)["type"], "session.started")
                for kind in ("session.start", "session.input_audio.append"):
                    with self.assertRaises(ValueError):
                        await client.send_json({"type": kind})
                await client.send_json(
                    {"type": "session.commentary.append", "content": "test"}
                )
                await asyncio.wait_for(media_arrived.wait(), 3)
                self.assertTrue(received)
                self.assertTrue(all(len(frame) % 2 == 0 for frame in received))
                await asyncio.sleep(0.05)
                self.assertTrue(input_calls)
                self.assertEqual(post_bodies[0]["session"]["model"], "gpt-live-1")
                self.assertNotIn("format", post_bodies[0]["session"]["audio"])
                self.assertEqual(
                    post_bodies[0]["session"]["delegation"]["responses"]["model"],
                    "gpt-6-luna",
                )
                self.assertEqual(len(post_bodies), 1)
                await client.close()
                self.assertEqual(client.final_event["usage"]["seconds"], 2.5)
                self.assertTrue(client.closed)
                self.assertEqual(sum(e["type"] == "session.close" for e in commands), 1)
                self.assertFalse(
                    any(
                        e["type"] in {"session.start", "session.input_audio.append"}
                        for e in commands
                    )
                )
                http.ws_connect.assert_not_awaited()
            finally:
                await client.close()
                await asyncio.gather(*(p.close() for p in peers))
                for task in server_tasks:
                    task.cancel()
                await asyncio.gather(*server_tasks, return_exceptions=True)

    async def test_failed_paid_creation_uses_same_session_sideband_close(self):
        commands, attached = [], []

        async def create(request):
            return web.json_response(
                {
                    "session": {"id": "live_failed"},
                    "transport": {"type": "webrtc", "sdp": "invalid SDP"},
                },
                status=201,
            )

        async def attach(request):
            attached.append(request.match_info["session"])
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            event = await socket.receive_json()
            commands.append(event)
            await socket.send_json({"type": "session.closed", "usage": {"seconds": 15}})
            await socket.close()
            return socket

        app = web.Application()
        app.router.add_post("/create", create)
        app.router.add_get("/attach/{session}", attach)
        async with TestServer(app) as server, aiohttp.ClientSession() as http:
            client = LiveWebRTC(
                http, "fake", lambda: SILENCE, AsyncMock(), close_timeout=1
            )
            with (
                patch("app.webrtc.LIVE_CREATE_URL", str(server.make_url("/create"))),
                patch(
                    "app.webrtc.LIVE_ATTACH_URL",
                    str(server.make_url("/attach/")) + "{session_id}",
                ),
            ):
                try:
                    with self.assertRaises(ValueError):
                        await client.connect({"model": "gpt-live-1"})
                finally:
                    await client.close()
            self.assertEqual(attached, ["live_failed"])
            self.assertEqual(commands, [{"type": "session.close"}])
            self.assertEqual(client.final_event["usage"]["seconds"], 15)
            self.assertTrue(client.closed)

    async def test_audio_and_session_start_cannot_be_sent_over_data_channel(self):
        client = LiveWebRTC(None, "fake", lambda: SILENCE, AsyncMock())
        try:
            with self.assertRaises(RuntimeError):
                await client.send_json({"type": "session.start"})
        finally:
            await client.close()

    async def test_live_session_selects_webrtc_without_primary_websocket_audio(self):
        made = []

        class Transport:
            def __init__(self, *args):
                made.append(self)
                self.ready = asyncio.Event()
                self.closed = False
                self.final_event = None
                self.offer_ms, self.http_ms, self.peer_ms = 1, 2, 3
                self.send_json = AsyncMock()

            async def connect(self, config):
                self.config = config

            async def receive(self):
                return aiohttp.WSMessage(
                    aiohttp.WSMsgType.TEXT, '{"type":"session.started"}', ""
                )

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self.closed:
                    raise StopAsyncIteration
                self.final_event = {"type": "session.closed", "usage": {"seconds": 1}}
                return aiohttp.WSMessage(
                    aiohttp.WSMsgType.TEXT, json.dumps(self.final_event), ""
                )

            async def close(self):
                self.closed = True

        settings = Settings(live_transport="webrtc")
        http = SimpleNamespace(
            ws_connect=AsyncMock(side_effect=AssertionError("No primary WebSocket"))
        )
        device = Device(Socket(), settings)
        tools = SimpleNamespace(catalog="", tools=[])
        with patch("app.webrtc.LiveWebRTC", Transport):
            session = LiveSession(settings, device, http, tools, None, "fake")
            await session.run()
        self.assertEqual(len(made), 1)
        self.assertEqual(made[0].config["model"], "gpt-live-1")
        self.assertTrue(made[0].ready.is_set())
        self.assertTrue(made[0].closed)
        self.assertTrue(session.usage_confirmed)
        self.assertEqual(session.usage_seconds, 1)
        events = [call.args[0]["type"] for call in made[0].send_json.await_args_list]
        self.assertNotIn("session.start", events)
        self.assertNotIn("session.input_audio.append", events)
        self.assertNotIn("session.close", events)  # adapter owns one graceful close
        http.ws_connect.assert_not_awaited()
