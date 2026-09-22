#!/bin/sh
#
# entrypoint.sh - Startup script for the SupertrendMoose container
#
# Responsibilities:
#   1. Check the data volume is writable (the rest of the filesystem is not)
#   2. Check TZ is a real time zone name
#   3. Work out the address the dashboard is reached on, from HOST_IP
#   4. Warn about settings that would quietly do the wrong thing
#   5. Print a short summary, then hand over to the web server
#
# The first scan, the daily schedule and the earnings refresh all start inside
# the app itself, so there is nothing to pre-load here.
#
# One worker only: the scan scheduler runs inside the web process, and more
# workers would mean more scans and duplicate alerts.
#
# Every failure prints why. A container that exits silently into a restart
# loop is the worst thing this script could do.

DATA_DIR="${MOOSE_DATA_DIR:-/data}"
SECRETS_DIR="${SECRETS_DIR:-/secrets}"
APP_PORT="${APP_PORT:-19080}"
LISTEN_PORT="${MOOSE_LISTEN_PORT:-8000}"

die() {
  echo ""
  echo "  SupertrendMoose failed to start: $1"
  echo ""
  exit 1
}

echo "SupertrendMoose: starting (uid $(id -u))"

# ── 1. writable data volume ───────────────────────────────────────────
if ! mkdir -p "$DATA_DIR" 2>/dev/null || ! touch "$DATA_DIR/.write-test" 2>/dev/null; then
  die "cannot write to $DATA_DIR as uid $(id -u).
    The database lives there, and the rest of the container is read-only.
    Check that docker-compose.yml still mounts the moose-data volume at /data."
fi
rm -f "$DATA_DIR/.write-test"

mkdir -p "${YF_CACHE_DIR:-/tmp/yf-cache}" 2>/dev/null \
  || echo "  note: could not create ${YF_CACHE_DIR:-/tmp/yf-cache}; Yahoo requests will be slower"

# ── 2. a real time zone ───────────────────────────────────────────────
# TZ must be an IANA name such as Australia/Melbourne. A typo would otherwise
# leave logs and the weekly earnings refresh on UTC without saying so.
ZONEINFO="${MOOSE_ZONEINFO_DIR:-/usr/share/zoneinfo}"
TZ="$(printf '%s' "${TZ:-}" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
export TZ
if [ -n "$TZ" ]; then
  case "$TZ" in
    /*|*..*) BAD_TZ=1 ;;
    *)       [ -f "$ZONEINFO/$TZ" ] && BAD_TZ="" || BAD_TZ=1 ;;
  esac
  if [ -n "$BAD_TZ" ]; then
    die "TZ=$TZ is not a time zone name.
    Use the name for your area, for example:
      Australia/Melbourne  Australia/Perth  Pacific/Auckland  Asia/Singapore
      Europe/London  Europe/Berlin  America/New_York  America/Chicago
      America/Los_Angeles  UTC
    The full list: https://en.wikipedia.org/wiki/List_of_tz_database_time_zones
    Set it in .env, then: docker compose up -d"
  fi
fi

# ── 3. which address this is reached on ───────────────────────────────
# HOST_IP is the host address from .env. 0.0.0.0 means "every interface",
# which is not an address anyone can browse to, so links fall back to
# localhost and a note explains what to set.
case "${HOST_IP:-}" in
  ""|127.0.0.1|localhost) PUBLIC_HOST="localhost" ;;
  0.0.0.0)                PUBLIC_HOST="" ;;
  *:*)                    PUBLIC_HOST=""
                          echo "  note: HOST_IP looks like IPv6 ($HOST_IP). Only IPv4 addresses are supported." ;;
  *)                      PUBLIC_HOST="$HOST_IP" ;;
esac

# Alert links. An explicit BASE_URL always wins. Links are only added when
# they would work from another device, so loopback-only installs get none.
if [ -z "${BASE_URL:-}" ] && [ -n "$PUBLIC_HOST" ] && [ "$PUBLIC_HOST" != "localhost" ]; then
  BASE_URL="http://$PUBLIC_HOST:$APP_PORT"
  export BASE_URL
fi

if [ -n "$PUBLIC_HOST" ]; then
  DASHBOARD="http://$PUBLIC_HOST:$APP_PORT"
else
  DASHBOARD="http://<this machine's IP>:$APP_PORT"
fi

# ── 4. settings worth a warning ───────────────────────────────────────
if [ -z "${AUTH_TOKEN:-}" ] && [ ! -s "$SECRETS_DIR/api-token" ]; then
  echo "  WARNING: no dashboard token found, so the dashboard is open to anyone"
  echo "           who can reach it. The ntfyMoose container normally creates one."
fi

# ── 5. summary and hand-over ──────────────────────────────────────────
echo "  dashboard   $DASHBOARD"
echo "  timezone    ${TZ:-UTC}"
echo "  data        $DATA_DIR"
[ -n "${BASE_URL:-}" ] && echo "  alert links ${BASE_URL}"
if [ "${HOST_IP:-}" = "0.0.0.0" ] && [ -z "${BASE_URL:-}" ]; then
  echo "  note: listening on every interface. Set BASE_URL in .env for links in alerts."
fi

# Anything passed after the image name runs instead of the server, e.g.
#   docker compose run --rm supertrendMoose sh
if [ "$#" -gt 0 ]; then
  exec "$@"
fi

exec uvicorn app.main:app --host 0.0.0.0 --port "$LISTEN_PORT" --workers 1
