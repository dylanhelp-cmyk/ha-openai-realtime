"""Exercise external speech through real Pipecat task and context aggregators."""
import asyncio
import unittest

from pipecat.frames.frames import (
    TextFrame, TTSStartedFrame, TTSStoppedFrame, TTSAudioRawFrame,
    UserStartedSpeakingFrame, UserStoppedSpeakingFrame, TranscriptionFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.base_transport import TransportParams

from app.media_player_output import MediaPlayerOutput
from app.phase_emitter import PhaseEmitter
from app.server_vad_guard import ServerVADGuard


class Playback:
    def __init__(self):
        self.started = asyncio.Event()
        self.calls = []

    async def play(self, audio, rate, channels):
        self.calls.append((audio, rate, channels))
        self.started.set()

    async def stop(self):
        pass

    async def close(self):
        pass


class SpeechSource(FrameProcessor):
    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, TextFrame) and frame.text == "test-question":
            self.create_task(self.reply())
        else:
            await self.push_frame(frame, direction)

    async def reply(self):
        # This is the sequence used by OpenAIRealtimeLLMService on every turn.
        await self.push_interruption_task_frame_and_wait()
        await self.push_frame(UserStartedSpeakingFrame())
        await self.push_frame(UserStoppedSpeakingFrame())
        await self.push_frame(TTSStartedFrame())
        await self.push_frame(TTSAudioRawFrame(b"\1\0" * 2400, 24000, 1))
        await self.push_frame(TTSStoppedFrame())


class LateTranscriptSpeechSource(SpeechSource):
    async def reply(self):
        await self.push_interruption_task_frame_and_wait()
        await self.push_frame(UserStartedSpeakingFrame())
        await self.push_frame(UserStoppedSpeakingFrame())
        # OpenAI's final transcript comes UPSTREAM after its server VAD stop.
        # The generic user aggregator has not seen those downstream VAD frames.
        await self.push_frame(TranscriptionFrame("test", "", ""), FrameDirection.UPSTREAM)
        await asyncio.sleep(0.05)
        await self.push_frame(TTSStartedFrame())
        await self.push_frame(TTSAudioRawFrame(b"\1\0" * 2400, 24000, 1))
        await self.push_frame(TTSStoppedFrame())


class AudioPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_late_final_transcript_does_not_interrupt_the_real_server_vad_reply(self):
        playback = Playback()
        output = MediaPlayerOutput(playback)
        transport = BaseInputTransport(TransportParams())
        context = LLMContextAggregatorPair(LLMContext())
        pipeline = Pipeline([
            transport, ServerVADGuard(), context.user(), LateTranscriptSpeechSource(),
            context.assistant(), output,
        ])
        task = PipelineTask(pipeline, idle_timeout_secs=None)
        runner = PipelineRunner(handle_sigint=False)
        running = asyncio.create_task(runner.run(task))
        try:
            for turn in range(2):
                playback.started.clear()
                await task.queue_frame(TextFrame("test-question"))
                await asyncio.wait_for(playback.started.wait(), 2)
                self.assertEqual(len(playback.calls), turn + 1)
                self.assertEqual(playback.calls[-1], (b"\1\0" * 2400, 24000, 1))
                await asyncio.sleep(0.9)  # include the aggregator's emulated stop
                self.assertTrue(any(m.get("role") == "user" and m.get("content") == "test"
                                    for m in context.user().context.get_messages()))
        finally:
            await task.cancel()
            await asyncio.wait_for(running, 3)

    async def test_reply_survives_real_speech_start_interruption_and_context_aggregation(self):
        playback = Playback()
        output = MediaPlayerOutput(playback)
        phases = []

        async def phase_changed(value):
            phases.append(value)

        phase = PhaseEmitter(phase_changed, idle_debounce_s=0.01)
        # Retain 0.7.0's upstream resume callback to reproduce its queue race:
        # the output must still resume at its own user-start boundary.
        phase.set_kill_window_handlers(on_real_speech=output.resume)
        context = LLMContextAggregatorPair(LLMContext())
        pipeline = Pipeline([context.user(), SpeechSource(), context.assistant(), phase, output])
        task = PipelineTask(pipeline, idle_timeout_secs=None)
        runner = PipelineRunner(handle_sigint=False)
        running = asyncio.create_task(runner.run(task))
        try:
            await task.queue_frame(TextFrame("test-question"))
            await asyncio.wait_for(playback.started.wait(), 2)
            self.assertEqual(playback.calls[0], (b"\1\0" * 2400, 24000, 1))
        finally:
            await task.cancel()
            await asyncio.wait_for(running, 3)


if __name__ == "__main__":
    unittest.main()
