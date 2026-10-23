<p align="center">
  <img src="openai_realtime_voice_agent/icon.png" alt="OpenAI Realtime 2 Voice Agent — a stylised Voice PE" width="160"/>
</p>

# OpenAI Realtime 2 Voice Agent (Home Assistant Voice PE)

> [!IMPORTANT]
> **This is 1 of 2 repos — you need both halves.** This repo is the **backend add-on**
> (the voice "brain"). It needs the custom Voice PE **firmware** to connect to it — the
> stock Home Assistant voice pipeline won't talk to this add-on. You must set up both:
> - 🧠 **Backend add-on** (this repo) — runs inside Home Assistant
> - 🔌 **Device firmware** → **[dylanhelp-cmyk/home-assistant-voice-pe](https://github.com/dylanhelp-cmyk/home-assistant-voice-pe)** (flashed onto the Voice PE)
>
> 📖 New here? The full **[INSTALL guide](https://github.com/dylanhelp-cmyk/home-assistant-voice-pe/blob/main/INSTALL.md)** walks through both halves, step by step.

A Home Assistant **add-on** that turns a [Voice PE](https://www.home-assistant.io/voice-pe/)
device into a low-latency voice assistant built on **OpenAI's Realtime API**
(`gpt-realtime-2`). The device streams microphone audio to this add-on over a
plain WebSocket; the add-on runs the Realtime speech-to-speech session and
controls Home Assistant through the official
**[Home Assistant MCP Server](https://www.home-assistant.io/integrations/mcp_server/)**
integration. STT, TTS and the LLM all run in the Realtime session — there is no
Home Assistant `voice_assistant` pipeline on the audio path.

> Fork of **[fjfricke/ha-openai-realtime](https://github.com/fjfricke/ha-openai-realtime)**,
> retargeted at `gpt-realtime-2`, the official HA MCP Server, optional web search,
> and the **Voice PE thin-client firmware** (a separate repo — see below).

## Repository layout

- **[`openai_realtime_voice_agent/`](openai_realtime_voice_agent/)** — the Home
  Assistant add-on (Python / [Pipecat](https://github.com/pipecat-ai/pipecat)).
  This is the only thing you install.
  - [`DOCS.md`](openai_realtime_voice_agent/DOCS.md) — full setup: OpenAI key, the
    Home Assistant MCP connection, recommended settings, web search, all options.
  - [`CHANGELOG.md`](openai_realtime_voice_agent/CHANGELOG.md) — what changed per version.

The **device firmware** lives in its own repository —
**[dylanhelp-cmyk/home-assistant-voice-pe](https://github.com/dylanhelp-cmyk/home-assistant-voice-pe)**
(a custom `va_client` ESPHome component, specific to the Voice PE hardware).

## Install

1. In Home Assistant, open **Settings → Add-ons → Add-on store → ⋮ → Repositories**
   and add `https://github.com/dylanhelp-cmyk/ha-openai-realtime`.
2. Install **OpenAI Realtime 2 Voice Agent**. It ships with no prebuilt `image:`,
   so Home Assistant builds it locally on first install (a few minutes on a Pi).
3. Configure the add-on and flash the companion firmware — see
   [`openai_realtime_voice_agent/DOCS.md`](openai_realtime_voice_agent/DOCS.md).

(An optional GitHub Actions workflow can publish container images to ghcr.io; it
isn't needed for a normal local-build install.)

## Switching from the original add-on

This fork's **0.6.1** release fixes duplicate pipeline startup. The add-on is
named **OpenAI Realtime 2 Voice Agent (Dylan fork)** and defaults to port **8081**.
Adding this repository does not update an add-on installed from another repository.

1. Copy the original add-on's Configuration YAML to a private local note so you
   can transfer your API key and settings.
2. Stop the original add-on and turn off its **Start on boot** and **Watchdog**
   toggles so it stays stopped.
3. Add `https://github.com/dylanhelp-cmyk/ha-openai-realtime` in the add-on store's
   **Repositories**, then install **OpenAI Realtime 2 Voice Agent (Dylan fork)**.
4. Paste your saved configuration into the new add-on. Set
   `websocket_port: 8081`, save, and start it.
5. Keep the Voice PE's `va_url` at `ws://<Home Assistant IP>:8081/`.
   The device reconnects automatically. Verify a device connection appears in
   the new add-on's logs before removing the old add-on.

## Choose a speaker for replies and wake chimes (0.7.1+)

Keep wake-word detection and the microphone on the Voice PE, and optionally
send the original OpenAI voice to a Home Assistant media player. In the add-on
Configuration, set:

```yaml
tts_output: media_player
tts_media_player: media_player.living_room
tts_announce: true
```

Use your speaker's entity ID, then save and restart the add-on. Keep your
working device port unchanged. For wake sounds on that same speaker, also
install firmware **1.3.0+** from the companion fork using ESPHome Builder.
The Voice PE waits for the remote chime to finish before listening. External
playback buffers audio first and adds some delay. The player must support
local MP3 playback through HA's Media source; announcement/music restoration
depends on its integration. Set `tts_output: voice_pe` to switch back.
See [the output guide](openai_realtime_voice_agent/DOCS.md#play-replies-on-another-speaker)
for requirements, timing, and troubleshooting.

## How it works

```
Voice PE (ESP32-S3)  ──WS, 16 kHz PCM up──▶   this add-on    ──▶  OpenAI Realtime API
  va_client firmware  ◀──── 24 kHz PCM down──  (Pipecat)          (gpt-realtime-2)
                                                   │ tools
                                                   ▼
                                         Home Assistant MCP Server
```

The device does wake-word detection and XMOS audio cleanup locally and is a thin
client. Interrupt a reply with the **"stop"** word or the center button.

## Known limitations

- **No voice timers or alarms yet** — every other Assist action (lights, switches,
  scenes, climate) and online questions work.
- **A brief reconnect about once an hour** (OpenAI's 60-minute session cap; the
  add-on refreshes proactively during a quiet moment, so it rarely interrupts).
- **Rarely, the assistant may stop itself** on a word in its own reply that sounds
  like "stop" — just ask again.

## Credits

- Based on **[xandervanerven/ha-openai-realtime](https://github.com/xandervanerven/ha-openai-realtime)**; original credits are retained below.

- Forked from **[fjfricke/ha-openai-realtime](https://github.com/fjfricke/ha-openai-realtime)**.
- Built on **[Pipecat](https://github.com/pipecat-ai/pipecat)**.
- Firmware thin-client design based on **[maxmaxme/home-assistant-voice-pe](https://github.com/maxmaxme/home-assistant-voice-pe)** (a fork of **[esphome/home-assistant-voice-pe](https://github.com/esphome/home-assistant-voice-pe)**, Nabu Casa / ESPHome).
- Inspiration from **[marcinnowak79/home-assistant-voice-pe](https://github.com/marcinnowak79/home-assistant-voice-pe)** (gemini-live-proxy).

## License

MIT — see [LICENSE](LICENSE).
