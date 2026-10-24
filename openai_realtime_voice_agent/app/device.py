"""Versioned, compact JSON + raw PCM protocol for Voice PE."""

import json


class Device:
    def __init__(self, ws, settings):
        self.ws, self.settings = ws, settings
        self.phase = "idle"
        self.mic_enabled = False
        self.wake_id = 0

    async def send(self, data):
        if self.wake_id:
            data = {**data, "wake_id": self.wake_id}
        if not self.ws.closed:
            await self.ws.send_str(json.dumps(data, separators=(",", ":")))

    async def hello(self):
        await self.send(
            {
                "type": "hello",
                "live": True,
                "protocol": 2,
                "external_audio": self.settings.tts_output == "media_player",
                "playback_prebuffer_ms": self.settings.playback_prebuffer_ms,
            }
        )

    async def set_phase(self, value):
        if value != self.phase:
            self.phase = value
            await self.send({"type": "phase", "value": value})

    async def mic(self, enabled):
        if enabled != self.mic_enabled:
            self.mic_enabled = enabled
            await self.send({"type": "mic", "enabled": enabled})

    async def end(self):
        await self.mic(False)
        await self.send({"type": "session_end"})
        await self.set_phase("idle")

    async def audio(self, pcm):
        if not self.ws.closed:
            prefix = self.wake_id.to_bytes(4, "little") if self.wake_id else b""
            await self.ws.send_bytes(prefix + pcm)
