import unittest
from app.pcm import InputAudio, SILENCE


class StartupInputTests(unittest.TestCase):
    def test_quiet_prefix_trim_keeps_speech_and_preroll(self):
        input = InputAudio(600)
        speech = (1000).to_bytes(2, "little") * 480
        input.data.extend(SILENCE * 20 + speech * 5)
        self.assertEqual(input.trim_startup_silence(), 360)
        self.assertEqual(input.data, SILENCE * 2 + speech * 5)

    def test_all_silence_keeps_two_frames(self):
        input = InputAudio(600)
        input.data.extend(SILENCE * 30)
        self.assertEqual(input.trim_startup_silence(), 560)
        self.assertEqual(input.data, SILENCE * 2)

    def test_speech_at_front_is_never_cut(self):
        input = InputAudio(600)
        speech = (1000).to_bytes(2, "little") * 480
        input.data.extend(speech + SILENCE * 20)
        self.assertEqual(input.trim_startup_silence(), 0)
        self.assertEqual(input.data, speech + SILENCE * 20)
