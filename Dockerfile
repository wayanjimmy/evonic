# syntax=docker/dockerfile:1.6
# Multi-stage build for Evonic AI Platform
# Usage: podman build -t evonic:latest -f Dockerfile .

# =============================================================================
# Stage 1: Build dependencies
# =============================================================================
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Install build tools needed for native extensions (rapidfuzz, tiktoken, Pillow)
# Plus curl + ca-certificates to fetch the pdftotext binary below.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libsqlite3-dev \
    libjpeg-dev \
    zlib1g-dev \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Install Python deps into a prefix (will be copied to runtime image)
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# Install pdftotext (Cosmopolitan Libc build, same source as the host dotfile
# install in chezmoi). The binary lands in /install/bin/pdftotext and is copied
# to /usr/local/bin/pdftotext by the COPY --from=builder /install /usr/local
# in the runtime stage below.
#
# Note: cosmo.zip ships Xpdf's pdftotext (4.05), not poppler's. Xpdf exits
# with code 99 after printing version/help banners, so the smoke test accepts
# exit 0 (success) OR 99 (info-printed) and fails on anything else.
RUN mkdir -p /install/bin \
    && curl -fsSL https://cosmo.zip/pub/cosmos/bin/pdftotext -o /install/bin/pdftotext \
    && chmod 0755 /install/bin/pdftotext \
    && /install/bin/pdftotext -v || [ $? -eq 99 ]

# =============================================================================
# Stage 2: Runtime image
# =============================================================================
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

# Runtime system deps only (no compilers)
RUN apt-get update && apt-get install -y --no-install-recommends \
    # SQLite + libs
    sqlite3 libsqlite3-0 \
    # Pillow runtime libs
    libjpeg62-turbo zlib1g \
    # Tools Evonic shells out to
    ripgrep \
    git \
    curl \
    ca-certificates \
    # Misc
    jq \
    && rm -rf /var/lib/apt/lists/*

# Copy Python packages from builder
COPY --from=builder /install /usr/local

# Create non-root user
RUN groupadd -g 1000 evonic && \
    useradd -l -u 1000 -g evonic -m evonic

WORKDIR /app

# Copy application code (respects .dockerignore)
COPY --chown=evonic:evonic . .

# Ensure mutable directories exist (will be overridden by volume mounts)
RUN mkdir -p /app/shared/db /app/shared/agents /app/shared/avatars \
             /app/data/db /app/logs \
    && chown -R evonic:evonic /app

# Default port (can be overridden by env at runtime)
EXPOSE 8091

USER evonic

ENTRYPOINT ["python", "app.py"]
