import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from app.device import Device
from app.render_lead_diagnostic import RenderLeadDiagnosticSession, LEADS
from app.settings import Settings
from test_live import Socket


class SweepTests(unittest.IsolatedAsyncioTestCase):
    async def test_settings_require_airplay_and_exclusive_diagnostic(self):
        for settings in (
            Settings(openai_api_key="fake", render_lead_diagnostic_on_next_wake=True),
            Settings(
                openai_api_key="fake",
                tts_output="airplay",
                airplay_host="192.0.2.50",
                render_lead_diagnostic_on_next_wake=True,
                playback_diagnostic_on_next_wake=True,
            ),
        ):
            with self.assertRaises(ValueError):
                settings.validate()
        Settings(
            openai_api_key="fake",
            tts_output="airplay",
            airplay_host="192.0.2.50",
            render_lead_diagnostic_on_next_wake=True,
        ).validate()

    async def test_sweep_changes_only_lead_and_never_saves_or_ends_between_trials(self):
        settings = Settings(
            tts_output="airplay", airplay_host="192.0.2.50", airplay_render_lead_ms=1500
        )
        device = Device(Socket(), settings)
        trials = []

        class Trial:
            def __init__(self, config, *args, **kwargs):
                trials.append((config, kwargs))
                self.result = (
                    {"delay_ms": config.airplay_render_lead_ms + 400, "confidence": 0.6}
                    if config.airplay_render_lead_ms >= 500
                    else None
                )
                self.run, self.close = AsyncMock(), AsyncMock()

        # Patch the module's time source, not asyncio.sleep globally.
        with patch("app.render_lead_diagnostic.CalibrationSession", Trial):
            session = RenderLeadDiagnosticSession(
                settings, device, None, None, "", None
            )
            with self.assertLogs("app.render_lead_diagnostic", "INFO") as logs:
                await session.run()
        self.assertEqual([s.airplay_render_lead_ms for s, _ in trials], list(LEADS))
        self.assertTrue(all(s.airplay_latency_ms == 600 for s, _ in trials))
        self.assertTrue(
            all(
                not k["save_result"]
                and not k["end_device"]
                and not k["software_loopback"]
                for _, k in trials
            )
        )
        self.assertEqual(settings.airplay_render_lead_ms, 1500)
        self.assertEqual(sum(e["type"] == "session_end" for e in device.ws.events), 1)
        self.assertIn("250:unavailable", "\n".join(logs.output))

    async def test_stop_prevents_remaining_trials(self):
        entered, release = asyncio.Event(), asyncio.Event()
        trial = AsyncMock()
        trial.result = None

        async def run():
            entered.set()
            await release.wait()

        trial.run.side_effect = run
        settings = Settings(tts_output="airplay")
        with patch(
            "app.render_lead_diagnostic.CalibrationSession", return_value=trial
        ) as factory:
            session = RenderLeadDiagnosticSession(
                settings, Device(Socket(), settings), None, None, "", None
            )
            task = asyncio.create_task(session.run())
            await entered.wait()
            await session.close()
            release.set()
            await task
        self.assertEqual(factory.call_count, 1)
