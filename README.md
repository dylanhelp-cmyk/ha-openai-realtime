# Home Assistant GPT-Live Voice Agent

This fork now uses **GPT-Live 1**, with a separate Responses backend for Home Assistant device control and optional web search.

- Wake starts microphone capture and a new Live session immediately.
- A configurable live-generated greeting replaces the wake chime.
- Choose continuous Voice PE PCM, direct AirPlay, or one continuous HTTP MP3 stream per conversation to a Home Assistant media player.
- Keep full-duplex capture through replies; calibrate external-speaker echo delay and tune cancellation, buffers and idle timeout.
- OpenAI stays disconnected between conversations; existing Assist permissions are preserved.
- Startup logs separate OpenAI connection/session timing, first audio and speaker handoff.
- A next-wake diagnostic compares external MP3 playback, software HTTP/MP3 decoding and local Voice PE PCM without overwriting AEC calibration.
- Version 1.3.0 adds direct AirPlay with buffer controls, separate AEC profiles and a local next-wake test, without Music Assistant or a firmware update. See [AirPlay setup](openai_realtime_voice_agent/DOCS.md#direct-airplay-output-130).

Install this repository through the Home Assistant app/add-on store. Existing installations retain the `openai_realtime_voice_agent` slug and their working device port.

**Before upgrading from 0.8.0, read [the migration guide](openai_realtime_voice_agent/DOCS.md).** Update the companion [Voice PE firmware](https://github.com/dylanhelp-cmyk/home-assistant-voice-pe) to **2.0.0** as well. The former `.realtime.yaml` package URL remains a compatibility entry point to `.live.yaml`; keep your Hey Jarvis `!extend alexa` override.

GPT-Live is billed for active session duration, including silence; backend use is separate. The app closes idle sessions instead of keeping a paid voice connection warm. The new behaviour has automated protocol/audio tests; actual model access, greeting wording and physical WiiM buffering/echo performance require testing on your installation. Version 1.2.1 includes clock/phase-tolerant local next-wake calibration for external-speaker AEC; see the configuration guide.
