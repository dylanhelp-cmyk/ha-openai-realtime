# GPT-Live Voice Agent

A greeting-first GPT-Live voice assistant for Home Assistant Voice PE and network speakers.

Say Hey Jarvis: the microphone starts immediately, GPT-Live opens with your configurable greeting, and the conversation continues. Home Assistant tools run through a separately configurable Responses backend. The paid voice session closes when the conversation is quiet.

See [the upgrade and configuration guide](DOCS.md) before updating from 0.8.0. Companion firmware **2.0.0** is required for the complete new behaviour. Preserve your working device port and Hey Jarvis override.

The add-on keeps its existing slug so Home Assistant can update the installed fork. GPT-Live API access and a supported backend model are required. Physical WiiM latency and full-duplex echo still need listening tests.
