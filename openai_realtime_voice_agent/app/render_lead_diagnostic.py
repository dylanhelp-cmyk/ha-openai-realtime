"""Controlled acoustic render-lead sweep, without OpenAI or calibration writes."""

import asyncio
import logging
from dataclasses import replace

from app.calibration import CalibrationSession

log = logging.getLogger(__name__)
LEADS = (2000, 1000, 500, 250)


class RenderLeadDiagnosticSession:
    def __init__(self, settings, device, http, server, token, store):
        self.s, self.device = settings, device
        self.http, self.token, self.store = http, token, store
        self.closed = False
        self.current = None
        self.results = []

    def receive_audio(self, audio):
        if not self.closed and self.current:
            self.current.receive_audio(audio)

    async def run(self):
        try:
            log.info(
                "Render lead sweep START: leads=%s ms, requested_buffer=%d ms held constant; twelve tones. Keep quiet. No OpenAI connection, automatic setting changes or AEC writes.",
                LEADS,
                self.s.airplay_latency_ms,
            )
            for lead in LEADS:
                if self.closed:
                    return
                settings = replace(self.s, airplay_render_lead_ms=lead)
                log.info(
                    "Render lead trial START: lead=%d ms, buffer=%d ms",
                    lead,
                    settings.airplay_latency_ms,
                )
                self.current = CalibrationSession(
                    settings,
                    self.device,
                    self.http,
                    None,
                    self.token,
                    self.store,
                    save_result=False,
                    end_device=False,
                    software_loopback=False,
                )
                await self.current.run()
                if self.closed:
                    return
                result = self.current.result
                self.results.append((lead, result))
                log.info(
                    "Render lead trial RESULT: requested_lead_ms=%d, roundtrip_ms=%s, confidence=%s; buffer_ms=%d; AEC unchanged",
                    lead,
                    f"{result['delay_ms']:.0f}" if result else "unavailable",
                    f"{result['confidence']:.3f}" if result else "unavailable",
                    settings.airplay_latency_ms,
                )
                await asyncio.sleep(1)
            summary = ", ".join(
                f"{lead}:{result['delay_ms']:.0f}" if result else f"{lead}:unavailable"
                for lead, result in self.results
            )
            log.info(
                "Render lead sweep RESULT: requested_lead_ms:measured_roundtrip_ms=[%s]; buffer_ms=%d; configured_lead_ms=%d unchanged. Missing correlation does not prove zero delay. No setting or AEC calibration saved.",
                summary,
                self.s.airplay_latency_ms,
                self.s.airplay_render_lead_ms,
            )
            log.info(
                "Render lead sweep complete; later wakes are normal conversations. Turn render_lead_diagnostic_on_next_wake off before future restarts."
            )
        finally:
            await self.close()

    async def close(self):
        if self.closed:
            return
        self.closed = True
        if self.current:
            await self.current.close()
        await self.device.end()
