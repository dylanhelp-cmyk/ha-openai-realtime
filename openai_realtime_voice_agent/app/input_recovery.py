"""Classify queued PCM once, in order; shorten only a guarded quiet prefix.

This is not a turn detector or microphone gate. Classification never changes
samples, and failures protect all queued audio. Metadata is consumed alongside
the original queue, including partial-frame overflow and startup trimming.
"""

import audioop
import logging
from collections import deque

import webrtcvad

from app.pcm import FRAME_BYTES, dbfs

log = logging.getLogger(__name__)


class InputRecovery:
    def __init__(self, level_guard_db=-42):
        self.level_guard_db = min(-42, level_guard_db)
        self.reset()

    def reset(self):
        self.partial = bytearray()
        self.frames = deque()
        self.framed = self.consumed = 0
        self.hangover = 0
        self.failed = False
        try:
            self.vad = webrtcvad.Vad(0)  # Least aggressive: favour retaining speech.
        except Exception:
            self.vad = None
            self.failed = True
            log.warning(
                "Microphone backlog recovery disabled: classifier unavailable; retaining audio"
            )

    def append(self, audio):
        self.partial.extend(audio)
        while len(self.partial) >= FRAME_BYTES:
            frame = bytes(self.partial[:FRAME_BYTES])
            del self.partial[:FRAME_BYTES]
            self.framed += FRAME_BYTES
            protected = True
            if not self.failed:
                try:
                    # The queue is 24 kHz; WebRTC VAD accepts 16 kHz / 20 ms.
                    vad_frame = audioop.ratecv(frame, 2, 1, 24000, 16000, None)[0]
                    voice = self.vad.is_speech(vad_frame, 16000)
                    voice = voice or dbfs(frame) >= self.level_guard_db
                    if voice:
                        self.hangover = 5  # Extra 100 ms, beyond VAD hangover.
                    protected = voice or self.hangover > 0
                    if not voice:
                        self.hangover = max(0, self.hangover - 1)
                except Exception:
                    self.failed = True
                    log.warning(
                        "Microphone backlog recovery disabled: classifier failed; retaining audio"
                    )
            self.frames.append((self.framed, protected))

    def consume(self, count):
        self.consumed += count
        while self.frames and self.frames[0][0] <= self.consumed:
            self.frames.popleft()

    def removable_prefix(self, queued_bytes):
        # Do not chase ordinary packet jitter. Preserve at least 80 ms total
        # and 100 ms of quiet immediately before any upcoming speech.
        if self.failed or queued_bytes <= 8 * FRAME_BYTES:
            return 0
        quiet_end = self.consumed
        for end, protected in self.frames:
            if protected:
                break
            quiet_end = end
        quiet = quiet_end - self.consumed
        if quiet < 15 * FRAME_BYTES:  # Require 300 ms confirmed non-speech.
            return 0
        end = min(
            quiet_end - 5 * FRAME_BYTES, self.consumed + queued_bytes - 4 * FRAME_BYTES
        )
        # End on an original classification boundary even after partial-frame
        # overflow. Never remove a piece of a protected frame.
        end = end // FRAME_BYTES * FRAME_BYTES
        return max(0, end - self.consumed)
