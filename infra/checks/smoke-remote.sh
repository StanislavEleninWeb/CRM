#!/bin/sh
# What must be true of a deployed environment, checked from outside. No credentials are used.
set -eu

if [ -z "${BASE_URL:-}" ]; then
  echo "::notice::No PUBLIC_BASE_URL is configured for this environment. Nothing was checked."
  exit 0
fi
fail() { echo "FAIL: $1" >&2; exit 1; }

curl --fail --silent --show-error --max-time 15 "$BASE_URL/readyz" | grep -q '"status":"ready"' || fail "the API is not ready"
curl --fail --silent --show-error --max-time 15 "$BASE_URL/" | grep -q '<div id="root">' || fail "the frontend is not served"
curl --fail --silent --show-error --max-time 15 "$BASE_URL/api/v1/system/info" | grep -q '"api_version"' || fail "the API is not reachable through the proxy"
[ "$(curl --silent --output /dev/null --write-out '%{http_code}' --max-time 15 "$BASE_URL/api/v1/companies")" = "401" ] || fail "an unauthenticated request was not refused"
[ "$(curl --silent --output /dev/null --write-out '%{http_code}' --max-time 15 "$BASE_URL/ops/metrics")" = "404" ] || fail "the monitoring endpoint is reachable from outside"
[ "$(curl --silent --output /dev/null --write-out '%{http_code}' --max-time 15 "$BASE_URL/api/v1/docs")" = "404" ] || fail "interactive API documentation is exposed"
case "$BASE_URL" in https://*) curl --silent --head --max-time 15 "$BASE_URL/" | grep -qi '^strict-transport-security:' || fail "HSTS header missing" ;; esac
echo "Smoke test passed for $BASE_URL"
