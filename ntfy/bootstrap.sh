#!/bin/sh
# Generates its own credentials on first start, creates the ntfy accounts,
# locks down the topic, then hands off to the server.
#
# Ordering matters twice over:
#   1. `ntfy user add` fails against an auth database that has never been
#      created, and the server is what creates it. So on a cold start we run
#      the server just long enough to build the database, then stop it.
#   2. Accounts and ACLs are applied with the server DOWN. Writing them
#      underneath a running server leaves it serving stale permissions, which
#      shows up as "user not authorized" in the web UI.
#
# Every failure path prints why. A container that dies silently in a restart
# loop is the worst thing this script could do.

SECRETS=/secrets
APP_UID=10001
HEALTH_URL="http://127.0.0.1:80/v1/health"
STARTUP_TIMEOUT=90

export NTFY_AUTH_FILE="${NTFY_AUTH_FILE:-/var/lib/ntfy/user.db}"
NTFY_TOPIC="${NTFY_TOPIC:-SupertrendMoose}"

# --- public address (begin) ---
# The address your phone subscribes to. NTFY_PUBLIC_URL in .env wins;
# otherwise it is built from HOST_IP and NTFY_PORT. 0.0.0.0 is not an address
# a phone can use, so that case falls back to localhost with a note.
if [ -z "${NTFY_BASE_URL:-}" ]; then
  case "${HOST_IP:-}" in
    ""|127.0.0.1|localhost) NTFY_HOST=localhost ;;
    0.0.0.0|*:*)            NTFY_HOST=localhost; NTFY_HOST_NOTE=1 ;;
    *)                      NTFY_HOST="$HOST_IP" ;;
  esac
  NTFY_BASE_URL="http://$NTFY_HOST:${NTFY_PORT:-19081}"
fi
export NTFY_BASE_URL
# --- public address (end) ---
# Kept so `moose-creds`, which runs as a separate process, can show it.
echo "$NTFY_BASE_URL" > /var/lib/ntfy/public-url 2>/dev/null || true

die() {
  echo ""
  echo "  SupertrendMoose bootstrap FAILED: $1"
  echo ""
  [ -n "$INIT_PID" ] && kill -KILL "$INIT_PID" 2>/dev/null
  exit 1
}

echo "SupertrendMoose: checking credentials (running as uid $(id -u))"

mkdir -p "$SECRETS" 2>/dev/null
if ! touch "$SECRETS/.write-test" 2>/dev/null; then
  die "cannot write to $SECRETS as uid $(id -u).
    The moose-secrets volume is root-owned, so this image must run as root.
    Check that ntfy/Dockerfile contains 'USER root', then:
      docker compose build --no-cache ntfyMoose && docker compose up -d"
fi
rm -f "$SECRETS/.write-test"

command -v base64 >/dev/null 2>&1 || die "base64 is missing from this image"

FIRST_RUN=0

# $1 = filename, $2 = owning uid (the app needs to read two of these)
generate() {
  file="$SECRETS/$1"
  if [ ! -s "$file" ]; then
    head -c 24 /dev/urandom | base64 | tr -d '\n' > "$file" \
      || die "could not write $file"
    [ -s "$file" ] || die "$file was written but is empty"
    FIRST_RUN=1
    echo "  generated $1"
  fi
  if chown "$2" "$file" 2>/dev/null; then
    # chmod on a file now owned by the app user needs CAP_FOWNER. Without it
    # the file keeps mode 644, readable by anyone in the volume, so say so.
    chmod 600 "$file" 2>/dev/null \
      || echo "  WARNING: could not make $1 private (mode 600). Check cap_add in docker-compose.yml"
  else
    echo "  note: chown unavailable for $1, falling back to mode 644"
    chmod 644 "$file"
  fi
}

generate ntfy-moose.pass "$APP_UID"   # app publishes with this
generate api-token       "$APP_UID"   # app's dashboard/API token
generate ntfy-notify.pass 0           # you log in with this

MOOSE_PASS=$(cat "$SECRETS/ntfy-moose.pass") || die "could not read ntfy-moose.pass"
READER_PASS=$(cat "$SECRETS/ntfy-notify.pass") || die "could not read ntfy-notify.pass"

# ── create the auth database on a cold start, then stop the server ────
if [ ! -s "$NTFY_AUTH_FILE" ]; then
  echo "  cold start: building the auth database"
  ntfy serve >/dev/null 2>&1 &
  INIT_PID=$!

  waited=0
  while [ "$waited" -lt "$STARTUP_TIMEOUT" ]; do
    wget -q -O - "$HEALTH_URL" 2>/dev/null | grep -q true && break
    kill -0 "$INIT_PID" 2>/dev/null \
      || die "ntfy exited while building the auth database.
    Run 'docker compose logs ntfyMoose' for its output."
    waited=$((waited + 1))
    sleep 1
  done
  [ "$waited" -lt "$STARTUP_TIMEOUT" ] \
    || die "ntfy did not start within ${STARTUP_TIMEOUT}s"

  kill -TERM "$INIT_PID" 2>/dev/null
  wait "$INIT_PID" 2>/dev/null
  INIT_PID=""
  sleep 1
  [ -f "$NTFY_AUTH_FILE" ] || die "no auth database was created at $NTFY_AUTH_FILE"
  echo "  auth database ready after ${waited}s, server stopped"
fi

# ── accounts and ACLs, applied with the server down ───────────────────
# Passwords are set ONCE, at creation. Running `ntfy user change-pass` on an
# existing account invalidates that user's access tokens, which silently logs
# your browser and phone out on every restart and surfaces as
# "user not authorized" even though the ACL is correct.
# Set MOOSE_RESET_PASSWORDS=1 to force a re-sync (you will need to log in again).
# The ntfy CLI writes this listing to stderr, so 2>&1 is required - dropping
# stderr makes every user look absent.
user_exists() {
  ntfy user list 2>&1 | grep -q "^user $1 "
}

ensure_user() {
  name="$1"; pass="$2"
  if user_exists "$name"; then
    if [ "${MOOSE_RESET_PASSWORDS:-0}" = "1" ]; then
      NTFY_PASSWORD="$pass" ntfy user change-pass "$name" >/dev/null 2>&1 \
        || die "could not reset the '$name' password"
      echo "  user $name password re-synced (existing logins are now invalid)"
    else
      echo "  user $name already exists, left untouched"
    fi
  elif NTFY_PASSWORD="$pass" ntfy user add --role=user "$name" >/dev/null 2>&1; then
    echo "  created user $name"
  elif user_exists "$name"; then
    # add can fail purely because the account is already there; that is fine.
    echo "  user $name already exists, left untouched"
  else
    die "could not create the '$name' account, and it does not exist.
    ntfy user list said:
$(ntfy user list 2>&1 | sed 's/^/      /')
    For a clean slate:  docker compose down -v && docker compose up -d --build"
  fi
}

ensure_user moose "$MOOSE_PASS"
ensure_user notifications "$READER_PASS"

# The reader account used to be called 'phone'. ntfy has no rename, so the new
# account is created above and the old one removed here. Harmless if absent.
if user_exists phone; then
  ntfy user del phone >/dev/null 2>&1 \
    && echo "  removed the old 'phone' account" \
    || echo "  note: could not remove the old 'phone' account"
fi

# Publisher may write only; reader may read only. Everyone else is denied
# by NTFY_AUTH_DEFAULT_ACCESS=deny-all.
ntfy access moose "$NTFY_TOPIC" write-only >/dev/null 2>&1 \
  || die "could not grant write access to 'moose'"
ntfy access notifications "$NTFY_TOPIC" read-only >/dev/null 2>&1 \
  || die "could not grant read access to 'notifications'"

# Print what was actually stored, so a permissions problem shows up here
# rather than only as "not authorized" in the web UI.
echo "  access control in effect:"
ntfy access 2>&1 | sed 's/^/    /'

if [ "$FIRST_RUN" = "1" ]; then
  cat <<BANNER

  ─────────────────────────────────────────────────────────
   SupertrendMoose credentials generated

   ntfy topic       $NTFY_TOPIC
   ntfy username    notifications
   ntfy password    $READER_PASS

   dashboard token  $(cat "$SECRETS/api-token")

   Shown once. To print them again:
     docker compose exec ntfyMoose moose-creds
  ─────────────────────────────────────────────────────────

BANNER
fi

echo "  phone server address: $NTFY_BASE_URL"
if [ -n "${NTFY_HOST_NOTE:-}" ]; then
  echo "  note: HOST_IP is ${HOST_IP}, which a phone cannot connect to."
  echo "        Set NTFY_PUBLIC_URL in .env to the address your phone should use."
fi
echo "SupertrendMoose: starting ntfy"
exec ntfy serve
