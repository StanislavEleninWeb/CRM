#!/bin/sh
# Smoke test for a running local stack: API readiness, frontend-to-API path, worker job.
set -eu

API_URL="${API_URL:-http://127.0.0.1:8000}"
WEB_URL="${WEB_URL:-http://127.0.0.1:5173}"

echo "1. API readiness"
curl --fail --silent --show-error "$API_URL/readyz"
echo

echo "2. Frontend serves the app and proxies the real API"
curl --fail --silent --show-error "$WEB_URL/" | grep -q '<div id="root">'
curl --fail --silent --show-error "$WEB_URL/api/v1/system/info"
echo

echo "3. Worker executes a synthetic job"
docker compose exec -T api python - <<'PY'
from app.worker.tasks import ping

result = ping.delay("smoke").get(timeout=20)
assert result["echo"] == "smoke", result
print("worker result:", result)
PY

echo "4. Scheduler has triggered due-row polling"
docker compose exec -T api python - <<'PY'
import time

import redis

from app.core.config import get_settings
from app.worker.tasks import LAST_POLL_KEY

client = redis.Redis.from_url(get_settings().redis_url)
deadline = time.time() + get_settings().due_poll_interval_seconds + 15
while time.time() < deadline:
    value = client.get(LAST_POLL_KEY)
    if value:
        print("last poll:", value.decode())
        break
    time.sleep(2)
else:
    raise SystemExit("scheduler has not polled")
PY

echo "Smoke test passed"
