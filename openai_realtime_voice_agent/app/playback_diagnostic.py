"""One wake compares the selected external transport and local PCM."""

import asyncio
import logging
from dataclasses import replace

from app.calibration import CalibrationSession

log = logging.getLogger(__name__)


class PlaybackDiagnosticSession:
    def __init__(self, settings, device, http, server, token, store):
        self.s, self.device = settings, device
        self.http, self.server, self.token, self.store = http, server, token, store
        self.closed = False
        self.current = self._trial(
            settings, server, software_loopback=settings.tts_output == "media_player"
        )

    def _trial(self, settings, server, software_loopback=False):
        return CalibrationSession(
            settings,
            self.device,
            self.http,
            server,
            self.token,
            self.store,
            save_result=False,
            end_device=False,
            software_loopback=software_loopback,
        )

    def receive_audio(self, audio):
        if not self.closed:
            self.current.receive_audio(audio)

    async def run(self):
        try:
            log.info(
                "Playback comparison START: %s, then Voice PE local speaker. Keep quiet; six tones total. No OpenAI connection or AEC calibration writes.",
                "direct AirPlay (no HTTP/MP3 software reference)"
                if self.s.tts_output == "airplay"
                else "external speaker plus software HTTP/MP3 reference",
            )
            await self.current.run()
            external, software = self.current.result, self.current.loopback_result
            if self.closed:
                return
            await asyncio.sleep(1)
            if self.closed:
                return
            local_settings = replace(self.s, tts_output="voice_pe")
            self.current = self._trial(local_settings, None)
            await self.current.run()
            if self.closed:
                return
            local = self.current.result

            def value(result):
                return f"{result['delay_ms']:.0f}" if result else "unavailable"

            remainder = (
                f"{external['delay_ms'] - software['delay_ms']:.0f}"
                if external and software
                else "unavailable"
            )
            log.info(
                "Playback comparison RESULT: external_roundtrip_ms=%s, software_http_mp3_ms=%s, voice_pe_roundtrip_ms=%s, external_minus_software_ms=%s; output=%s; remainder includes LAN/player/acoustics/microphone return, not pure WiiM buffering. Saved AEC calibration unchanged.",
                value(external),
                value(software),
                value(local),
                remainder,
                self.s.tts_output,
            )
            if not local:
                log.info(
                    "Voice PE reference unavailable: its own hardware echo cancellation may suppress the tones; missing correlation is not evidence of zero delay."
                )
            log.info(
                "Playback comparison complete. Later wakes are normal conversations. Turn playback_diagnostic_on_next_wake off before future restarts."
            )
        finally:
            await self.close()

    async def close(self):
        if self.closed:
            return
        self.closed = True
        await self.current.close()
        await self.device.end()
