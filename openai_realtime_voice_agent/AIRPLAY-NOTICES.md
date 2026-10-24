# Standalone AirPlay sender

Direct AirPlay mode invokes **cliairplay 0.5.9-live1**, a locally modified
source build of upstream v0.5.9, as a separate executable.
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

The only local native-code change is the auditable
[`native/render-lead.patch`](native/render-lead.patch): it exposes the existing
render-lead variable as an independently validated `--render-lead` integer
(200–3000 ms). The default remains 2000 ms. This is an experimental sender
request, not a promise that a receiver can render at the requested delay.

[`native/build-airplay.sh`](native/build-airplay.sh) verifies the exact upstream
source commit, checks out its pinned immediate submodule revisions, applies the
patch without fuzz, and rebuilds using the upstream Makefile. The Makefile uses
the upstream submodules' prebuilt static dependency archives and headers; this
is a source rebuild of the sender, not a from-scratch rebuild of every codec.
The helper is shared by the container builds and native CI tests. It checks
accepted/rejected render-lead inputs and the executable version. No native
binary or source is downloaded at add-on runtime.

The image retains the upstream `LICENSE` and `THIRD_PARTY_NOTICES.md`, this
notice, the exact patch/build helper in `build/`, and the modified upstream CLI
source archive in `modified-source.tar.gz`, all under
`/usr/share/doc/cliairplay/`. The archive omits Git metadata, build outputs and
the separately pinned libraop/dependency checkout. Corresponding unmodified
dependency source references, exact revisions, licences and original build
instructions are available through the source/submodule and third-party
inventory links above. Preserve the upstream licences, these modification
notices and source/build references when redistributing.

## Verification boundaries

Automated tests cover the Python sender contract, persistent speech/silence
input, resampling, errors, cancellation, process/FIFO cleanup, calibration
profile separation, render-lead input validation and the real patched binary
against a local RTSP/RTP peer.
That synthetic peer is not a real WiiM, does not validate Apple's entire
receiver stack, and cannot establish physical playback latency or acoustic
echo cancellation performance. The next-wake diagnostic measures those on
the user's installation without opening an OpenAI session.
