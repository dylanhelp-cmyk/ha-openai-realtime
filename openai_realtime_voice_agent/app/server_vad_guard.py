"""Keep transcript-derived VAD from interrupting OpenAI's server-managed turns."""

from pipecat.frames.frames import (
    EmulateUserStartedSpeakingFrame,
    EmulateUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


class ServerVADGuard(FrameProcessor):
    """Place between the input transport and the user context aggregator.

    Realtime emits its server VAD frames downstream of the user aggregator.
    A final transcript returns upstream, so that aggregator can mistake it for
    speech without VAD and request a second, emulated interruption. The input
    transport emits user-start BEFORE this interruption, which closes the
    external output gate again and discards the entire answer.

    OpenAI already owns turn detection here. Consume only the upstream requests
    to emulate VAD; preserve transcripts, genuine VAD, and real interruptions.
    The aggregator still records the transcript through its normal timeout.
    """

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if direction == FrameDirection.UPSTREAM and isinstance(
            frame, (EmulateUserStartedSpeakingFrame, EmulateUserStoppedSpeakingFrame)
        ):
            return
        await self.push_frame(frame, direction)
