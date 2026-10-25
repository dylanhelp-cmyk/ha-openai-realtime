"""Ordered, bounded PCM input with continuous silence while the microphone is gated."""

import audioop
import math
import time
from collections import deque

RATE = 24000
FRAME_SAMPLES = 480
FRAME_BYTES = FRAME_SAMPLES * 2
SILENCE = bytes(FRAME_BYTES)


def dbfs(audio):
    rms = audioop.rms(audio, 2) if audio else 0
    return 20 * math.log10(rms / 32768) if rms else -120


class InputAudio:
    def __init__(
        self, buffer_ms, track_timing=False, recover_backlog=False, level_guard_db=-42
    ):
        self.data = bytearray()
        self.limit = buffer_ms * 48
        self.resampler = None
        self.odd = b""
        self.dropped = 0
        self.track_timing = track_timing
        self.arrivals = deque()
        self.last_frame_residence_ms = None
        self.quiet_removed_ms = 0
        self.recovery = None
        if recover_backlog:
            # Lazy import keeps the plain PCM buffer independent of VAD.
            from app.input_recovery import InputRecovery

            self.recovery = InputRecovery(level_guard_db)

    def _consume_timing(self, count):
        if self.recovery:
            self.recovery.consume(count)
        while count and self.arrivals:
            size, arrived = self.arrivals.popleft()
            used = min(count, size)
            count -= used
            if used < size:
                self.arrivals.appendleft((size - used, arrived))

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
        if self.recovery:
            self.recovery.append(converted)
        if self.track_timing and converted:
            self.arrivals.append((len(converted), time.monotonic()))
        if len(self.data) > self.limit:
            remove = (len(self.data) - self.limit + 1) // 2 * 2
            self.dropped += remove
            del self.data[:remove]
            self._consume_timing(remove)

    def frame(self):
        data = bytes(self.data[:FRAME_BYTES])
        self.last_frame_residence_ms = (
            max(0, time.monotonic() - self.arrivals[0][1]) * 1000
            if data and self.arrivals
            else None
        )
        del self.data[:FRAME_BYTES]
        self._consume_timing(len(data))
        return data + bytes(FRAME_BYTES - len(data))

    def clear(self):
        self.data.clear()
        self.odd = b""
        self.resampler = None
        self.arrivals.clear()
        self.last_frame_residence_ms = None
        if self.recovery:
            self.recovery.reset()

    def recover_backlog(self):
        if not self.recovery:
            return 0
        remove = self.recovery.removable_prefix(len(self.data))
        if remove:
            del self.data[:remove]
            self._consume_timing(remove)
        removed_ms = remove / 48
        self.quiet_removed_ms += removed_ms
        return removed_ms

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
        self._consume_timing(remove)
        return remove / (RATE * 2) * 1000
