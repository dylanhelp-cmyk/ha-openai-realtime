"""Opt-in, metadata-only observations. Never gate, trim, or schedule audio.

Level-based speech bursts are not semantic turns or proof of user speech: echo,
noise, pauses and backchannels can affect pairing. All clocks are local; device
capture, network transit and acoustic playback are not measured here.
"""

import logging
from collections import deque
from dataclasses import dataclass

from app.pcm import dbfs

log = logging.getLogger(__name__)


def ms(value):
    return "n/a" if value is None else f"{value * 1000:.0f}"


@dataclass
class SpeechBurst:
    number: int
    start: float
    end: float
    paused: bool = False
    replied: bool = False
    mic_residence: float | None = None


@dataclass
class Reply:
    number: int
    received: float
    user_end: float | None
    user_number: int | None


class TurnTiming:
    def __init__(self, settings, wake_id, wake_started_at):
        self.s, self.wake_id, self.origin = settings, wake_id, wake_started_at
        self.users = deque(maxlen=16)
        self.user_number = self.reply_number = 0
        self.output_quiet_ms = 350
        self.last_output_loud_at = None
        self.last_queues_at = None
        self.backends = {}
        log.info(
            "Turn timing enabled (wake=%d, transport=%s, mic_threshold_db=%.0f; "
            "300 ms level-based pause estimate, not server VAD; timing logs contain no audio/text)",
            wake_id,
            settings.live_transport,
            settings.input_activity_threshold_db,
        )

    def input_received(self, audio, now, queued_ms, dropped):
        loud = dbfs(audio) > self.s.input_activity_threshold_db
        current = self.users[-1] if self.users else None
        if loud:
            if current is None or now - current.end >= 0.3:
                self.user_number += 1
                current = SpeechBurst(self.user_number, now, now)
                self.users.append(current)
            current.end = now
        elif current and not current.paused and now - current.end >= 0.3:
            current.paused = True
            log.info(
                "Turn timing USER_PAUSE: wake=%d burst=%d end_since_wake_ms=%s "
                "observed_speech_ms=%s mic_queue_ms=%.0f dropped_input_ms=%.0f "
                "(post-AEC level estimate at backend, not semantic turn end)",
                self.wake_id,
                current.number,
                ms(current.end - self.origin),
                ms(current.end - current.start),
                queued_ms,
                dropped / 48,
            )

    def input_dequeued(self, audio, now, residence_ms, queued_ms, dropped, output_ms):
        if (
            residence_ms is not None
            and dbfs(audio) > self.s.input_activity_threshold_db
        ):
            arrived = now - residence_ms / 1000
            for burst in reversed(self.users):
                if burst.start - 0.001 <= arrived <= burst.end + 0.001:
                    burst.mic_residence = residence_ms / 1000
                    break
        if self.last_queues_at is None or now - self.last_queues_at >= 5:
            self.last_queues_at = now
            log.info(
                "Turn timing QUEUES: wake=%d mic_oldest_residence_ms=%s "
                "mic_queued_audio_ms=%.0f output_queued_audio_ms=%.0f dropped_input_ms=%.0f "
                "(backend queues only; mic residence excludes device/LAN)",
                self.wake_id,
                "n/a" if residence_ms is None else f"{residence_ms:.0f}",
                queued_ms,
                output_ms,
                dropped / 48,
            )

    def output_received(self, loud, now, queued_ms, mic_queue_ms, queued_speech_ms=0):
        if not loud:
            self.output_quiet_ms += 20
            return None
        onset = self.output_quiet_ms >= 350 or (
            queued_speech_ms == 0
            and self.last_output_loud_at is not None
            and now - self.last_output_loud_at >= 0.35
        )
        self.output_quiet_ms = 0
        self.last_output_loud_at = now
        if not onset:
            return None
        self.reply_number += 1
        user = self.users[-1] if self.users else None
        # Do not label a second assistant burst as another response to the same
        # user burst. Backchannels/overlap are still possible and explicitly noted.
        if user and user.replied:
            user = None
        if user:
            user.replied = True
        reply = Reply(
            self.reply_number,
            now,
            user.end if user else None,
            user.number if user else None,
        )
        log.info(
            "Turn timing REPLY_RECEIVED: wake=%d reply=%d user_burst=%s "
            "pause_to_reply_ms=%s mic_last_speech_residence_ms=%s mic_queue_ms=%.0f "
            "output_ahead_ms=%.0f overlap_or_short_pause=%s "
            "(first non-silent PCM burst; level-based pairing, may be backchannel/echo)",
            self.wake_id,
            reply.number,
            reply.user_number or "n/a",
            ms(now - user.end if user else None),
            ms(user.mic_residence if user else None),
            mic_queue_ms,
            queued_ms,
            bool(user and now - user.end < 0.3),
        )
        return reply

    def output_handed(self, reply, now):
        log.info(
            "Turn timing REPLY_HANDOFF: wake=%d reply=%d user_burst=%s "
            "output_queue_wait_ms=%s pause_to_handoff_ms=%s "
            "(sender handoff, NOT audible playback; excludes AirPlay render delay)",
            self.wake_id,
            reply.number,
            reply.user_number or "n/a",
            ms(now - reply.received),
            ms(now - reply.user_end if reply.user_end is not None else None),
        )

    def backend_started(self, response_id, now):
        if len(self.backends) >= 32:
            self.backends.pop(next(iter(self.backends)))
        self.backends[response_id] = now
        log.info(
            "Turn timing BACKEND_START: wake=%d user_burst=%s",
            self.wake_id,
            self.users[-1].number if self.users else "n/a",
        )

    def backend_finished(self, response_id, now, status):
        start = self.backends.pop(response_id, None)
        log.info(
            "Turn timing BACKEND_DONE: wake=%d response_ms=%s status=%s "
            "(one backend response, not total delegated task duration)",
            self.wake_id,
            ms(now - start if start is not None else None),
            status,
        )

    def tool_finished(self, start, now):
        log.info(
            "Turn timing TOOL_DONE: wake=%d tool_ms=%s (includes tool network wait)",
            self.wake_id,
            ms(now - start),
        )
