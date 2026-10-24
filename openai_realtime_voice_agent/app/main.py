"""GPT-Live Home Assistant voice bridge. No paid connection while the device rests."""

import asyncio
import json
import logging
import os
import time

import aiohttp
from aiohttp import web

from app.calibration import CalibrationSession
from app.device import Device
from app.echo import CalibrationStore
from app.live_audio import AudioHTTPServer
from app.live_session import LiveSession
from app.mcp_tools import HomeTools
from app.playback_diagnostic import PlaybackDiagnosticSession
from app.settings import Settings

log = logging.getLogger(__name__)


class VoiceBridge:
    def __init__(self, settings):
        self.s = settings
        self.http = None
        self.server = None
        self.tools = None
        self.devices = set()
        self.sessions = set()
        self.owner = None
        self.calibration_store = CalibrationStore()
        self.calibration_pending = settings.aec_calibrate_on_next_wake
        self.diagnostic_pending = settings.playback_diagnostic_on_next_wake
        self.token = os.environ.get("SUPERVISOR_TOKEN") or settings.longlived_token

    async def start(self, app):
        self.http = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15, connect=5)
        )
        self.tools = HomeTools(
            self.s, self.s.longlived_token if self.s.ha_mcp_url else self.token
        )
        try:
            await self.tools.initialize()
        except Exception as exc:
            log.warning(
                "HA discovery unavailable (%s); voice still available, no invented device controls",
                type(exc).__name__,
            )
        if self.s.tts_output == "media_player":
            self.server = AudioHTTPServer(
                self.s.tts_stream_base_url,
                self.s.tts_stream_port,
                buffer_seconds=self.s.tts_stream_buffer_seconds,
            )
            await self.server.start()
            log.info("Speech HTTP listening on port %d", self.server.port)
        log.info(
            "GPT-Live ready on device port %d; OpenAI stays disconnected until wake",
            self.s.websocket_port,
        )

    async def websocket(self, request):
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=128 * 1024)
        await ws.prepare(request)
        device = Device(ws, self.s)
        device.identity = request.remote or "unknown"
        self.devices.add(device)
        session = None
        runner = None

        async def stop():
            nonlocal session, runner
            if session:
                await session.close()
                self.sessions.discard(session)
                session = None
            if runner:
                runner.cancel()
                await asyncio.gather(runner, return_exceptions=True)
                runner = None
            if self.owner is device:
                self.owner = None

        try:
            await device.hello()
            async for message in ws:
                if message.type == aiohttp.WSMsgType.BINARY:
                    if session:
                        session.receive_audio(message.data)
                    continue
                if message.type != aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    event = json.loads(message.data)
                except ValueError:
                    continue
                kind = event.get("type")
                if kind == "wake":
                    wake_started_at = time.monotonic()
                    await stop()
                    wake_id = event.get("wake_id", 0)
                    device.wake_id = (
                        wake_id
                        if type(wake_id) is int and 0 < wake_id <= 0xFFFFFFFF
                        else 0
                    )
                    # One device owns a shared speaker at a time. Another device
                    # cannot interrupt an active conversation on this target.
                    if self.owner and self.owner is not device:
                        await device.send(
                            {
                                "type": "error",
                                "message": "Voice speaker is already in use",
                            }
                        )
                        continue
                    self.owner = device
                    await device.mic(True)
                    await device.set_phase("listening")
                    # Firmware 1.3 external wake compatibility: immediately grant
                    # readiness, with no chime and no artificial delay.
                    if event.get("external_wake") and type(event.get("wake_id")) is int:
                        await device.send(
                            {
                                "type": "wake_ready",
                                "wake_id": event["wake_id"],
                                "ok": True,
                            }
                        )
                    if self.diagnostic_pending:
                        self.diagnostic_pending = False
                        session = PlaybackDiagnosticSession(
                            self.s,
                            device,
                            self.http,
                            self.server,
                            self.token,
                            self.calibration_store,
                        )
                    elif self.calibration_pending:
                        self.calibration_pending = False
                        session = CalibrationSession(
                            self.s,
                            device,
                            self.http,
                            self.server,
                            self.token,
                            self.calibration_store,
                        )
                    else:
                        session = LiveSession(
                            self.s,
                            device,
                            self.http,
                            self.tools,
                            self.server,
                            self.token,
                            calibration_store=self.calibration_store,
                            wake_started_at=wake_started_at,
                        )
                    self.sessions.add(session)
                    runner = asyncio.create_task(session.run())

                    def finished(task, owned=session):
                        self.sessions.discard(owned)
                        if self.owner is device and not any(
                            not s.closed and s.device is device for s in self.sessions
                        ):
                            self.owner = None

                    runner.add_done_callback(finished)
                elif kind in {"interrupt", "flush", "stop", "end"}:
                    await stop()
                elif kind == "start":
                    pass  # Initial hello was already sent; no duplicate state reset.
        finally:
            await stop()
            self.devices.discard(device)
        return ws

    async def close(self, app):
        await asyncio.gather(
            *(s.close() for s in list(self.sessions)), return_exceptions=True
        )
        for device in list(self.devices):
            await device.ws.close()
        if self.server:
            await self.server.close()
        if self.http:
            await self.http.close()

    def application(self):
        app = web.Application()
        app.router.add_get("/", self.websocket)

        async def health(request):
            return web.json_response({"status": "ok", "voice_model": "gpt-live-1"})

        app.router.add_get("/health", health)
        app.on_startup.append(self.start)
        app.on_cleanup.append(self.close)
        return app


def main():
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    settings = Settings.load()
    web.run_app(
        VoiceBridge(settings).application(),
        host="0.0.0.0",
        port=settings.websocket_port,
        access_log=None,
    )


if __name__ == "__main__":
    main()
