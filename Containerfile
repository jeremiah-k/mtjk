# SPDX-License-Identifier: GPL-3.0-or-later
#
# Copyright (C) 2025 Olliver Schinagl <oliver@schinagl.nl>
# Copyright (C) 2025 Jeremiah K. <jeremiahk@gmx.com>

# Build stage
FROM docker.io/library/python:3.14-slim-bookworm AS builder

WORKDIR /build

# Copy dependency files first to leverage Docker layer caching.
# README.md is required by pyproject.toml for the build.
COPY pyproject.toml poetry.lock README.md ./

# Export locked requirements from poetry.lock and install to prefix.
# This ensures reproducible builds with exact dependency versions.
RUN pip install --no-cache-dir poetry==2.4.1 && \
    poetry export --format requirements.txt --extras cli --extras tunnel \
    --without dev --output requirements.txt && \
    pip install --no-cache-dir --prefix=/install -r requirements.txt

# Copy source code, build the wheel, install it to prefix.
# --no-index ensures only the locally built wheel is used.
COPY meshtastic/ meshtastic/
RUN poetry build --format wheel --no-interaction && \
    pip install --no-cache-dir --no-index --no-deps --prefix=/install \
    --find-links=./dist "mtjk"

# Runtime stage
FROM docker.io/library/python:3.14-slim-bookworm

# Create a non-root user for security.
RUN useradd --system --create-home --home-dir /home/meshtastic meshtastic

# Copy installed Python packages from the builder.
COPY --from=builder /install /usr/local

# Copy entrypoint
COPY ./bin/container-entrypoint.sh /init
RUN chmod 0755 /init

# OCI metadata labels (supplied via build args from CI).
ARG BUILD_DATE
ARG VCS_REF
ARG VERSION
LABEL org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.title="mtjk" \
      org.opencontainers.image.description="Python API and CLI for Meshtastic devices (mtjk fork)" \
      org.opencontainers.image.url="https://github.com/jeremiah-k/mtjk" \
      org.opencontainers.image.source="https://github.com/jeremiah-k/mtjk" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.licenses="GPL-3.0-only"

ENV PYTHONUNBUFFERED=1

WORKDIR /home/meshtastic
USER meshtastic

ENTRYPOINT ["/init"]
