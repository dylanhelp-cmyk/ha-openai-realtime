# GPT-Live Voice Agent 1.0.0

Wake → microphone starts immediately → GPT-Live streams the configured greeting → conversation → quiet timeout closes the paid session.
No wake chime, no manual turn commits, and no always-connected OpenAI session.

## Upgrade from Realtime 0.8

1. Keep a copy of your existing add-on and device configuration.
2. Update the add-on from this fork. Keep its existing installation slug, API key, HA access, voice, `websocket_port`, and speaker settings. No account re-linking is needed.
3. Install companion Voice PE firmware **2.0.0** from the firmware fork. Existing `home-assistant-voice.realtime.yaml` package references redirect to the new `.live.yaml` package, preserving device substitutions and your Hey Jarvis `!extend alexa` override.
4. Keep the same device `va_url`, including your working port. The repository example uses 8081; do not overwrite a working 18081 setup.
5. Save the new add-on settings and restart it. GPT-Live (`gpt-live-1`) and the chosen Responses backend must be available to your OpenAI project. Existing Realtime access does not prove Live/backend access.

Firmware 1.3 external-speaker wake requests are acknowledged immediately for transition compatibility, but firmware 2.0.0 is required for explicit microphone control, chime-free local output, and server-owned session expiry. Upgrade both halves before evaluating the new behaviour. Remove any old per-device `va_client.barge_in` override; microphone mode is now configured in the add-on. Firmware 2.0.0 refuses a Realtime-only backend.

Realtime-only fields are removed from the current schema. If your Supervisor retains unknown fields after an upgrade, remove them in Edit in YAML: `openai_model`, `openai_model_custom`, `openai_voice_custom`, `openai_speed`, `max_output_tokens`, `transcription_language`, `transcription_model`, `transcription_model_custom`, `turn_detection_type`, `vad_*`, `noise_reduction`, `follow_up_*`, `wake_open_delay_ms`, `phase_idle_debounce_ms`, `session_reuse_timeout_seconds`, `max_context_messages`, `web_search_model`, `web_search_model_custom`, `enable_recording`, `tts_delivery`, `tts_completion_mode`, `tts_finish_grace_ms`, `tts_state_poll_ms`, and `tts_state_timeout_ms`.

## Example: your existing WiiM speaker

Merge these into the add-on's new defaults, keeping your actual key and device port:

```yaml
opening_greeting: "Yes, how can I help?"
greeting_language: English
openai_voice: cedar
backend_model: gpt-6-luna
backend_reasoning_effort: low
websocket_port: 18081
tts_output: media_player
tts_media_player: media_player.living_room_speakers
tts_stream_base_url: http://192.168.1.56:18082
tts_stream_port: 18082
tts_announce: true
microphone_mode: gated
mic_reopen_delay_ms: 150
tts_stream_prebuffer_ms: 120
tts_start_allowance_ms: 300
idle_timeout_seconds: 15
max_session_seconds: 180
```

The HTTP address must be reachable from the WiiM. Keep it on the LAN. Replies are progressively encoded as MP3; the speaker's internal buffer still affects latency. Each speech segment has a unique temporary path, compatible with WiiM's announcement cache parameter. Spotify Connect resume remains unsupported by the current WiiM integration; this upgrade does not claim to fix it.

## Greeting and microphone behaviour

The greeting text is included at session startup and sent once through `session.commentary.append` after `session.started`, with input audio running. GPT-Live may paraphrase, add words, or stay silent. A commentary acknowledgement is not proof of audible playback. No prerecorded clip or secondary TTS service is substituted. Empty `opening_greeting` disables it.

`gated` mode opens the microphone at wake, closes it while audio plays, and reopens it after the playback clock and echo guard. `full_duplex` keeps the microphone open. With external ceiling speakers, echo can be interpreted as the user; full duplex needs a physical listening test and is not the default. The Stop word, centre button and mute still terminate the conversation.

## Timing controls

Every option has help on the Configuration tab. Save and restart after editing.

| Setting | Default | Effect |
| --- | --- | --- |
| `tts_stream_prebuffer_ms` | 120 | MP3 collected before asking the speaker to play. |
| `playback_prebuffer_ms` | 100 | Voice PE's local PCM jitter cushion. |
| `tts_start_allowance_ms` | 300 | Speaker startup/tail margin after the known audio duration. |
| `mic_reopen_delay_ms` | 150 | Additional echo decay before a gated mic opens. |
| `output_silence_ms` | 350 | Quiet audio needed to finish a speech segment. |
| `output_gap_timeout_ms` | 600 | Fallback when no more audio packets arrive. |
| `output_silence_threshold_db` | -55 | Silence detector sensitivity; more negative retains quieter audio. |
| `input_activity_threshold_db` | -42 | Microphone activity that refreshes the idle timer. |
| `input_buffer_ms` | 600 | Capture retained during network startup; older excess is dropped. |
| `idle_timeout_seconds` | 15 | Quiet time before closing the paid session, after audio/tools finish. |
| `max_session_seconds` | 180 | Hard cap, including noisy-room cases. |

GPT-Live has no per-spoken-response audio-done event. Silence/gap segmentation and playback timing are application estimates, with these controls exposed for tuning. Lower margins are quicker but can split phrases or reopen a microphone too early.

## Home Assistant control

The app fetches MCP tool definitions and optionally a device-name snapshot once at startup. Known-device commands do not require a preliminary catalogue request. The separate Responses backend chooses the existing tools, and the add-on executes them locally using the existing Assist permissions. Supervisor/MCP credentials are never forwarded to OpenAI or the speaker. Backend tools receive only their arguments and return explicit success/error results. Calls are deduplicated by call ID, all pending function results are returned before continuation, and failed mutations are not automatically retried. Closing a conversation does not roll back an already-issued device action.

## Cost and diagnostics

GPT-Live voice costs **US$0.05/minute of active session time**, billed per second, including silence and backend work. The reasoning backend and hosted web search are charged separately. This implementation connects to OpenAI only after a wake and closes on idle, Stop, mute, disconnect, failure, or the maximum duration. It does not keep a paid session warm between wakes. Final usage is logged only when confirmed by API events; forced disconnects can leave final usage unknown.

Transcript logging is off by default. Useful logs include startup latency, no-audible-greeting warnings, tool result status, dropped startup input, and confirmed session usage. A playback fetch timeout means the speaker could not reach its URL, not an intentional delay. Tests use a local fake Live endpoint, MCP doubles, real HTTP and ffmpeg; no user's API key or live home-device actions are used.

## Runtime and protocol

`aiohttp` handles both WebSockets. Device input is mono PCM16 at 16 kHz, converted statefully to Live's 24 kHz PCM16; local output remains 24 kHz. External output is progressive MP3. Input is paced in 20 ms blocks, including silence while capture is gated, with bounded buffering. There is no Pipecat, SDK-specific Realtime adapter, second transcription model, legacy VAD, buffered media-source fallback, web-search wrapper, recording subsystem, or wake-chime asset.

Source references: [Live WebSockets](https://developers.openai.com/api/docs/guides/voice-websockets), [session greeting/lifecycle](https://developers.openai.com/api/docs/guides/live-conversations), [delegation/tools](https://developers.openai.com/api/docs/guides/live-delegation), [prompting](https://developers.openai.com/api/docs/guides/live-prompting), [pricing](https://developers.openai.com/api/docs/models/gpt-live-1).
