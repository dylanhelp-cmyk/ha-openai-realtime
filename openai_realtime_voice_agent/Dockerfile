ARG BUILD_FROM=ghcr.io/home-assistant/aarch64-base-debian:bookworm
FROM ${BUILD_FROM} AS airplay-build
RUN apt-get update && apt-get install -y --no-install-recommends build-essential git patch ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY native/ /buildtools/
RUN sh /buildtools/build-airplay.sh /tmp/airplay-source /usr/local/bin/cliairplay \
    && tar --exclude=./.git --exclude=./libraop --exclude=./build --exclude=./bin \
       -czf /tmp/cliairplay-modified-source.tar.gz -C /tmp/airplay-source .
FROM ${BUILD_FROM}
ENV PIP_BREAK_SYSTEM_PACKAGES=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-pip ffmpeg libspeexdsp1 libstdc++6 curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
# Exact upstream commit and submodule revisions are rebuilt, not binary-edited.
# Retain the modified source, patch, build instructions, licence and notices.
COPY --from=airplay-build /usr/local/bin/cliairplay /usr/local/bin/cliairplay
COPY --from=airplay-build /tmp/airplay-source/LICENSE /usr/share/doc/cliairplay/LICENSE
COPY --from=airplay-build /tmp/airplay-source/THIRD_PARTY_NOTICES.md /usr/share/doc/cliairplay/THIRD_PARTY_NOTICES.md
COPY --from=airplay-build /tmp/cliairplay-modified-source.tar.gz /usr/share/doc/cliairplay/modified-source.tar.gz
COPY native/ /usr/share/doc/cliairplay/build/
RUN /usr/local/bin/cliairplay --render-lead 2000 --check
COPY pyproject.toml /tmp/app_build/pyproject.toml
COPY app/ /tmp/app_build/app/
RUN pip3 install /tmp/app_build && rm -rf /tmp/app_build
COPY app/ /app/
COPY root/ /
COPY AIRPLAY-NOTICES.md /usr/share/doc/cliairplay/SOURCE.md
RUN chmod a+x /run.sh
WORKDIR /
CMD ["/run.sh"]
