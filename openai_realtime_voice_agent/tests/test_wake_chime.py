import asyncio
import unittest
import json

from app.wake_chime import ExternalWakeChime
from app.raw_audio_serializer import RawAudioSerializer


class ChimePlayback:
    def __init__(self):
        self.started = asyncio.Event()
        self.finished = asyncio.Event()
        self.stops = 0

    async def play_wake_chime(self):
        self.started.set()
        await self.finished.wait()

    async def stop(self):
        self.stops += 1


class WakeChimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playback = ChimePlayback()
        self.messages = []

        async def send(message):
            self.messages.append(message)

        self.chime = ExternalWakeChime(self.playback, send, open_delay_ms=0)

    async def asyncTearDown(self):
        await self.chime.cancel()

    async def test_ready_only_after_external_chime_finishes(self):
        serializer = RawAudioSerializer()
        serializer.set_wake_handler(self.chime.start)
        await serializer.deserialize(json.dumps({"type": "wake", "external_wake": True, "wake_id": 1}))
        await self.playback.started.wait()
        self.assertEqual(self.messages, [])
        self.playback.finished.set()
        await self.chime.task
        self.assertEqual(self.messages, [{"type": "wake_ready", "wake_id": 1, "ok": True}])

    async def test_stop_during_chime_never_reopens_microphone(self):
        await self.chime.start({"external_wake": True, "wake_id": 2})
        await self.playback.started.wait()
        await self.chime.cancel()
        self.playback.finished.set()
        await asyncio.sleep(0)
        self.assertEqual(self.messages, [])
        self.assertEqual(self.playback.stops, 1)

    async def test_new_wake_cancels_old_acknowledgement(self):
        await self.chime.start({"external_wake": True, "wake_id": 3})
        await self.playback.started.wait()
        await self.chime.start({"external_wake": True, "wake_id": 4})
        self.playback.finished.set()
        await self.chime.task
        self.assertEqual([m["wake_id"] for m in self.messages], [4])

    async def test_old_firmware_does_not_receive_a_second_chime(self):
        await self.chime.start({"type": "wake"})
        self.assertIsNone(self.chime.task)
        self.assertFalse(self.playback.started.is_set())

    async def test_disabled_wake_sound_opens_without_playing_chime(self):
        await self.chime.start({"external_wake": True, "wake_id": 5, "wake_sound": False})
        await self.chime.task
        self.assertFalse(self.playback.started.is_set())
        self.assertTrue(self.messages[0]["ok"])

    async def test_failed_chime_does_not_open_microphone(self):
        async def fail():
            raise RuntimeError("player unavailable")

        self.playback.play_wake_chime = fail
        await self.chime.start({"external_wake": True, "wake_id": 6})
        await self.chime.task
        self.assertFalse(self.messages[0]["ok"])


if __name__ == "__main__":
    unittest.main()
