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

# Install the obscura headless browser (used by the agent's web tools).
# Staged under /tmp/obscura (NOT /install) so the bulk
# `COPY --from=builder /install /usr/local` in the runtime stage does not
# also drag it into /usr/local/home/... as a root-owned copy. The tool
# wrapper probes $HOME/.local/share/obscura/obscura, so the runtime stage
# COPY below drops the staged tree into /home/evonic/.local/share/obscura/.
#
# Tarball layout: `obscura` and `obscura-worker` ship at the archive root
# (no parent dir), so we extract with no --strip-components. The `scrape`
# subcommand needs obscura-worker beside obscura, hence both are kept.
ARG OBSCURA_VERSION=v0.1.11
# pass --build-arg OBSCURA_ARCH=aarch64 on ARM hosts
ARG OBSCURA_ARCH=x86_64
RUN set -eux; \
    mkdir -p /tmp/obscura; \
    curl -fsSL \
        "https://github.com/h4ckf0r0day/obscura/releases/download/${OBSCURA_VERSION}/obscura-${OBSCURA_ARCH}-linux.tar.gz" \
        | tar -xz -C /tmp/obscura; \
    chmod +x /tmp/obscura/obscura /tmp/obscura/obscura-worker; \
    /tmp/obscura/obscura --version

# Install kubectl client (pinned). Architectures: amd64 / arm64.
# Override with: --build-arg KUBECTL_VERSION=vX.Y.Z --build-arg KUBECTL_ARCH=arm64
ARG KUBECTL_VERSION=v1.31.0
ARG KUBECTL_ARCH=amd64
# Verify integrity: dl.k8s.io ships kubectl.sha256 as a bare hash (no filename),
# so reformat it into "<hash>  kubectl" before sha256sum -c.
RUN set -eux; \
    curl -fsSLo /tmp/kubectl \
        "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${KUBECTL_ARCH}/kubectl"; \
    curl -fsSLo /tmp/kubectl.sha256 \
        "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${KUBECTL_ARCH}/kubectl.sha256"; \
    printf '%s  /tmp/kubectl\n' "$(cat /tmp/kubectl.sha256)" | sha256sum -c -; \
    install -m 0755 /tmp/kubectl /usr/local/bin/kubectl; \
    rm /tmp/kubectl /tmp/kubectl.sha256; \
    kubectl version --client=true

# Install kubectl client (pinned). Architectures: amd64 / arm64.
# Override with: --build-arg KUBECTL_VERSION=vX.Y.Z --build-arg KUBECTL_ARCH=arm64
ARG KUBECTL_VERSION=v1.31.0
ARG KUBECTL_ARCH=amd64
# Verify integrity: dl.k8s.io ships kubectl.sha256 as a bare hash (no filename),
# so reformat it into "<hash>  kubectl" before sha256sum -c.
RUN set -eux; \
    curl -fsSLo /tmp/kubectl \
        "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${KUBECTL_ARCH}/kubectl"; \
    curl -fsSLo /tmp/kubectl.sha256 \
        "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${KUBECTL_ARCH}/kubectl.sha256"; \
    printf '%s  /tmp/kubectl\n' "$(cat /tmp/kubectl.sha256)" | sha256sum -c -; \
    install -m 0755 /tmp/kubectl /usr/local/bin/kubectl; \
    rm /tmp/kubectl /tmp/kubectl.sha256; \
    kubectl version --client=true

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
    openssh-client \
    curl \
    ca-certificates \
    # socat: SOCKS5 ProxyCommand for outbound SSH via the host's xray listener
    # (see playbooks/templates/evonic-ssh-config.j2)
    socat \
    # ffmpeg: merge video+audio (DASH), HLS, transcode for download workflows
    ffmpeg \
    # Misc
    jq \
    && rm -rf /var/lib/apt/lists/* \
    && ffmpeg -version >/dev/null

# Copy Python packages from builder
COPY --from=builder /install /usr/local

# Copy kubectl client from builder (root-owned, world-executable; lives on $PATH)
COPY --from=builder /usr/local/bin/kubectl /usr/local/bin/kubectl

# Copy kubectl client from builder (root-owned, world-executable; lives on $PATH)
COPY --from=builder /usr/local/bin/kubectl /usr/local/bin/kubectl

# Create non-root user
RUN groupadd -g 1000 evonic && \
    useradd -l -u 1000 -g evonic -m evonic

# Copy the staged obscura binary into the evonic user's home with correct
# ownership. The tool wrapper probes $HOME/.local/share/obscura/obscura.
COPY --from=builder --chown=evonic:evonic /tmp/obscura /home/evonic/.local/share/obscura/

# Symlink into /usr/local/bin so the skill's expected path resolves
RUN ln -s /home/evonic/.local/share/obscura/obscura /usr/local/bin/obscura

# Smoke test against the runtime image's glibc so a future base-image bump
# that drops glibc below 2.35 fails the build, not agent runtime.
RUN /home/evonic/.local/share/obscura/obscura --version

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
