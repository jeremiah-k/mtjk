# SPDX-License-Identifier: GPL-3.0-or-later
#
# Copyright (C) 2025 Olliver Schinagl <oliver@schinagl.nl>
# Copyright (C) 2025 Jeremiah K. <jeremiahk@gmx.com>

# Build stage
FROM docker.io/library/python:3.14-slim-bookworm AS builder

# git is required for the immutable riden VCS dependency.
# build-essential and libffi-dev provide gcc and ffi.h for compiling native
# extensions (cffi, msgpack, rapidfuzz) from source when needed.
RUN apt-get update && apt-get install -y --no-install-recommends \
    git build-essential libffi-dev && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Install uv in an isolated tool environment.
RUN python -m venv /opt/uv && \
    /opt/uv/bin/pip install --no-cache-dir uv==0.12.23

# --- Layer 1: Locked dependencies (cached unless project metadata changes) ---
COPY pyproject.toml uv.lock README.md LICENSE.md ./

# Export registry dependencies with their lockfile hashes. The immutable riden
# Git dependency cannot participate in pip's hash-checking mode, so derive its
# exact locked VCS requirement separately and install it only after the hashed
# registry set succeeds.
RUN --mount=type=cache,target=/root/.cache/uv \
    /opt/uv/bin/uv export --locked --all-extras --group powermon --no-dev \
    --no-emit-project --no-emit-package riden --format requirements-txt \
    --output-file requirements-registry.txt && \
    /opt/uv/bin/uv export --locked --all-extras --group powermon --no-dev \
    --no-hashes --no-emit-project --format requirements-txt \
    --output-file requirements-all.txt && \
    grep -xE 'riden @ git\+https://github\.com/geeksville/riden\.git@[0-9a-f]{40}' \
    requirements-all.txt > requirements-riden.txt && \
    test "$(wc -l < requirements-riden.txt)" -eq 1 && \
    pip install --no-cache-dir --no-deps --require-hashes --prefix=/install \
    -r requirements-registry.txt && \
    pip install --no-cache-dir --no-deps --prefix=/install \
    -r requirements-riden.txt

# --- Layer 2: Source + wheel build (rebuilt on every source change) ---
COPY meshtastic/ meshtastic/

# Install the wheel over dependencies already installed in the first layer.
RUN /opt/uv/bin/uv build --wheel && \
    pip install --no-cache-dir --no-deps --prefix=/install ./dist/*.whl

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
