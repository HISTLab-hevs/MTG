#!/usr/bin/env bash
# One-command setup: configures .env, picks free ports (80/443 preferred),
# builds and starts the full stack, and optionally creates the admin user.
#
#   ./setup.sh                          # interactive
#   ./setup.sh --admin-email a@b.c --admin-password secret   # non-interactive
#
# The app is served over HTTPS only; plain HTTP is redirected. Caddy generates
# a local CA; client trust and public domains are covered in installation.md.

set -euo pipefail
cd "$(dirname "$0")"

ADMIN_EMAIL="" ADMIN_PASSWORD=""
while [ $# -gt 0 ]; do
  case "$1" in
    --admin-email) ADMIN_EMAIL="$2"; shift 2 ;;
    --admin-password) ADMIN_PASSWORD="$2"; shift 2 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

if ! docker compose version >/dev/null 2>&1; then
  echo "Error: docker with the compose plugin is required." >&2
  exit 1
fi

port_is_free() {
  ! (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null
}

pick_port() {
  local candidate
  for candidate in "$@"; do
    if port_is_free "$candidate"; then echo "$candidate"; return; fi
  done
  echo "Error: none of the candidate ports ($*) are free." >&2
  exit 1
}

HTTP_PORT=$(pick_port 80 8089 8090 8091)
HTTPS_PORT=$(pick_port 443 8444 8445 8446)
echo "Using HTTP port $HTTP_PORT and HTTPS port $HTTPS_PORT."

# Create .env on first run with a real random secret.
if [ ! -f .env ]; then
  cp .env.example .env
  secret=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')
  sed -i "s|^APP_SECRET_KEY=.*|APP_SECRET_KEY=${secret}|" .env
  echo "Created .env (random APP_SECRET_KEY generated)."
  echo "NOTE: set all three model endpoints in .env, then re-run ./setup.sh:"
  echo "      EXTRACTION_BASE_URL / EXTRACTION_MODEL   (VLM, page extraction)"
  echo "      GENERATION_BASE_URL / GENERATION_MODEL   (rules, reports, chats)"
  echo "      EMBEDDING_BASE_URL  / EMBEDDING_MODEL    (page indexing, materials chat)"
  echo "      Without them, uploads are stored but processing fails."
fi

# Record source mode and the chosen ports for subsequent compose commands.
export MTG_IMAGE=mtg:local
for var in MTG_IMAGE HTTP_PORT HTTPS_PORT; do
  value=$(eval echo "\$$var")
  if grep -q "^${var}=" .env; then
    sed -i "s|^${var}=.*|${var}=${value}|" .env
  else
    printf '\n%s=%s\n' "$var" "$value" >> .env
  fi
done

# Build once for all four application services. Explicitly select the local
# image even if this .env previously belonged to a published-image install.
docker compose build web
docker compose up -d --no-build --pull missing

echo -n "Waiting for the app to come up"
# -k accepts Caddy's internal-CA certificate. Errors are silenced (no -S) because
# the first attempts are expected to fail while Caddy issues the certificate and
# web finishes migrating; only the outcome below is worth reporting.
ready=false
for _ in $(seq 1 90); do
  if curl -fsk -o /dev/null "https://localhost:${HTTPS_PORT}/login" 2>/dev/null; then
    ready=true
    echo " — ready."
    break
  fi
  echo -n "."
  sleep 2
done
if [ "$ready" = false ]; then
  echo " — timed out."
  echo "The app did not answer on https://localhost:${HTTPS_PORT}. Check the logs:" >&2
  echo "  docker compose logs web caddy" >&2
  exit 1
fi

# Admin account: from flags, or interactively when running in a terminal.
if [ -z "$ADMIN_EMAIL" ] && [ -t 0 ]; then
  read -r -p "Create an admin user now? [y/N] " answer
  if [ "${answer,,}" = "y" ]; then
    read -r -p "Admin email: " ADMIN_EMAIL
    read -r -s -p "Admin password: " ADMIN_PASSWORD; echo
  fi
fi
if [ -n "$ADMIN_EMAIL" ]; then
  docker compose exec web python -m app.cli create-admin \
    --email "$ADMIN_EMAIL" --password "$ADMIN_PASSWORD" \
    --first-name Admin --last-name User || true
fi

echo
echo "MTG is running (HTTPS only):"
echo "  https://localhost:${HTTPS_PORT}"
echo "  Trust Caddy's local CA, or configure a public domain: docs/installation.md#5-transport-security"
echo "  http://localhost:${HTTP_PORT} redirects here."
echo
echo "Create more admins anytime with:"
echo "  docker compose exec web python -m app.cli create-admin --email ... --password ... --first-name ... --last-name ..."
