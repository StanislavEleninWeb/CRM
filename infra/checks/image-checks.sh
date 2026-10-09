#!/bin/sh
# Checks on built images before they are published.
#   ./infra/checks/image-checks.sh <api-image> <frontend-image>
set -eu

API="${1:?api image}"
WEB="${2:?frontend image}"
fail() { echo "FAIL: $1" >&2; exit 1; }

[ "$(docker run --rm --entrypoint id "$API" -u)" != "0" ] || fail "the API image runs as root"
[ "$(docker run --rm --entrypoint id "$WEB" -u)" != "0" ] || fail "the frontend image runs as root"

# Nothing secret is baked into image configuration.
for image in "$API" "$WEB"; do
  docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$image" \
    | grep -Ei '(SECRET|PASSWORD|TOKEN|API_KEY|PRIVATE_KEY)=.+' && fail "$image has a secret-looking environment variable" || true
done

# No env files, keys, tests or development tooling in the API image.
docker run --rm --entrypoint sh "$API" -c '
  found="$(find /app -name ".env*" -o -name "*.pem" -o -name "*.key" -o -name "tests" -o -name ".git" 2>/dev/null | head -5)"
  [ -z "$found" ] || { echo "$found"; exit 1; }
  ! python -c "import pytest" 2>/dev/null
' || fail "the API image contains files or packages that do not belong in production"

# The built frontend is public: it must contain no server-side setting or key material.
docker run --rm --entrypoint sh "$WEB" -c '
  cd /usr/share/nginx/html
  ! grep -rIlE "SESSION_SECRET|SECRET_ENCRYPTION_KEYS|OIDC_CLIENT_SECRET|STRIPE_SECRET|GMAIL_CLIENT_SECRET|DATABASE_URL|postgresql\+psycopg|sk_(live|test)_|whsec_|BEGIN (RSA |EC )?PRIVATE KEY|dev-password" . \
    && ! ls -a | grep -E "^\.env" \
    && ! find . -name "*.map" | grep -q .
' || fail "the frontend bundle contains server-side configuration, key material or source maps"
echo "Image checks passed"
