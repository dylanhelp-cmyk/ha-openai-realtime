# Standalone AirPlay sender

Direct AirPlay mode invokes **cliairplay v0.5.9** as a separate executable.
Music Assistant's server is not included or required.

- Project and corresponding release source: <https://github.com/music-assistant/airplay-cli/tree/v0.5.9>
- Release, source archives, checksums and binary assets: <https://github.com/music-assistant/airplay-cli/releases/tag/v0.5.9>
- Exact source revision: `0a58c239d5fb3cf8927b9707a71c6fe897266b6a`
- Source build instructions and submodule dependencies: <https://github.com/music-assistant/airplay-cli/blob/v0.5.9/README.md#building>
- Included libraop source revision: <https://github.com/philippe44/libraop/tree/81c2182649da8645ac2a58b78e9f370c79a4165b>
- Upstream licence: <https://github.com/music-assistant/airplay-cli/blob/v0.5.9/LICENSE>
- Complete upstream third-party inventory, copyright notices, licences and component source references: <https://github.com/music-assistant/airplay-cli/blob/v0.5.9/THIRD_PARTY_NOTICES.md>

The upstream combined executable is distributed under **GNU GPL version 3**;
this executable is not relicensed under the surrounding project's licence.
Its source and build dependencies remain available at the corresponding
upstream references above. Preserve its licence and notices when redistributing.

The image installs the upstream, checksum-verified `LICENSE` and
`THIRD_PARTY_NOTICES.md` alongside this notice under
`/usr/share/doc/cliairplay/`. No native binary is downloaded at add-on runtime.
Both amd64 and aarch64 asset hashes are pinned in the Dockerfile, and the binary
is checked with `--check` during each image build.

## Verification boundaries

Automated tests cover the Python sender contract, persistent speech/silence
input, resampling, errors, cancellation, process/FIFO cleanup, calibration
profile separation and a real released binary against a local RTSP/RTP peer.
That synthetic peer is not a real WiiM, does not validate Apple's entire
receiver stack, and cannot establish physical playback latency or acoustic
echo cancellation performance. The next-wake diagnostic measures those on
the user's installation without opening an OpenAI session.
