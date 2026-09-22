# Dockerfile - SupertrendMoose production image
#
# Two stages:
#   builder  installs the Python packages into a virtual environment
#   runtime  a slim image with only that environment, the app and tzdata
#
# Hardening:
#   - base image pinned to a Python minor version and a Debian release, so a
#     rebuild never jumps to a different OS underneath you
#   - OS packages upgraded at build time for current security fixes
#   - no pip, compilers, curl or package caches in the final image
#   - runs as an unprivileged user that cannot modify the application code
#   - docker-compose.yml adds a read-only filesystem, no capabilities and
#     no-new-privileges on top
#
# To pin the base image exactly, add its digest to BASE_IMAGE, e.g.
#   python:3.12-slim-trixie@sha256:<digest>
# (docker buildx imagetools inspect python:3.12-slim-trixie shows it), and
# update it when you want the newer patch release.
ARG BASE_IMAGE=python:3.12-slim-trixie

# ─────────────────────────────────────────────────────────────────────────
# Stage 1: builder
# ─────────────────────────────────────────────────────────────────────────
FROM ${BASE_IMAGE} AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
COPY requirements.txt .

# Every dependency ships as a wheel, so no compiler is needed. pip is removed
# from the environment once it has done its job.
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt \
 && /opt/venv/bin/pip uninstall -y pip \
 && find /opt/venv -name '__pycache__' -prune -exec rm -rf {} +

# ─────────────────────────────────────────────────────────────────────────
# Stage 2: runtime
# ─────────────────────────────────────────────────────────────────────────
FROM ${BASE_IMAGE} AS runtime

LABEL org.opencontainers.image.title="SupertrendMoose" \
      org.opencontainers.image.description="Self-hosted Supertrend swing-trading scanner for US equities" \
      org.opencontainers.image.authors="theblackmoose" \
      org.opencontainers.image.source="https://github.com/theblackmoose/supertrendMoose"

# HOME points at the tmpfs because the root filesystem is read-only.
# yfinance caches Yahoo's session cookie and crumb there; without a writable
# cache every request goes out uncredentialed and Yahoo returns HTML, which
# yfinance reports as "possibly delisted".
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp \
    YF_CACHE_DIR=/tmp/yf-cache \
    PATH="/opt/venv/bin:$PATH"

# Security fixes for the base OS, plus tzdata for the scan schedule. The
# base image's own pip is removed too: nothing in here installs packages.
# tzdata is deliberately not version-pinned: time zone rules must stay current,
# and pinned apt versions stop building once Debian drops them.
# hadolint ignore=DL3008
RUN apt-get update \
 && apt-get upgrade -y \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && (python -m pip uninstall -y pip || true)

# The app user, supertrendmoose, uid/gid 10001. Everything else refers to it
# by number (the ntfy bootstrap hands secrets to uid 10001, and existing
# /data volumes are owned by it), so the name can change without effect.
# No home directory and no login shell.
RUN groupadd --system --gid 10001 supertrendmoose \
 && useradd --system --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin supertrendmoose \
 && mkdir -p /data \
 && chown 10001:10001 /data

WORKDIR /app

# Code stays owned by root and is only readable by the app user. The app
# never writes here, and if it were ever compromised it could not rewrite
# itself or plant code that runs on the next start, even without the
# read-only filesystem from docker-compose.yml. Only /data is the app's own.
COPY --from=builder /opt/venv /opt/venv
COPY app/ ./app/
COPY static/ ./static/

# Startup script. The sed keeps it working when the repository was checked
# out on Windows with CRLF line endings, which would otherwise fail with a
# confusing "no such file or directory".
COPY entrypoint.sh /app/entrypoint.sh
RUN sed -i 's/\r$//' /app/entrypoint.sh \
 && chmod 755 /app/entrypoint.sh \
 && chmod -R a+rX,go-w /app /opt/venv

# Numeric, so tools that check for a non-root user can verify it.
USER 10001:10001

VOLUME ["/data"]
EXPOSE 8000

# Python rather than curl, so the image carries no download tool.
HEALTHCHECK --interval=60s --timeout=5s --start-period=40s --retries=3 \
  CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"]

# entrypoint.sh checks the data volume, prints the dashboard address, then
# starts uvicorn with a single worker (the scan scheduler lives in-process).
ENTRYPOINT ["/app/entrypoint.sh"]
