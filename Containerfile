# SPDX-License-Identifier: GPL-3.0-or-later
#
# Copyright (C) 2025 Olliver Schinagl <oliver@schinagl.nl>
# Copyright (C) 2025 Jeremiah K. <jeremiahk@gmx.com>

# Build stage
FROM docker.io/library/python:3.14-slim-bookworm AS builder

# git is required for pip VCS installs (e.g. riden) and poetry build metadata.
# build-essential provides gcc for compiling native extensions (cffi, msgpack, etc.)
# on platforms where pre-built wheels are not available (e.g. arm/v7 on Python 3.14).
RUN apt-get update && apt-get install -y --no-install-recommends \
    git build-essential && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Copy dependency files first to leverage Docker layer caching.
# README.md is required by pyproject.toml for the build.
COPY pyproject.toml poetry.lock README.md ./
COPY meshtastic/ meshtastic/

# Build the wheel with Poetry, then install to a relocatable prefix.
# --find-links prefers the locally built wheel; deps are fetched from PyPI.
# The container is immutable once built, providing reproducible deployments.
RUN pip install --no-cache-dir poetry==2.4.1 && \
    poetry build --format wheel --no-interaction && \
    pip install --no-cache-dir --prefix=/install \
    --find-links=./dist "mtjk[cli,tunnel,analysis]" && \
    pip install --no-cache-dir --prefix=/install \
    riden@git+https://github.com/geeksville/riden.git@1.2.1 \
    ppk2-api parse pyarrow platformdirs

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
