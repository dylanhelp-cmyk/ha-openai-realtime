# GPT-Live Voice Agent

A greeting-first GPT-Live voice assistant for Home Assistant Voice PE and network speakers.

Version **1.3.0** adds optional direct AirPlay: one continuous connection per conversation, tunable sender buffering and transport-specific AEC calibration. No Music Assistant or firmware change required. Start with the [local AirPlay playback comparison](DOCS.md#direct-airplay-output-130); physical latency improvement is not guaranteed.

Say Hey Jarvis: the microphone starts immediately, GPT-Live opens with your configurable greeting, and the conversation continues. Home Assistant tools run through a separately configurable Responses backend. The paid voice session closes when the conversation is quiet.

See [the upgrade and configuration guide](DOCS.md) before updating from 0.8.0. Companion firmware **2.0.0** is required for the complete new behaviour. Preserve your working device port and Hey Jarvis override.

The add-on keeps its existing slug so Home Assistant can update the installed fork. GPT-Live API access and a supported backend model are required. Physical WiiM latency and full-duplex echo still need listening tests.

Version **1.2.0** adds external-speaker software echo cancellation and a local next-wake delay calibration. See [calibration and tuning](DOCS.md#upgrade-from-110-live-external-speaker-echo-cancellation) before evaluating full duplex.
