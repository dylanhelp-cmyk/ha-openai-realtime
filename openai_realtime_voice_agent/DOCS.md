# OpenAI Realtime 2 Voice Agent — Documentation

> This is the Dylan fork (0.8.0), with live audio for network speakers,
> adjustable playback timing, and cached Assist device discovery.
> Its add-on defaults to port **8081**. Set the Voice PE's `va_url` to
> `ws://<Home Assistant IP>:8081/`. For an existing installation, follow
> [Switching from the original add-on](../README.md#switching-from-the-original-add-on).

This add-on runs an **OpenAI `gpt-realtime-2`** voice session and bridges it to Home
Assistant control and web search. It is the backend half of a two-part project; the
front half is custom **firmware for the Home Assistant Voice PE** device (see
[Firmware](#firmware-home-assistant-voice-pe-only) below).

```
Voice PE device  ──WebSocket──▶  this add-on  ──▶  OpenAI Realtime API
(mic up / speaker down)               │  tools
                                       ▼
                              HA MCP Server (/api/mcp)  → controls your home
```

---

## 1. Install the add-on

1. In Home Assistant go to **Settings → Add-ons → Add-on Store**.
2. Top-right **⋮ → Repositories**, add:
   `https://github.com/dylanhelp-cmyk/ha-openai-realtime`
3. Find **OpenAI Realtime 2 Voice Agent** in the store and click **Install**.
   (There is no prebuilt image — Home Assistant builds it locally the first time,
   which takes a few minutes.)
4. Open the add-on's **Configuration** tab to set it up (next sections).

## 2. Get an OpenAI API key

1. Go to <https://platform.openai.com/> → **API keys** → **Create new secret key**.
2. Make sure **billing** is set up on your OpenAI account — `gpt-realtime-2` (audio)
   and web search are paid usage.
3. Paste the key into the add-on's **`openai_api_key`** option.

> **Heads-up on rate limits:** new accounts start at a low tokens-per-minute (TPM)
> tier. `gpt-realtime-2` audio is token-heavy, so if you see *"Rate limit reached"*
> in the logs, raise your usage tier in the OpenAI dashboard, or keep
> `max_context_messages` modest (default 12).

## 3. Let it control Home Assistant (MCP)

The assistant controls your home through Home Assistant's **official MCP Server**.

1. In HA: **Settings → Devices & Services → Add Integration → "Model Context
   Protocol Server"** and add it.
2. **Expose the entities** you want voice control over to **Assist**
   (Settings → Voice assistants → *Exposed entities*). The MCP server only offers
   what's exposed.
3. In the add-on, leave **`ha_mcp_url`** **blank** — it then uses the built-in
   endpoint (`http://supervisor/core/api/mcp`) with the add-on's own token. Leave
   **`longlived_token`** blank too, unless startup logs a 401/403 on
   `/core/api/mcp` (then paste a HA long-lived token there).

You get a small fixed set of Assist tools (`HassTurnOn`, `HassTurnOff`,
`HassLightSet`, `GetLiveContext`, `GetDateTime`, …). **`GetLiveContext`** is the
"what's the current state?" tool — keep it; it's what answers *"is the light on?"*.

**`mcp_tool_allowlist`** (optional): a comma-separated whitelist of tool names. Leave
blank to expose all, or trim to just what you use, e.g.:
`HassTurnOn,HassTurnOff,HassLightSet,GetLiveContext,GetDateTime`

## 4. Recommended starting settings

**The defaults are the recommended settings** — for a first run you only need the
API key, the MCP integration (section 3), and ideally your language. The
Configuration tab is grouped: **🔑 Basics → 🗣️ Model & voice → 💬 Conversation →
🌐 Web search → 🎚️ Audio → 🏠 Home Assistant → ⚙️ Advanced → 🔍 Debug**, and every
option has plain-language inline help.

| Option | Default | Note |
|---|---|---|
| `openai_model` | `gpt-realtime-2` | newest speech-to-speech model |
| `openai_voice` | `marin` | `marin`/`cedar` are the newest voices |
| `transcription_language` | *(blank)* | set your ISO code (e.g. `nl`): locks the language + logs the user transcript |
| `instructions` | *(English default)* | the system prompt; swap the LANGUAGE line for your language |
| `follow_up_listen_seconds` | `8` | mic stays open this long so you can answer back |
| `follow_up_open_delay_ms` | `700` | echo guard before the follow-up mic opens; lower = snappier but risks ghost turns |
| `wake_open_delay_ms` | `700` | the same echo guard right after the wake chime; lower = snappier wake but risks a ghost turn |
| `vad_eagerness` | `low` | waits longest before deciding you're done talking |
| `playback_prebuffer_ms` | `150` | raise to ~250 if you hear crackle; 0 = play immediately |
| `max_context_messages` | `12` | bounds per-turn token cost |
| `enable_web_search` | `true` | online lookups; set `false` to disable |
| `web_search_model` | `gpt-5.5` | best-quality search model; mini/nano are cheaper |

The legacy `server_vad` turn-detection fields live at the bottom of ⚙️ Advanced and
only appear when you enable **"Show unused optional configuration options"** —
leave them unset unless you have a specific reason.

## 5. Web search

When **`enable_web_search`** is on (**the default**), the assistant gets a `web_search` tool. When
it needs current or general info (weather, news, facts), it calls that tool; the
add-on then makes a **second, server-side OpenAI call** (the Responses API
`web_search` built-in tool, on **`web_search_model`**) and reads a short spoken
answer back.

- Uses your **existing OpenAI key** — no extra account.
- Default model `gpt-5.5` (best quality). Cheaper options trade price/quality
  (`gpt-5.4`, `gpt-5-mini`, the nano models, …) — a few cents per search.
- Adds ~1–3 s while it searches (the device shows "thinking").
- If the model name is rejected, the assistant just says it couldn't search — it
  won't crash the session, so you can change `web_search_model` and retry.

## 6. Options reference & tuning

### Play replies on another speaker

Update the add-on to **0.8.0** or later, then open **Configuration → Edit in YAML**
and add or change these options alongside your existing settings:

```yaml
tts_output: media_player
tts_media_player: media_player.living_room
tts_announce: true
tts_delivery: stream
tts_stream_base_url: http://192.168.1.100:18082
tts_stream_port: 18082
tts_completion_mode: auto
```

Replace `media_player.living_room` with the exact entity ID of your speaker
(find it in **Developer tools → States**). Replace the IP in the audio URL with
**your Home Assistant LAN IP**, reachable by the speaker. Use a free port,
and use the same number in both HTTP options. Save and restart the add-on. Keep
your working `websocket_port` and Voice PE `va_url` unchanged. Spoken replies
go only to the selected speaker. The Voice PE continues to handle wake words,
microphone and LEDs.

For the **wake chime on the same external speaker**, also install firmware
**1.3.0 or later** from `dylanhelp-cmyk/home-assistant-voice-pe` using ESPHome
Device Builder → your Voice PE → **Install → Wirelessly**. Keep your existing
device settings and Hey Jarvis override. The backend tells updated firmware
which output is selected, so no extra routing option is needed in ESPHome.
Updated firmware suppresses the device's own wake/center-button chime in
external mode, asks the backend to play the original Voice PE wake sound on
the selected player, and waits for it to finish before opening the microphone.
The device's Wake sound switch still controls the wake-word chime.

Update the add-on first, then the firmware. Older firmware keeps its local
chime while benefiting from the 0.7.1 silent-reply fix. If you switch back to
`tts_output: voice_pe`, local wake sounds and streaming replies work as before.

- The original OpenAI speech is encoded as MP3; the configured voice and speed
  still apply. This does not call another TTS provider or regenerate the reply.
- `tts_delivery: stream` progressively encodes and serves MP3 while OpenAI is
  still generating the reply. This removes the wait for a complete response;
  the speaker's network and playback buffers still affect latency. Raw Realtime
  WebSocket PCM cannot be passed directly to an HTTP media player.
- `tts_delivery: buffered` waits for each complete audio segment and remains
  the default for compatibility. It can use the same Audio HTTP address, so
  WiiM announcement fixes also work without live streaming.
- `tts_announce: true` requests an announcement. Players that support it may
  restore their music afterward. Set it to `false` for ordinary playback, which
  replaces the current media. Music restore/ducking depends on the integration.
- **WiiM Audio integration:** use the Audio HTTP address for announcements.
  Its added cache parameter cannot invalidate these per-turn URLs, unlike HA's
  signed media-source URLs. Each chime/reply gets a fresh path. When the previous
  item is an assistant URL, ordinary playback prevents it being resumed as music.
- `auto` completion with Audio HTTP uses the actual speaker GET, audio duration
  and an adjustable startup allowance. It avoids waiting for delayed HA state
  updates. This is an estimate of audible completion, so tune the allowance
  for your speaker. Choose `state` to use HA state with a bounded fallback.
- "Stop" or the center button cancels pending replies and requests
  `media_player.media_stop` for active external playback. It does not stop
  unrelated music when no reply is active. The player must support stopping.
- With firmware 1.3.0+, pressing the center button, muting, disconnecting, or starting another
  wake while the chime is playing cancels that pending wake. A late chime
  acknowledgement cannot reopen the microphone.
- To switch speakers, change `tts_media_player` and restart the add-on. To
  return speech to the device, set `tts_output: voice_pe` and restart.

The target must support HTTP MP3 playback; Spotify-only entities cannot play
arbitrary reply audio. Audio HTTP serves only generated audio at unpredictable
temporary paths, without forwarding HA credentials. Files expire 60 seconds
after playback. Keep this port on your LAN. A fetch timeout means the speaker
could not reach the configured IP/port; it is not an intentional playback delay.

With Audio HTTP empty, buffered mode uses HA's Media source integration and
shared `/media` directory as before. `media_source:` must be enabled and the
`local` media directory must map to `/media`. For WiiM, keep announcements off
on this legacy signed-URL path.

### External speaker timing

All values below are editable on the Configuration tab. Save and restart.

| Option | Default | What to tune |
| --- | --- | --- |
| `tts_stream_prebuffer_ms` | 120 | Audio collected before requesting live playback. Lower starts sooner; raise for stuttering. |
| `tts_start_allowance_ms` | 300 | Speaker startup/buffer margin for chimes and replies. Raise if the mic opens before sound ends; lower if it opens late. |
| `wake_open_delay_ms` | 700 | Echo guard after the wake chime. Try 150–300 for a quick external speaker. |
| `follow_up_open_delay_ms` | 700 | Echo guard after the reply. Try 150–300; raise if it hears its own tail. |
| `phase_idle_debounce_ms` | 1500 | Quiet margin before the idle phase. Try 150–300; raise if it returns to idle between reply segments. |
| `tts_completion_mode` | auto | Real HTTP fetch plus duration when configured; `state` polls HA; `duration` always uses duration. |
| `tts_finish_grace_ms` | 1500 | Maximum extra completion wait in state mode. Does not affect HTTP auto timing. |
| `tts_state_poll_ms` | 100 | Poll interval in state mode; faster polling cannot make the integration refresh faster. |
| `tts_state_timeout_ms` | 500 | Limit for a single state poll. |
| `tts_stream_start_timeout_s` | 10 | Failure timeout for fetching audio, not extra delay on successful playback. |

### Device lookup without a search before every command

`mcp_preload_context: true` reads the existing Assist-exposed device catalogue
once when the voice session connects, with a 60-second reconnect cache. It adds
startup work and model context, but no mandatory catalogue call to each control
request. Known names go straight to the control tools; unknown names or lookup
errors can trigger discovery. Turn it off for on-demand discovery only. No
additional entities are exposed. MCP errors and structured-only responses are
preserved so the model can distinguish a failed lookup from a successful action.

### Other settings

Every option has a description on the **Configuration** tab. The ones worth knowing:

- **Model / voice / transcription model** are dropdowns with a **`custom`** entry +
  a `*_custom` text field if you want a value not in the list.
- **`transcription_language`** turns the side-channel transcript on. With it set you
  get `🗣️ user: …` lines in the add-on log (handy for debugging); it does **not**
  change what the model understands — the main model hears your audio natively.
- **`follow_up_open_delay_ms` / `playback_prebuffer_ms`** default to `700` / `150`
  — an echo guard and jitter cushion. Lowering them makes the device feel
  snappier, but below ~700 ms open delay the reply's own speaker tail can leak
  into the fresh follow-up mic and become a ghost turn (the assistant "answers
  nobody" or repeats itself); raise the prebuffer if you hear crackle at the
  start of replies.

## 7. Reading the logs

The add-on log shows each turn: `🗣️ user:` (when transcription language is set),
`🤖 assistant:` (the reply text), `📞 phase ->` (device state), tool calls, and
`🔌 …reconnecting` / `✅ reconnected` on a connection recovery. View it on the add-on
**Log** tab.

## Known limitations

- **No voice timers or alarms yet.** Setting a timer by voice isn't supported (the
  official Home Assistant timer intent isn't wired up). Everything else — lights,
  switches, scenes, climate, and questions — works.
- **A brief reconnect about once an hour.** OpenAI limits a realtime session to
  60 minutes. The add-on refreshes proactively during a quiet moment, so you'll
  rarely notice it, but a reconnect can occasionally cause a ~1–2 second pause.
- **Rarely, the assistant may stop itself** on a word in its own reply that sounds
  like "stop" — just ask again.

**Using "stop":** say "stop" (or press the center button) to interrupt the assistant
*while it's speaking* — during a reply, or during the short listening window right
after one. It has no effect before the assistant has started answering (there's
nothing to stop yet).

## Firmware (Home Assistant Voice PE only)

This add-on expects the custom **Voice PE firmware** that turns the device into a
thin client (it streams mic audio here and plays the reply). That firmware:

- is **specific to the Home Assistant Voice PE** hardware (ESP32-S3 + XMOS), and
- lives in its own **public** repository:
  **[dylanhelp-cmyk/home-assistant-voice-pe](https://github.com/dylanhelp-cmyk/home-assistant-voice-pe)**.

You flash it once via a tiny per-device "stub" in ESPHome Builder; after that, firmware
updates are **one click** — no tokens, no copy-pasting. That repo has the full
from-scratch guide (flashing + adopting the device in ESPHome Builder).

## Credits

- Based on **[xandervanerven/ha-openai-realtime](https://github.com/xandervanerven/ha-openai-realtime)**; original credits are retained below.

- Backend forked from **[fjfricke/ha-openai-realtime](https://github.com/fjfricke/ha-openai-realtime)** (Felix Fricke).
- Firmware thin-client design based on **[maxmaxme/home-assistant-voice-pe](https://github.com/maxmaxme/home-assistant-voice-pe)**, a fork of **[esphome/home-assistant-voice-pe](https://github.com/esphome/home-assistant-voice-pe)** (Nabu Casa / ESPHome).
- Inspiration from **[marcinnowak79/home-assistant-voice-pe](https://github.com/marcinnowak79/home-assistant-voice-pe)** (gemini-live-proxy).
- Built on **[pipecat-ai](https://github.com/pipecat-ai/pipecat)**, the **OpenAI Realtime API**, and the official **[Home Assistant MCP Server](https://www.home-assistant.io/integrations/mcp_server/)** integration.
