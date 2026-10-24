"""Ordered, bounded PCM input with continuous silence while the microphone is gated."""

import audioop
import math

RATE = 24000
FRAME_SAMPLES = 480
FRAME_BYTES = FRAME_SAMPLES * 2
SILENCE = bytes(FRAME_BYTES)


def dbfs(audio):
    rms = audioop.rms(audio, 2) if audio else 0
    return 20 * math.log10(rms / 32768) if rms else -120


class InputAudio:
    def __init__(self, buffer_ms):
        self.data = bytearray()
        self.limit = buffer_ms * 48
        self.resampler = None
        self.odd = b""
        self.dropped = 0

    def append(self, audio):
        audio = self.odd + audio
        self.odd = audio[len(audio) // 2 * 2 :]
        audio = audio[: len(audio) // 2 * 2]
        if not audio:
            return
        converted, self.resampler = audioop.ratecv(
            audio, 2, 1, 16000, RATE, self.resampler
        )
        self.data.extend(converted)
        if len(self.data) > self.limit:
            remove = (len(self.data) - self.limit + 1) // 2 * 2
            self.dropped += remove
            del self.data[:remove]

    def frame(self):
        data = bytes(self.data[:FRAME_BYTES])
        del self.data[:FRAME_BYTES]
        return data + bytes(FRAME_BYTES - len(data))

    def clear(self):
        self.data.clear()
        self.odd = b""
        self.resampler = None

    def trim_startup_silence(self, threshold_db=-65, keep_ms=40):
        """Remove only a quiet prefix at readiness, keeping speech and pre-roll.

        Called once, not per turn. Never speed up, burst or discard a detected
        speech frame to catch up. Already-overflowed input cannot be recovered.
        """
        quiet = 0
        for offset in range(0, len(self.data) - FRAME_BYTES + 1, FRAME_BYTES):
            if dbfs(self.data[offset : offset + FRAME_BYTES]) > threshold_db:
                break
            quiet += FRAME_BYTES
        keep = max(0, keep_ms // 20) * FRAME_BYTES
        remove = max(0, quiet - keep)
        del self.data[:remove]
        return remove / (RATE * 2) * 1000
