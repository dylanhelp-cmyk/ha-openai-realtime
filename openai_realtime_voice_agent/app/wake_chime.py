"""Non-blocking external wake chime and microphone-ready handshake."""
import asyncio
from contextlib import suppress
import logging

logger = logging.getLogger(__name__)


class ExternalWakeChime:
    def __init__(self, playback, send_json, open_delay_ms=700):
        self.playback = playback
        self.send_json = send_json
        self.open_delay_s = max(0, open_delay_ms) / 1000
        self.task = None

    async def start(self, data):
        await self.cancel()
        # Old firmware already played its local chime and opened the mic.
        # Only updated firmware explicitly asks us to play a remote chime.
        if not data.get("external_wake"):
            return
        wake_id = data.get("wake_id")
        if type(wake_id) is not int or not 0 < wake_id <= 0xFFFFFFFF:
            logger.warning("Ignoring external wake without a valid request ID")
            return
        self.task = asyncio.create_task(self._play(wake_id, data.get("wake_sound", True)))

    async def _play(self, wake_id, enabled):
        try:
            async with asyncio.timeout(15):
                if enabled:
                    await self.playback.play_wake_chime()
                    await asyncio.sleep(self.open_delay_s)
                await self.send_json({"type": "wake_ready", "wake_id": wake_id, "ok": True})
        except asyncio.CancelledError:
            raise  # a stopped/superseded wake must never reopen the microphone
        except Exception as exc:
            logger.error("External wake chime failed: %s", exc)
            await self.playback.stop()
            await self.send_json({"type": "wake_ready", "wake_id": wake_id, "ok": False})

    async def cancel(self):
        task, self.task = self.task, None
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            await self.playback.stop()
