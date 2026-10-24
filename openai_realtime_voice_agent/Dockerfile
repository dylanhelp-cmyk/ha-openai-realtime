ARG BUILD_FROM=ghcr.io/home-assistant/aarch64-base-debian:bookworm
FROM ${BUILD_FROM}
ENV PIP_BREAK_SYSTEM_PACKAGES=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-pip ffmpeg libspeexdsp1 libstdc++6 curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
# Immutable upstream release, architecture-specific checksums. No runtime download.
# Keep the executable's GPL and third-party notices with the distributed image.
RUN set -eu; \
    case "$(dpkg --print-architecture)" in \
      amd64) airplay_arch=x86_64; airplay_sha=affa85c5b3edff370f7c08f921d6a3095444acbc10579a3d12713e3b1ebe0425 ;; \
      arm64) airplay_arch=aarch64; airplay_sha=37fcc7dc59e4878f465afd2520923baefeb2c9b761c29f4d7fe5b2815bb3b354 ;; \
      *) exit 1 ;; \
    esac; \
    curl -fLsS --retry 3 --max-time 120 "https://github.com/music-assistant/airplay-cli/releases/download/v0.5.9/cliairplay-linux-${airplay_arch}" -o /usr/local/bin/cliairplay; \
    echo "${airplay_sha}  /usr/local/bin/cliairplay" | sha256sum -c -; \
    chmod 755 /usr/local/bin/cliairplay; \
    /usr/local/bin/cliairplay --check; \
    mkdir -p /usr/share/doc/cliairplay; \
    curl -fLsS --retry 3 --max-time 120 https://github.com/music-assistant/airplay-cli/releases/download/v0.5.9/LICENSE -o /usr/share/doc/cliairplay/LICENSE; \
    echo '3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986  /usr/share/doc/cliairplay/LICENSE' | sha256sum -c -; \
    curl -fLsS --retry 3 --max-time 120 https://github.com/music-assistant/airplay-cli/releases/download/v0.5.9/THIRD_PARTY_NOTICES.md -o /usr/share/doc/cliairplay/THIRD_PARTY_NOTICES.md; \
    echo '7103c466fe46a3cb412e34e52445495979f21a1e0eb794d5319761d7a06521d6  /usr/share/doc/cliairplay/THIRD_PARTY_NOTICES.md' | sha256sum -c -
COPY pyproject.toml /tmp/app_build/pyproject.toml
COPY app/ /tmp/app_build/app/
RUN pip3 install /tmp/app_build && rm -rf /tmp/app_build
COPY app/ /app/
COPY root/ /
COPY AIRPLAY-NOTICES.md /usr/share/doc/cliairplay/SOURCE.md
RUN chmod a+x /run.sh
WORKDIR /
CMD ["/run.sh"]
