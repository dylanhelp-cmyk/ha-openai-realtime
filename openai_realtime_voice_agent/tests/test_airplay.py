"""Direct sender contract, continuous PCM, cleanup and isolated AEC profiles."""

import asyncio
import audioop
import os
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.airplay import AirPlayOutput
from app.calibration import CalibrationSession
from app.device import Device
from app.echo import CalibrationStore, EchoProcessor, pcm
from app.output import SpeechOutput
from app.playback_diagnostic import PlaybackDiagnosticSession
from app.settings import Settings
from test_live import Socket, TONE, until


class SenderRig:
    """Models the executable's two status streams and single PCM stdin."""

    def __init__(self, *, connect=True, acknowledge=True):
        self.connect, self.acknowledge = connect, acknowledge
        self.calls, self.chunks, self.commands = [], [], []
        self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
        self.returncode = None
        self.exited = asyncio.Event()
        self.process = SimpleNamespace(
            stdout=self.stdout,
            stderr=self.stderr,
            stdin=self,
            returncode=None,
            wait=self.wait,
            kill=self.kill,
        )
        self.os_write = os.write

    async def spawn(self, *args, **kwargs):
        self.calls.append(args)
        if self.connect:
            self.stderr.feed_data(b"[STATUS] connected\n")
            self.stdout.feed_data(b"[STATUS] latency warm_lead_ms=600 lead_ms=2000\n")
        return self.process

    def write(self, frame):
        self.chunks.append(frame)
        if len(self.chunks) == 1:
            self.stderr.feed_data(b"[STATUS] audio buffered_ms=20\n")

    async def drain(self):
        await asyncio.sleep(0)

    def command(self, fd, value):
        self.commands.append(value)
        if b"ACTION=START" in value and self.acknowledge:
            self.stdout.feed_data(
                f"[STATUS] started requested_unix_ms=0 at_unix_ms={int(time.time() * 1000 + 200)}\n".encode()
            )
        if b"ACTION=STOP" in value:
            self.finish(0)
        return self.os_write(fd, value)

    def finish(self, code):
        self.process.returncode = self.returncode = code
        self.exited.set()

    def kill(self):
        self.finish(-9)

    async def wait(self):
        await self.exited.wait()
        return self.returncode

    def patches(self):
        return (
            patch("app.airplay.asyncio.create_subprocess_exec", self.spawn),
            patch("app.airplay.os.write", self.command),
        )


class AirPlayTests(unittest.IsolatedAsyncioTestCase):
    def settings(self, **kwargs):
        return Settings(
            openai_api_key="fake",
            tts_output="airplay",
            airplay_host="192.0.2.50",
            **kwargs,
        )

    async def test_one_sender_through_speech_and_silence_no_http_or_ha(self):
        settings, rig = self.settings(), SenderRig()
        device = Device(Socket(), settings)
        callback, failure = AsyncMock(), AsyncMock()
        references = []
        first, second = rig.patches()
        with first, second:
            out = SpeechOutput(
                settings, device, None, None, callback, callback, failure, "unused"
            )
            out._ha = AsyncMock(side_effect=AssertionError("No HA playback"))
            out.on_reference = lambda frame, stamp: references.append((frame, stamp))
            try:
                await asyncio.wait_for(out.ready.wait(), 1)
                await out.write(TONE * 3)
                await until(lambda: out.first_speech_sent and not out.frames)
                count = len(rig.chunks)
                await asyncio.sleep(0.12)
                self.assertGreater(len(rig.chunks), count)
                await out.write(TONE * 2)
                await until(lambda: not out.frames)
                self.assertEqual(len(rig.calls), 1)
                self.assertEqual(sum(b"ACTION=START" in c for c in rig.commands), 1)
                self.assertTrue(any(any(frame) for frame in rig.chunks))
                self.assertTrue(any(not any(frame) for frame in rig.chunks[count:]))
                self.assertTrue(any(any(frame) for frame, _ in references))
                self.assertFalse(device.ws.audio)
                out._ha.assert_not_awaited()
                failure.assert_not_awaited()
                self.assertIsNone(out.encoder)
                fifo = Path(out.airplay.folder.name) / "control"
                self.assertEqual(fifo.stat().st_mode & 0o777, 0o600)
                self.assertTrue(out.airplay.ready.is_set())
            finally:
                await out.close()
            self.assertFalse(fifo.exists())
            self.assertEqual(rig.returncode, 0)
            self.assertFalse(out.airplay.tasks)
            self.assertIsNone(out.airplay.command_fd)

    async def test_resample_stereo_duration_and_native_arguments(self):
        rig, sender = SenderRig(), AirPlayOutput(self.settings())
        first, second = rig.patches()
        with first, second:
            try:
                await sender.start()
                prime_frames = len(rig.chunks)
                self.assertGreaterEqual(prime_frames, 5)
                # Bounded prefill plus paced startup silence, then one second.
                for _ in range(50):
                    await sender.write(TONE)
                audio = b"".join(rig.chunks)
                self.assertAlmostEqual(
                    len(audio) / 4 / 44100, 1 + prime_frames * 0.02, delta=2 / 44100
                )
                self.assertEqual(
                    audioop.tomono(audio, 2, 1, 0), audioop.tomono(audio, 2, 0, 1)
                )
                args = rig.calls[0]
                self.assertIn("--timing", args)
                self.assertEqual(args[args.index("--timing") + 1], "ntp")
                self.assertEqual(args[args.index("--latency") + 1], "600")
                self.assertNotIn("--encrypt", args)  # native encryption is built-in
            finally:
                await sender.close()

    async def test_compatibility_route_is_explicit_and_encryption_is_tunable(self):
        for enabled in (True, False):
            rig = SenderRig()
            sender = AirPlayOutput(
                self.settings(
                    airplay_protocol="raop",
                    airplay_port=5000,
                    airplay_encryption=enabled,
                )
            )
            first, second = rig.patches()
            with first, second:
                try:
                    await sender.start()
                    self.assertEqual("--encrypt" in rig.calls[0], enabled)
                    self.assertNotIn("--timing", rig.calls[0])
                finally:
                    await sender.close()

    async def test_handshake_error_is_coded_and_native_details_are_not_logged(self):
        rig, sender = SenderRig(connect=False), AirPlayOutput(self.settings())
        first, second = rig.patches()
        with first, second:
            task = asyncio.create_task(sender.start())
            await until(lambda: sender.process is not None)
            with self.assertLogs("app.airplay", "INFO") as logs:
                rig.stdout.feed_data(
                    b"CREDENTIALS: PRIVATE_SENTINEL\n[STATUS] route selected=airplay2\n"
                )
                rig.stderr.feed_data(
                    b'[STATUS] error code=auth_failed http=403 detail="PRIVATE_SENTINEL"\n'
                )
                with self.assertRaisesRegex(RuntimeError, "auth_failed"):
                    await task
            self.assertNotIn("PRIVATE_SENTINEL", "\n".join(logs.output))
        self.assertTrue(sender.closed)
        self.assertEqual(rig.returncode, 0)
        self.assertEqual(len(rig.calls), 1)  # no silent fallback/retry

    async def test_missing_start_ack_times_out_and_removes_fifo(self):
        rig = SenderRig(acknowledge=False)
        sender = AirPlayOutput(self.settings(tts_stream_start_timeout_s=0.05))
        first, second = rig.patches()
        with first, second:
            with self.assertRaises(TimeoutError):
                await sender.start()
        self.assertTrue(sender.closed)
        self.assertIsNone(sender.folder)
        self.assertIsNone(sender.command_fd)
        self.assertEqual(rig.returncode, 0)

    async def test_pcm_keeps_flowing_during_delayed_start_ack(self):
        rig = SenderRig(acknowledge=False)
        sender = AirPlayOutput(self.settings())
        first, second = rig.patches()
        with first, second:
            task = asyncio.create_task(sender.start())
            try:
                await until(lambda: any(b"ACTION=START" in c for c in rig.commands))
                before = len(rig.chunks)
                await asyncio.sleep(0.1)
                self.assertGreater(len(rig.chunks), before)
                self.assertFalse(any(any(c) for c in rig.chunks))
                self.assertFalse(sender.ready.is_set())
                rig.stdout.feed_data(b"[STATUS] started at_unix_ms=0\n")
                await task
                self.assertTrue(sender.ready.is_set())
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await sender.close()
            count = len(rig.chunks)
            await asyncio.sleep(0.04)
            self.assertEqual(len(rig.chunks), count)

    async def test_stalled_pcm_pipe_fails_instead_of_accumulating_audio(self):
        rig, sender = SenderRig(), AirPlayOutput(self.settings())
        first, second = rig.patches()
        with first, second:
            try:
                await sender.start()
                sender.process.stdin = SimpleNamespace(
                    write=lambda _: None,
                    drain=AsyncMock(side_effect=TimeoutError),
                )
                with self.assertRaises(TimeoutError):
                    await sender.write(TONE)
                with self.assertRaisesRegex(RuntimeError, "backpressure"):
                    sender.check()
            finally:
                await sender.close()

    async def test_sender_exit_after_ready_fails_conversation_without_retry(self):
        rig, settings = SenderRig(), self.settings()
        failure = AsyncMock()
        first, second = rig.patches()
        with first, second:
            out = SpeechOutput(
                settings,
                Device(Socket(), settings),
                None,
                None,
                AsyncMock(),
                AsyncMock(),
                failure,
                "",
            )
            try:
                await asyncio.wait_for(out.ready.wait(), 1)
                rig.finish(1)
                await until(lambda: failure.await_count > 0)
                self.assertIn("Direct AirPlay", failure.call_args.args[0])
                self.assertEqual(len(rig.calls), 1)
            finally:
                await out.close()

    async def test_stop_during_spawn_reaps_eventual_sender(self):
        rig, sender = SenderRig(), AirPlayOutput(self.settings())
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow(*args, **kwargs):
            entered.set()
            await release.wait()
            return await rig.spawn(*args, **kwargs)

        first, second = rig.patches()
        with first, second, patch("app.airplay.asyncio.create_subprocess_exec", slow):
            task = asyncio.create_task(sender.start())
            await entered.wait()
            await sender.close()
            task.cancel()
            release.set()
            await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(sender.closed)
        self.assertIsNotNone(rig.returncode)
        self.assertFalse(sender.tasks)
        self.assertIsNone(sender.folder)

    async def test_firmware_external_flag_and_calibration_use_direct_airplay(self):
        settings, rig = self.settings(aec_calibration_max_delay_ms=200), SenderRig()
        device = Device(Socket(), settings)
        device.identity = "mic"
        await device.hello()
        self.assertTrue(device.ws.events[0]["external_audio"])
        with tempfile.TemporaryDirectory() as folder:
            store = CalibrationStore(Path(folder) / "calibration.json")
            first, second = rig.patches()
            timers = []
            with first, second:
                session = CalibrationSession(
                    settings, device, None, None, "unused", store
                )
                original = session.reference

                def reference(frame, stamp):
                    original(frame, stamp)
                    returned = session.reference_frames[-1][1] * 0.6
                    timers.append(
                        asyncio.get_running_loop().call_later(
                            0.14, session.receive_audio, pcm(returned)
                        )
                    )

                session.output.on_reference = reference
                try:
                    await asyncio.wait_for(session.run(), 6)
                    self.assertIsNotNone(session.result)
                    self.assertAlmostEqual(store.load(settings, device), 120, delta=8)
                    self.assertEqual(len(rig.calls), 1)
                finally:
                    await session.close()
                    for timer in timers:
                        timer.cancel()

    async def test_airplay_comparison_does_not_attempt_mp3_loopback(self):
        settings = self.settings()
        device = Device(Socket(), settings)
        trials = []

        class Trial:
            def __init__(self, config, *args, **kwargs):
                trials.append((config, kwargs))
                self.result = {"delay_ms": 600}
                self.loopback_result = None
                self.run = self.close = AsyncMock()

        with patch("app.playback_diagnostic.CalibrationSession", Trial):
            session = PlaybackDiagnosticSession(settings, device, None, None, "", None)
            await session.run()
        self.assertEqual([s.tts_output for s, _ in trials], ["airplay", "voice_pe"])
        self.assertTrue(all(not k["software_loopback"] for _, k in trials))
        self.assertTrue(all(not k["save_result"] for _, k in trials))


class AirPlaySettingsTests(unittest.TestCase):
    def test_direct_mode_does_not_require_http_or_media_player(self):
        Settings(
            openai_api_key="fake",
            tts_output="airplay",
            airplay_host="192.0.2.50",
            aec_calibrate_on_next_wake=True,
        ).validate()

    def test_invalid_target_and_tuning_are_rejected(self):
        base = Settings(
            openai_api_key="fake", tts_output="airplay", airplay_host="192.0.2.50"
        )
        for change in (
            {"airplay_host": ""},
            {"airplay_host": "https://192.0.2.50/"},
            {"airplay_host": "-option"},
            {"airplay_host": "224.0.0.1"},
            {"airplay_protocol": "auto"},
            {"airplay_port": 0},
            {"airplay_latency_ms": 0},
            {"airplay_latency_ms": 3001},
            {"airplay_volume": 101},
            {"airplay_interface": "not-an-ip"},
            {"airplay_encryption": "false"},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                replace(base, **change).validate()

    def test_http_calibration_is_preserved_and_airplay_profiles_are_separate(self):
        device = SimpleNamespace(identity="mic")
        http = Settings(tts_output="media_player", tts_media_player="media_player.test")
        airplay = replace(http, tts_output="airplay", airplay_host="192.0.2.50")
        with tempfile.TemporaryDirectory() as folder:
            store = CalibrationStore(Path(folder) / "calibration.json")
            old_target = {
                "player": http.tts_media_player,
                "microphone": "mic",
                "announce": http.tts_announce,
                "prebuffer_ms": http.tts_stream_prebuffer_ms,
                "reference_version": 1,
            }
            self.assertEqual(store.target(http, device), old_target)
            store.save(http, device, {"delay_ms": 3020})
            self.assertIsNone(store.load(airplay, device))
            store.save(airplay, device, {"delay_ms": 620})
            self.assertEqual(store.load(http, device), 3020)
            self.assertEqual(store.load(airplay, device), 620)
            for change in (
                {"airplay_latency_ms": 800},
                {"airplay_protocol": "raop"},
                {"airplay_port": 5000},
            ):
                self.assertIsNone(store.load(replace(airplay, **change), device))
            processor = EchoProcessor(airplay, device, store)
            try:
                self.assertEqual(processor.delay_ms, 620)
                self.assertIsNotNone(processor.native)
            finally:
                processor.close()


@unittest.skipUnless(
    os.environ.get("AIRPLAY_TEST_BINARY"),
    "Native sender provided by CI or AIRPLAY_TEST_BINARY",
)
class NativeAirPlayTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_raop_sender_streams_to_a_loopback_rtsp_receiver(self):
        """Exercise the shipped binary + FIFO + PCM against a local protocol peer.

        Not a physical speaker/latency test: no sound or external device access.
        """
        packets, methods, peers = [], [], set()
        loop = asyncio.get_running_loop()

        class UDP(asyncio.DatagramProtocol):
            def datagram_received(self, data, address):
                packets.append(data)

        transports = []
        for _ in range(3):
            transport, _ = await loop.create_datagram_endpoint(
                UDP, local_addr=("127.0.0.1", 0)
            )
            transports.append(transport)
        ports = [t.get_extra_info("sockname")[1] for t in transports]

        async def receive(reader, writer):
            peers.add(writer)
            try:
                while True:
                    raw = await reader.readuntil(b"\r\n\r\n")
                    lines = raw.decode().split("\r\n")
                    method = lines[0].split(" ")[0]
                    methods.append(method)
                    headers = dict(
                        line.split(": ", 1) for line in lines[1:] if ": " in line
                    )
                    size = int(headers.get("Content-Length", "0"))
                    if size:
                        await reader.readexactly(size)
                    extra = ""
                    if method == "OPTIONS":
                        extra += "Public: ANNOUNCE, SETUP, RECORD, SET_PARAMETER, GET_PARAMETER, FLUSH, TEARDOWN, OPTIONS\r\n"
                    if method == "SETUP":
                        extra += f"Transport: RTP/AVP/UDP;unicast;mode=record;server_port={ports[0]};control_port={ports[1]};timing_port={ports[2]}\r\nSession: 1\r\n"
                    if method == "RECORD":
                        extra += "Audio-Latency: 11025\r\n"
                    writer.write(
                        f"RTSP/1.0 200 OK\r\nCSeq: {headers.get('CSeq', '1')}\r\n{extra}Content-Length: 0\r\n\r\n".encode()
                    )
                    await writer.drain()
            except (asyncio.IncompleteReadError, ConnectionError):
                pass
            finally:
                peers.discard(writer)
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(receive, "127.0.0.1", 0)
        settings = Settings(
            tts_output="airplay",
            airplay_host="127.0.0.1",
            airplay_port=server.sockets[0].getsockname()[1],
            airplay_protocol="raop",
            airplay_encryption=False,
            tts_stream_start_timeout_s=5,
        )
        sender = AirPlayOutput(settings)
        native_log = []
        original_reader = sender._read_status

        async def capture(reader):
            proxy = asyncio.StreamReader()
            task = asyncio.create_task(original_reader(proxy))
            try:
                while line := await reader.readline():
                    native_log.append(line.decode("utf-8", "replace"))
                    proxy.feed_data(line)
                proxy.feed_eof()
                await task
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        sender._read_status = capture
        try:
            with patch("app.airplay.AIRPLAY_BINARY", os.environ["AIRPLAY_TEST_BINARY"]):
                try:
                    await sender.start()
                except Exception as exc:
                    raise AssertionError(
                        "Synthetic receiver methods="
                        + repr(methods)
                        + "\n"
                        + "".join(native_log)
                    ) from exc
                for _ in range(30):
                    await sender.write(TONE)
                    await asyncio.sleep(0.02)
                await until(lambda: bool(packets))
                self.assertIn("ANNOUNCE", methods)
                self.assertIn("SETUP", methods)
                self.assertIn("RECORD", methods)
                self.assertTrue(sender.ready.is_set())
                # RTP audio, not merely RTSP control, reaches the UDP peer.
                self.assertTrue(
                    any(len(p) > 12 and p[1] & 0x7F == 0x60 for p in packets)
                )
        finally:
            await sender.close()
            server.close()
            await server.wait_closed()
            for writer in tuple(peers):
                writer.close()
            await asyncio.gather(
                *(w.wait_closed() for w in tuple(peers)), return_exceptions=True
            )
            for transport in transports:
                transport.close()
        self.assertIsNotNone(sender.process.returncode)
        self.assertIsNone(sender.folder)
