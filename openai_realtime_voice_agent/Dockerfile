ARG BUILD_FROM=ghcr.io/home-assistant/aarch64-base-debian:bookworm
FROM ${BUILD_FROM}
ENV PIP_BREAK_SYSTEM_PACKAGES=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-pip ffmpeg libspeexdsp1 \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml /tmp/app_build/pyproject.toml
COPY app/ /tmp/app_build/app/
RUN pip3 install /tmp/app_build && rm -rf /tmp/app_build
COPY app/ /app/
COPY root/ /
RUN chmod a+x /run.sh
WORKDIR /
CMD ["/run.sh"]
