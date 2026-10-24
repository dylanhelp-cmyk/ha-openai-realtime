"""Render lead is independent of queue depth and scoped to calibration."""

import os
import subprocess
import unittest
from dataclasses import replace
from types import SimpleNamespace

from app.airplay import AirPlayOutput
from app.echo import CalibrationStore
from app.settings import Settings
from test_airplay import SenderRig


class RenderLeadTests(unittest.TestCase):
    def settings(self, **changes):
        return replace(
            Settings(
                openai_api_key="fake", tts_output="airplay", airplay_host="192.0.2.50"
            ),
            **changes,
        )

    def test_independent_defaults_and_validation(self):
        settings = self.settings()
        self.assertEqual(settings.airplay_render_lead_ms, 2000)
        self.assertEqual(settings.airplay_latency_ms, 600)
        for value in (200, 250, 500, 1000, 2000, 3000):
            self.settings(airplay_render_lead_ms=value).validate()
        for value in (0, 199, 3001, True, 250.0, "500"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.settings(airplay_render_lead_ms=value).validate()

    def test_new_startup_profile_and_nondefault_lead_are_separate(self):
        settings, mic = self.settings(), SimpleNamespace(identity="mic")
        original = {
            "output": "airplay",
            "host": settings.airplay_host,
            "port": settings.airplay_port,
            "protocol": settings.airplay_protocol,
            "latency_ms": 600,
            "interface": "",
            "encryption": True,
            "microphone": "mic",
            "reference_version": 2,
        }
        self.assertEqual(CalibrationStore.target(settings, mic), original)
        low = replace(settings, airplay_render_lead_ms=250)
        self.assertEqual(CalibrationStore.target(low, mic)["render_lead_ms"], 250)
        self.assertNotEqual(
            CalibrationStore.key(settings, mic), CalibrationStore.key(low, mic)
        )
        self.assertEqual(low.airplay_latency_ms, 600)


class RenderLeadSenderTests(unittest.IsolatedAsyncioTestCase):
    async def test_sender_passes_requested_lead_independently_of_buffer(self):
        settings = Settings(
            tts_output="airplay",
            airplay_host="192.0.2.50",
            airplay_render_lead_ms=250,
            airplay_latency_ms=600,
        )
        rig, sender = SenderRig(), AirPlayOutput(settings)
        first, second = rig.patches()
        with first, second:
            try:
                await sender.start()
                arguments = rig.calls[0]
                self.assertEqual(arguments[arguments.index("--render-lead") + 1], "250")
                self.assertEqual(arguments[arguments.index("--latency") + 1], "600")
            finally:
                await sender.close()


@unittest.skipUnless(
    os.environ.get("AIRPLAY_TEST_BINARY"), "Patched native binary required"
)
class NativeRenderLeadTests(unittest.TestCase):
    def test_patched_cli_accepts_sweep_values_and_rejects_invalid_input(self):
        binary = os.environ["AIRPLAY_TEST_BINARY"]
        for value in ("2000", "1000", "500", "250"):
            result = subprocess.run(
                [binary, "--render-lead", value, "--check"],
                capture_output=True,
                timeout=5,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(b"0.5.9-live1", result.stdout)
        for value in ("0", "199", "3001", "500junk", "", "nan"):
            result = subprocess.run(
                [binary, "--render-lead", value, "--check"],
                capture_output=True,
                timeout=5,
            )
            self.assertNotEqual(result.returncode, 0)
