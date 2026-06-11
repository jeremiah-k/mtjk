# SPDX-License-Identifier: GPL-3.0-or-later
#
# Copyright (C) 2025 Olliver Schinagl <oliver@schinagl.nl>
# Copyright (C) 2025 Jeremiah K. <jeremiahk@gmx.com>

# Build stage
FROM docker.io/library/python:3.14-slim-bookworm AS builder

# git is required for pip VCS installs (e.g. riden) and poetry build metadata.
# build-essential and libffi-dev provide gcc and ffi.h for compiling native
# extensions (cffi, msgpack, rapidfuzz) from source when needed.
RUN apt-get update && apt-get install -y --no-install-recommends \
    git build-essential libffi-dev && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Install poetry and the export plugin in an isolated venv so their
# dependencies (packaging, requests, etc.) do not pollute the system Python.
# Without isolation, pip's --prefix=/install skips shared deps it considers
# "already installed", causing missing modules in the runtime image.
RUN python -m venv /opt/poetry && \
    /opt/poetry/bin/pip install --no-cache-dir poetry==2.4.1 poetry-plugin-export

# --- Layer 1: Dependency resolution and install (cached unless lock changes) ---
COPY pyproject.toml poetry.lock README.md ./

# Export all pinned deps (extras + powermon group) to requirements.txt, then
# install them to the relocatable prefix.  This layer is only rebuilt when
# pyproject.toml or poetry.lock changes — source edits do NOT invalidate it.
RUN --mount=type=cache,target=/root/.cache/pip \
    /opt/poetry/bin/poetry export \
    --format requirements.txt \
    --extras cli --extras tunnel --extras analysis \
    --with powermon \
    --without dev \
    --without-hashes \
    --output requirements.txt && \
    pip install --no-cache-dir --prefix=/install -r requirements.txt

# --- Layer 2: Source + wheel build (rebuilt on every source change) ---
COPY meshtastic/ meshtastic/

# Build the wheel and install it on top of the already-installed deps.
# --no-deps avoids re-resolving; all transitive deps are in Layer 1.
RUN /opt/poetry/bin/poetry build --format wheel --no-interaction && \
    pip install --no-cache-dir --no-deps --prefix=/install \
    ./dist/*.whl

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
