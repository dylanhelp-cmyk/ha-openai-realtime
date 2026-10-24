# GPT-Live Voice Agent 1.2.1

Wake → microphone starts immediately → GPT-Live streams the configured greeting → conversation → quiet timeout closes the paid session.
No wake chime, no manual turn commits, and no always-connected OpenAI session.

## Upgrade from 1.2.0: low calibration correlation

Update the add-on to **1.2.1** and repeat calibration with the same settings and a comfortable speaker volume. Keep `aec_calibration_min_confidence: 0.35`. No ESP32 flash is required. Previously saved delays remain valid; failed tests still leave them unchanged.

The original whole-waveform detector could score below 0.2 even with a clean, audible return when playback and capture clocks differed slightly. Higher speaker volume cannot correct that mismatch, and can introduce clipping. The new detector also compares voice-band frequency envelopes, which tolerate clock skew, phase changes, speaker EQ and gain changes. All three sweeps must independently match and agree on delay within 16 ms, and competing delay peaks are rejected. Where reliable, waveform matching retains sample-level precision; otherwise spectral matching provides 4 ms resolution for the adaptive filter.

Logs report `method`, `waveform`, `spectral`, individual sweep scores and delay spread, plus microphone RMS/peak levels (PCM16 full scale is 32768) and clipping on failure. Scores are normalised matches, not probabilities; waveform and spectral scores measure different properties. A low waveform score can now be accepted when the spectral and consistency checks pass. A low spectral score still requires investigation: check that the device hears all three sweeps, other sound is stopped, microphone levels are nonzero, and the candidate delay is inside the configured search range. Paste the full `Echo calibration NOT saved` line when seeking help; it contains numeric diagnostics, not captured audio.

## Upgrade from 1.1.0: live external-speaker echo cancellation

Update the add-on to **1.2.1**. Keep Voice PE firmware **2.0.0**, the current device YAML, working ports, speaker and greeting. No ESP32 flash is required.

The backend now uses SpeexDSP adaptive acoustic echo cancellation before forwarding microphone audio to GPT-Live. The reference is the actual paced PCM frame sent into the existing continuous MP3 stream, including silence. Capture is processed in 20 ms blocks at 16 kHz and then resampled to Live's format. Full duplex remains open throughout; no echo-triggered microphone muting or per-reply playback restarts are added.

### Measure the playback offset

1. Keep the WiiM in its normal source/group arrangement and at a comfortable listening volume. Stop other sounds and keep quiet during the test.
2. Merge these into your add-on configuration, save and restart:

   ```yaml
   microphone_mode: full_duplex
   aec_enabled: true
   aec_delay_ms: -1
   aec_auto_delay: true
   aec_calibrate_on_next_wake: true
   ```

3. Say **Hey Jarvis**. This wake runs a local calibration instead of opening GPT-Live. Three short, soft-edged sweeps play through the normal HTTP/MP3 speaker path; the Voice PE records their return. With the default search range the test lasts roughly eight seconds after speaker startup. No test audio is sent to OpenAI.
4. Read the add-on log. A usable result looks like `Echo calibration SAVED: delay=... ms, confidence=..., peak_ratio=..., method=...`. The delay includes encoding, player buffering, room travel and the microphone capture/return path. The confidence value is a normalised correlation score for the reported method, not a probability.
5. Set `aec_calibrate_on_next_wake: false`, save and restart. With `aec_delay_ms: -1`, the saved delay is used automatically. The log confirms `Live AEC active` and its effective delay on the next conversation.

Calibration is one-shot per add-on start: later wakes are normal conversations even while the flag remains true. Turn the flag off before later restarts unless you want to repeat the test. Failed, ambiguous, clipped or interrupted captures do not overwrite the last good measurement. The saved result is scoped to the speaker entity, microphone network address, announcement setting and stream prebuffer. It survives add-on restarts and updates in `/data/echo_calibration.json`; raw audio is kept only in bounded memory and discarded after the test.

Until a matching calibration is available, automatic mode logs that AEC is awaiting calibration and passes microphone audio through unchanged. A manual non-negative `aec_delay_ms` also enables processing. AEC is applied to `media_player` output only; local Voice PE output keeps its existing hardware processing.

### Tune the cancellation

| Setting | Default | Purpose |
| --- | --- | --- |
| `aec_enabled` | true | Enable or bypass external-speaker AEC. |
| `aec_delay_ms` | -1 | Use saved measurement; any value from 0 to 10000 overrides it manually. Positive means the echo arrives later than the reference. |
| `aec_auto_delay` | true | Follow small changes and independent-clock drift using strongly correlated audio. |
| `aec_delay_search_ms` | 80 | Search radius around the effective offset during tracking. |
| `aec_alignment_margin_ms` | 20 | Internal reference lead for the adaptive filter. Do not subtract this from the measured delay yourself. |
| `aec_filter_ms` | 256 | Room-response tail after aligning the main delay. Longer filters use more CPU and adapt more slowly. |
| `aec_suppression_db` | -25 | Residual echo suppression when only the assistant speaks; more negative is stronger. |
| `aec_suppression_active_db` | -8 | Residual suppression while you speak too; move toward 0 if your voice is being reduced. |
| `aec_log_stats` | false | Log effective delay and before/after microphone RMS every five seconds. These levels may include your voice and are not echo-only quality measurements. |
| `aec_calibration_max_delay_ms` | 5000 | Measurement search range; increase for a very slow player. |
| `aec_calibration_min_confidence` | 0.35 | Minimum spectral match for the complete probe and each sweep; weak, inconsistent or ambiguous results are rejected. |
| `aec_calibration_level` | 0.15 | Test signal peak level; does not change WiiM volume. |

The delay selects an older reference; it does **not** hold microphone audio for that many milliseconds. Small corrections are smoothed to avoid reference jumps. Periodic tones and weak matches during user speech do not move the offset. The native adaptive filter and residual suppression handle the echo; tracking is only for alignment.

Start with the measured delay and defaults. If user speech sounds reduced during simultaneous speaking, bring `aec_suppression_active_db` closer to 0 before increasing suppression elsewhere. If echo remains, check alignment before increasing `aec_filter_ms`. Recalibrate after large source, grouping, buffering or placement changes. The adaptive filter needs a brief convergence period at each new conversation. Player clipping, nonlinear speaker processing and large variable delays can limit cancellation; physical WiiM performance still needs testing.

Implementation references: [SpeexDSP echo API](https://github.com/xiph/speexdsp/blob/master/include/speex/speex_echo.h), [residual suppression controls](https://github.com/xiph/speexdsp/blob/master/include/speex/speex_preprocess.h), and [Speex echo timing guidance](https://www.speex.org/docs/manual/speex-manual/node7.html).

## Upgrade from GPT-Live 1.0.0

Update the add-on to 1.1.0 and restart it. Voice PE firmware 2.0.0 and your existing device YAML need no changes. Preserve your working ports, player, keys, greeting and `microphone_mode: full_duplex`.

One encoder and one speaker playback request now cover the entire conversation. The HTTP connection carries real-time speech and silence until Stop, idle closure or the duration cap. The speaker starts warming at wake, before the generated greeting arrives. Short or long pauses do not replace the URL, finish the encoder or issue another `play_media` call.

Remove obsolete `output_gap_timeout_ms` and `audio_retention_seconds` if Supervisor retains them. Quiet detection now affects speech activity only. The new `output_buffer_ms` bounds incoming PCM and `tts_stream_buffer_seconds` bounds the rolling HTTP buffer; neither waits to fill before playing.

Full duplex no longer clears captured input or runs a mic-reopening delay after each reply. It keeps listening while the single output stream runs. `gated` remains an optional echo workaround and uses quiet detection plus the configured playback/echo margins, without splitting the output stream.

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
microphone_mode: full_duplex
mic_reopen_delay_ms: 150
tts_stream_prebuffer_ms: 120
tts_start_allowance_ms: 300
idle_timeout_seconds: 15
max_session_seconds: 180
```

The HTTP address must be reachable from the WiiM. Keep it on the LAN. One encoder produces continuous MP3 for the whole conversation, including keepalive silence. The unique per-conversation URL tolerates WiiM's announcement cache parameter. The speaker's own buffering still affects latency; this change removes repeated startup rather than claiming zero delay. Spotify Connect resume remains unsupported by the current WiiM integration; this upgrade does not claim to fix it.

## Greeting and microphone behaviour

The greeting text is included at session startup and sent once through `session.commentary.append` after `session.started`, with input audio running. GPT-Live may paraphrase, add words, or stay silent. A commentary acknowledgement is not proof of audible playback. No prerecorded clip or secondary TTS service is substituted. Empty `opening_greeting` disables it.

`full_duplex` is the default and keeps the microphone open throughout the conversation without clearing captured user audio between replies. `gated` optionally closes it during speech activity and reopens after the quiet period and echo guard. Both modes keep the same output stream open through pauses. With external ceiling speakers, run the echo calibration above to enable software AEC. Full duplex and the measured alignment still need a physical listening test. The Stop word, centre button and mute still terminate the conversation.

## Timing controls

Every option has help on the Configuration tab. Save and restart after editing.

| Setting | Default | Effect |
| --- | --- | --- |
| `tts_stream_prebuffer_ms` | 120 | Initial MP3 cushion at wake; never repeated between replies. |
| `tts_stream_buffer_seconds` | 5 | Bounded HTTP memory window, not a waiting delay. |
| `output_buffer_ms` | 3000 | Maximum queued PCM; fails rather than accumulating delayed speech. |
| `playback_prebuffer_ms` | 100 | Voice PE's local PCM jitter cushion. |
| `tts_start_allowance_ms` | 300 | Speaker buffer estimate for gated mic reopening only. |
| `mic_reopen_delay_ms` | 150 | Additional gated-mic echo decay; ignored in full duplex. |
| `output_silence_ms` | 350 | Quiet period for speech activity, LEDs and optional gating; stream stays open. |
| `output_silence_threshold_db` | -55 | Speech activity sensitivity; does not remove quiet audio samples. |
| `input_activity_threshold_db` | -42 | Microphone activity that refreshes the idle timer. |
| `input_buffer_ms` | 600 | Capture retained during network startup; older excess is dropped. |
| `idle_timeout_seconds` | 15 | Quiet time before closing the paid session, after audio/tools finish. |
| `max_session_seconds` | 180 | Hard cap, including noisy-room cases. |

Output is clocked in 20 ms blocks. When Live has no queued output, silence keeps the player connection and decoder running. Speech detection does not split or discard audio. Keepalive silence does not refresh the idle timer, so the paid session still closes normally. A reconnect uses the same URL and joins near the live edge; the URL expires immediately when the conversation closes. Slow readers cannot grow memory without limit.

## Home Assistant control

The app fetches MCP tool definitions and optionally a device-name snapshot once at startup. Known-device commands do not require a preliminary catalogue request. The separate Responses backend chooses the existing tools, and the add-on executes them locally using the existing Assist permissions. Supervisor/MCP credentials are never forwarded to OpenAI or the speaker. Backend tools receive only their arguments and return explicit success/error results. Calls are deduplicated by call ID, all pending function results are returned before continuation, and failed mutations are not automatically retried. Closing a conversation does not roll back an already-issued device action.

## Cost and diagnostics

GPT-Live voice costs **US$0.05/minute of active session time**, billed per second, including silence and backend work. The reasoning backend and hosted web search are charged separately. This implementation connects to OpenAI only after a wake and closes on idle, Stop, mute, disconnect, failure, or the maximum duration. It does not keep a paid session warm between wakes. Final usage is logged only when confirmed by API events; forced disconnects can leave final usage unknown.

Transcript logging is off by default. Useful logs include startup latency, no-audible-greeting warnings, tool result status, dropped startup input, and confirmed session usage. A playback fetch timeout means the speaker could not reach its URL, not an intentional delay. Tests use a local fake Live endpoint, MCP doubles, real HTTP and ffmpeg, including decoding multiple replies and quiet gaps from one connection; no user's API key or live home-device actions are used.

## Runtime and protocol

`aiohttp` handles both WebSockets. Device input is mono PCM16 at 16 kHz, converted statefully to Live's 24 kHz PCM16; local output remains 24 kHz. External output is one continuously paced MP3 stream per conversation. Reference-aligned SpeexDSP AEC processes captured microphone audio locally when an offset is measured or configured; NumPy supplies bounded reference interpolation and correlation. Local PCM output is also continuously paced through speech and silence. Input is paced in 20 ms blocks, including silence while capture is gated, with bounded buffering. There is no Pipecat, SDK-specific Realtime adapter, second transcription model, legacy VAD, buffered media-source fallback, web-search wrapper, recording subsystem, or wake-chime asset.

Source references: [Live WebSockets](https://developers.openai.com/api/docs/guides/voice-websockets), [session greeting/lifecycle](https://developers.openai.com/api/docs/guides/live-conversations), [delegation/tools](https://developers.openai.com/api/docs/guides/live-delegation), [prompting](https://developers.openai.com/api/docs/guides/live-prompting), [pricing](https://developers.openai.com/api/docs/models/gpt-live-1).
