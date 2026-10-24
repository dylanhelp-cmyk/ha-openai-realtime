import asyncio, json, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
from app.device import Device
from app.live_audio import AudioHTTPServer
from app.live_session import LiveSession
from app.main import VoiceBridge
from app.output import SpeechOutput
from app.pcm import InputAudio, FRAME_BYTES, SILENCE
from app.settings import Settings

TONE = (1000).to_bytes(2, "little") * 480


class Socket:
    def __init__(self):
        self.closed = False
        self.events = []
        self.audio = []

    async def send_str(self, x):
        self.events.append(json.loads(x))

    async def send_bytes(self, x):
        self.audio.append(x)


async def until(test):
    async with asyncio.timeout(3):
        while not test():
            await asyncio.sleep(0.01)


class LiveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.s = Settings(
            openai_api_key="fake",
            opening_greeting="Yes, Dylan?",
            microphone_mode="gated",
            tts_start_allowance_ms=0,
            mic_reopen_delay_ms=0,
            output_silence_ms=100,
        )
        self.http = aiohttp.ClientSession()
        self.socket = Socket()
        self.device = Device(self.socket, self.s)
        await self.device.mic(True)
        self.tools = SimpleNamespace(
            catalog="",
            tools=[],
            call=AsyncMock(return_value={"isError": False, "result": "done"}),
        )
        self.live = LiveSession(
            self.s, self.device, self.http, self.tools, None, "fake-token"
        )

    async def asyncTearDown(self):
        await self.live.close()
        await self.http.close()

    async def test_greeting_and_backend_are_separate_and_credentials_stay_local(self):
        c, g = self.live.session_config()
        self.assertEqual(c["model"], "gpt-live-1")
        self.assertFalse(c["store"])
        self.assertIn("Yes, Dylan?", c["input"][0]["content"][0]["text"])
        self.assertIn("<|SpawnThinking|>", c["instructions"])
        self.assertNotIn("fake-token", json.dumps(c))
        self.assertEqual(c["delegation"]["type"], "responses")

    async def test_greeting_can_be_disabled(self):
        self.s.opening_greeting = ""
        self.assertNotIn("input", self.live.session_config()[0])

    async def test_keepalive_silence_does_not_count_as_speech_or_gate_microphone(self):
        await self.live.output.write(SILENCE * 20)
        await asyncio.sleep(0.01)
        self.assertFalse(self.live.output.active)
        self.assertTrue(self.device.mic_enabled)
        self.assertTrue(all(frame == SILENCE for frame in self.socket.audio))

    async def test_gated_mic_waits_for_real_playback(self):
        await self.live.output.write(TONE * 5)
        await until(lambda: not self.device.mic_enabled)
        self.assertFalse(self.device.mic_enabled)
        await self.live.output.write(SILENCE * 5)
        await until(lambda: not self.live.output.active and self.device.mic_enabled)
        self.assertGreaterEqual(len(self.socket.audio), 5)
        self.assertEqual(self.device.phase, "listening")

    async def test_full_duplex_keeps_microphone_open(self):
        self.s.microphone_mode = "full_duplex"
        await self.live.output.write(TONE)
        await until(lambda: self.live.output.speaking)
        self.assertTrue(self.device.mic_enabled)

    async def test_full_duplex_listening_never_delays_or_discards_user_audio(self):
        self.s.microphone_mode = "full_duplex"
        self.s.mic_reopen_delay_ms = 5000
        self.live.receive_audio(TONE)
        captured = bytes(self.live.input.data)
        self.assertTrue(captured)
        await asyncio.wait_for(self.live.listening(), 0.1)
        self.assertEqual(bytes(self.live.input.data), captured)
        self.assertTrue(self.device.mic_enabled)

    async def test_stop_during_audio_never_reopens_microphone(self):
        await self.live.output.write(TONE * 10)
        await self.live.close()
        await asyncio.sleep(0.02)
        self.assertFalse(self.device.mic_enabled)
        idx = next(
            i for i, x in enumerate(self.socket.events) if x["type"] == "session_end"
        )
        self.assertFalse(
            any(
                x.get("enabled") for x in self.socket.events[idx:] if x["type"] == "mic"
            )
        )

    async def test_packet_gap_only_ends_speech_activity_not_the_transport(self):
        await self.live.output.write(TONE)
        await until(lambda: self.live.output.speaking)
        await until(lambda: not self.live.output.active)
        self.assertFalse(self.live.output.worker.done())
        self.assertTrue(self.device.mic_enabled)

    async def test_all_unique_tool_results_precede_continuation(self):
        self.live.send = AsyncMock()
        await self.live.backend_event(
            {"type": "response.created", "response": {"id": "r"}}, "d"
        )
        for cid in ("a", "b", "a"):
            await self.live.backend_event(
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "function_call",
                        "call_id": cid,
                        "name": "HassTurnOn",
                        "arguments": "{}",
                    },
                },
                "d",
            )
        await self.live.backend_event(
            {"type": "response.completed", "response": {"id": "r", "output": []}}, "d"
        )
        await asyncio.gather(*list(self.live.tool_tasks))
        self.assertEqual(self.tools.call.await_count, 2)
        self.assertEqual(
            [c.args[0]["type"] for c in self.live.send.call_args_list],
            ["response.item.create", "response.item.create", "response.create"],
        )

    async def test_interleaved_delegations_keep_correct_call_ownership(self):
        for i in (1, 2):
            await self.live.backend_event(
                {"type": "response.created", "response": {"id": f"r{i}"}}, f"d{i}"
            )
        await self.live.backend_event(
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "call_id": "c",
                    "name": "test",
                    "arguments": "{}",
                },
            },
            "d2",
        )
        self.assertFalse(self.live.responses["r1"])
        self.assertEqual(len(self.live.responses["r2"]), 1)

    async def test_bad_arguments_return_error_without_executing_mutation(self):
        self.live.send = AsyncMock()
        await self.live._complete_tools(
            [{"call_id": "a", "name": "HassTurnOn", "arguments": "broken"}]
        )
        self.tools.call.assert_not_awaited()
        self.assertTrue(
            json.loads(self.live.send.call_args_list[0].args[0]["item"]["output"])[
                "isError"
            ]
        )

    async def test_closed_session_does_not_execute_or_replay_calls(self):
        await self.live.close()
        self.live.send = AsyncMock()
        await self.live._complete_tools(
            [{"call_id": "late", "name": "HassTurnOn", "arguments": "{}"}]
        )
        self.tools.call.assert_not_awaited()
        self.live.send.assert_not_awaited()

    async def test_real_websocket_sends_one_greeting_continuous_audio_and_idle_close(
        self,
    ):
        received = []

        async def fake(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            received.append(await ws.receive_json())
            await ws.send_json({"type": "session.started", "session": {"id": "test"}})
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                e = json.loads(msg.data)
                received.append(e)
                if e["type"] == "session.close":
                    await ws.send_json(
                        {"type": "session.closed", "usage": {"seconds": 1.25}}
                    )
                    break
            await ws.close()
            return ws

        app = web.Application()
        app.router.add_get("/live", fake)
        self.s.idle_timeout_seconds = 0.12
        async with TestServer(app) as server:
            with patch("app.live_session.LIVE_URL", str(server.make_url("/live"))):
                await asyncio.wait_for(self.live.run(), 3)
        kinds = [e["type"] for e in received]
        self.assertEqual(kinds[0], "session.start")
        self.assertEqual(kinds.count("session.commentary.append"), 1)
        self.assertIn("session.input_audio.append", kinds)
        self.assertIn("session.close", kinds)
        self.assertTrue(self.live.usage_confirmed)
        self.assertEqual(self.live.usage_seconds, 1.25)
        self.assertFalse(self.device.mic_enabled)

    async def test_failed_startup_closes_output_and_microphone(self):
        self.http.ws_connect = AsyncMock(side_effect=OSError("unavailable"))
        await self.live.run()
        self.assertTrue(self.live.closed)
        self.assertTrue(self.live.output.closed)
        self.assertFalse(self.device.mic_enabled)

    async def test_speaker_failure_before_live_connect_does_not_start_a_paid_session(
        self,
    ):
        self.http.ws_connect = AsyncMock()
        await self.live.fail("Speaker startup failed")
        await self.live.run()
        self.http.ws_connect.assert_not_awaited()
        self.assertTrue(self.live.closed)
        self.assertFalse(self.device.mic_enabled)

    async def test_hard_duration_limit_applies_even_with_stuck_backend(self):
        self.s.max_session_seconds = 0.01
        self.live.responses["stuck"] = []
        self.live.ws = SimpleNamespace(close=AsyncMock(), closed=False)
        await asyncio.wait_for(self.live._watchdog(), 1)
        self.live.ws.close.assert_awaited_once()
        self.live.ws = None


class AudioTests(unittest.IsolatedAsyncioTestCase):
    async def test_pcm_resampling_is_bounded_and_sample_aligned(self):
        a = InputAudio(100)
        a.append(b"\0")
        a.append(TONE * 20 + b"\0")
        self.assertGreater(a.dropped, 0)
        self.assertLessEqual(len(a.data), a.limit)
        self.assertEqual(len(a.frame()), FRAME_BYTES)
        a.clear()
        self.assertEqual(a.frame(), SILENCE)

    async def test_http_mp3_arrives_before_generation_finishes(self):
        server = AudioHTTPServer("http://127.0.0.1:0", 0, "127.0.0.1")
        await server.start()
        http = aiohttp.ClientSession()
        s = Settings(
            tts_start_allowance_ms=0, tts_stream_prebuffer_ms=0, output_silence_ms=100
        )
        fetched = asyncio.Event()
        requests = []
        downloads = []

        async def download(url):
            async with http.get(url + "?_=wiim-cache") as r:
                self.assertTrue(await r.content.read(128))
                fetched.set()
                return await r.read()

        async def ha(method, path, **kwargs):
            if method == "GET":
                return {"state": "playing", "attributes": {"media_title": "Spotify"}}
            if path.endswith("play_media"):
                requests.append(kwargs["json"])
                downloads.append(
                    asyncio.create_task(download(kwargs["json"]["media_content_id"]))
                )
            return []

        out = SpeechOutput(
            s,
            Device(Socket(), s),
            http,
            server,
            AsyncMock(),
            AsyncMock(),
            AsyncMock(),
            "fake",
        )
        out._ha = ha
        try:
            await out.write(TONE * 50)
            await asyncio.wait_for(fetched.wait(), 3)
            self.assertFalse(out.encoder.resource.finished.is_set())
            await out.write(SILENCE * 5)
            await until(lambda: not out.active)
            await out.close()
            await asyncio.gather(*downloads)
            self.assertTrue(requests[0]["announce"])
            self.assertNotIn("authSig", requests[0]["media_content_id"])
            self.assertFalse(
                out._announce(
                    {"attributes": {"media_title": requests[0]["media_content_id"]}}
                )
            )
        finally:
            await out.close()
            await http.close()
            await server.close()

    async def test_one_byte_probe_does_not_start_playback_clock(self):
        server = AudioHTTPServer("http://127.0.0.1:0", 0, "127.0.0.1")
        await server.start()
        try:
            r = server.create()
            await r.append(b"mp3")
            await r.finish()
            async with aiohttp.ClientSession() as http:
                async with http.get(
                    server.url(r), headers={"Range": "bytes=0-0"}
                ) as response:
                    self.assertEqual(response.status, 206)
                    self.assertEqual(await response.read(), b"m")
            self.assertFalse(r.fetched.is_set())
        finally:
            await server.close()


class ConfigTests(unittest.TestCase):
    def test_container_release_extracts_the_manifest_version(self):
        import os
        import subprocess
        import tempfile

        import yaml

        root = Path(__file__).parents[2]
        workflow = yaml.safe_load(
            (root / ".github/workflows/build-addon.yml").read_text()
        )
        step = next(
            s for s in workflow["jobs"]["build"]["steps"] if s.get("id") == "version"
        )
        manifest = yaml.safe_load(
            (root / "openai_realtime_voice_agent/config.yaml").read_text()
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            subprocess.run(
                ["bash", "-e", "-c", step["run"]],
                check=True,
                cwd=root,
                env={**os.environ, "GITHUB_OUTPUT": str(output)},
                capture_output=True,
            )
            self.assertEqual(
                output.read_text().strip(), f"version={manifest['version']}"
            )

    def test_schema_and_translations_have_no_dead_realtime_fields(self):
        import yaml

        root = Path(__file__).parents[1]
        c = yaml.safe_load((root / "config.yaml").read_text())
        self.assertEqual(set(c["options"]), set(c["schema"]))
        self.assertEqual(c["slug"], "openai_realtime_voice_agent")
        for lang in ("en", "nl"):
            labels = yaml.safe_load(
                (root / "translations" / f"{lang}.yaml").read_text()
            )["configuration"]
            self.assertEqual(set(c["schema"]), set(labels))
        for old in (
            "openai_model",
            "vad_eagerness",
            "transcription_model",
            "tts_delivery",
            "follow_up_open_delay_ms",
        ):
            self.assertNotIn(old, c["schema"])

    def test_external_audio_requires_reachable_url_and_separate_port(self):
        s = Settings(
            openai_api_key="fake",
            tts_output="media_player",
            tts_media_player="media_player.test",
        )
        with self.assertRaises(ValueError):
            s.validate()
        s.tts_stream_base_url = "http://192.168.1.1:18082"
        s.tts_stream_port = s.websocket_port
        with self.assertRaises(ValueError):
            s.validate()


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_persistent_device_connection_never_starts_paid_session(self):
        b = VoiceBridge(Settings(openai_api_key="fake"))
        with (
            patch("app.main.HomeTools.initialize", new=AsyncMock()),
            patch("app.main.LiveSession") as live,
        ):
            async with TestServer(b.application()) as server:
                async with aiohttp.ClientSession() as http:
                    async with http.ws_connect(server.make_url("/")) as ws:
                        hello = await ws.receive_json()
                        self.assertTrue(hello["live"])
                        await ws.send_json({"type": "start", "protocol": 2})
                        await asyncio.sleep(0.03)
                        live.assert_not_called()
                        self.assertFalse(b.sessions)


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_controls_and_audio_are_tagged_with_their_wake_generation(self):
        ws = Socket()
        d = Device(ws, Settings())
        d.wake_id = 19
        await d.mic(True)
        await d.audio(b"\1\0")
        await d.end()
        self.assertTrue(all(e["wake_id"] == 19 for e in ws.events))
        self.assertEqual(ws.audio[0], (19).to_bytes(4, "little") + b"\1\0")

    async def test_legacy_local_audio_has_no_new_protocol_prefix(self):
        ws = Socket()
        d = Device(ws, Settings())
        await d.audio(b"\1\0")
        self.assertEqual(ws.audio[0], b"\1\0")


class MCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_structured_results_and_failures_are_preserved_without_retry(self):
        from app.mcp_tools import HomeTools

        h = HomeTools(Settings(), "fake")
        h.names = {"control"}
        result = SimpleNamespace(
            structuredContent={"success": True}, content=[], isError=False
        )
        h._run = AsyncMock(return_value=result)
        self.assertEqual(
            await h.call("control", {}), {"isError": False, "result": {"success": True}}
        )
        h._run = AsyncMock(side_effect=TimeoutError())
        self.assertTrue((await h.call("control", {}))["isError"])
        h._run.assert_awaited_once()

    async def test_disallowed_tool_cannot_execute(self):
        from app.mcp_tools import HomeTools

        h = HomeTools(Settings(), "fake")
        h._run = AsyncMock()
        self.assertTrue((await h.call("not-allowed", {}))["isError"])
        h._run.assert_not_awaited()


class EncoderStopTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_while_encoder_is_starting_cannot_leave_an_orphan_stream(self):
        started, release = asyncio.Event(), asyncio.Event()
        server = AudioHTTPServer("http://127.0.0.1:0", 0, "127.0.0.1")
        spawn = asyncio.create_subprocess_exec
        processes = []

        async def starting(*args, **kwargs):
            process = await spawn(*args, **kwargs)
            processes.append(process)
            started.set()
            await release.wait()
            return process

        s = Settings()
        output = SpeechOutput(
            s,
            Device(Socket(), s),
            None,
            server,
            AsyncMock(),
            AsyncMock(),
            AsyncMock(),
            "fake",
        )
        with patch(
            "app.live_audio.asyncio.create_subprocess_exec", side_effect=starting
        ):
            await started.wait()
            closing = asyncio.create_task(output.close())
            await asyncio.sleep(0)
            release.set()
            await asyncio.wait_for(closing, 3)
        self.assertIsNotNone(processes[0].returncode)
        self.assertFalse(server.resources)
