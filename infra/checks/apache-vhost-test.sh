#!/bin/sh
# Runs the CRM's Apache virtual host in a throwaway Apache container, with stand-ins for the API and
# the pages and a made-up certificate authority in the role of Cloudflare, and checks what the
# virtual host is supposed to guarantee. Needs Docker. Uses ports 127.0.0.1:28080 and :28443.
set -eu

HERE="$(cd "$(dirname "$0")/../production" && pwd)"
WORK="$(mktemp -d)"
NAME="crm-apache-test-$$"
fail() { echo "FAIL: $1" >&2; exit 1; }
cleanup() { docker rm -f "$NAME" "$NAME-api" "$NAME-web" >/dev/null 2>&1 || true; rm -rf "${WORK:?}"; }
trap cleanup EXIT

mkdir -p "$WORK/edge" "$WORK/client"
# "Cloudflare": a certificate authority, and a client certificate signed by it.
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj "/CN=Test Origin Pull CA" -keyout "$WORK/client/ca.key" -out "$WORK/edge/cloudflare-origin-pull-ca.pem" >/dev/null 2>&1
openssl req -newkey rsa:2048 -nodes -subj "/CN=cloudflare-edge" -keyout "$WORK/client/client.key" -out "$WORK/client/client.csr" >/dev/null 2>&1
openssl x509 -req -days 1 -in "$WORK/client/client.csr" -CA "$WORK/edge/cloudflare-origin-pull-ca.pem" -CAkey "$WORK/client/ca.key" -CAcreateserial -out "$WORK/client/client.pem" >/dev/null 2>&1
# Someone else's client certificate, not signed by that authority.
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj "/CN=stranger" -keyout "$WORK/client/other.key" -out "$WORK/client/other.pem" >/dev/null 2>&1
# The origin certificate.
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj "/CN=crm.seweb.co" -keyout "$WORK/edge/origin.key" -out "$WORK/edge/origin.pem" >/dev/null 2>&1
chmod 644 "$WORK"/edge/*

cat > "$WORK/httpd.conf" <<'CONF'
ServerRoot "/usr/local/apache2"
ServerName localhost
Listen 80
Listen 443
LoadModule mpm_event_module modules/mod_mpm_event.so
LoadModule unixd_module modules/mod_unixd.so
LoadModule authz_core_module modules/mod_authz_core.so
LoadModule log_config_module modules/mod_log_config.so
LoadModule alias_module modules/mod_alias.so
LoadModule socache_shmcb_module modules/mod_socache_shmcb.so
LoadModule ssl_module modules/mod_ssl.so
LoadModule proxy_module modules/mod_proxy.so
LoadModule proxy_http_module modules/mod_proxy_http.so
LoadModule headers_module modules/mod_headers.so
LoadModule rewrite_module modules/mod_rewrite.so
User daemon
Group daemon
Define APACHE_LOG_DIR /tmp
ErrorLog /proc/self/fd/2
Include /crm/apache-crm.seweb.co.conf
CONF

cat > "$WORK/echo.py" <<'PY'
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer

class Echo(BaseHTTPRequestHandler):
    def _answer(self):
        if self.headers.get("Expect", "").lower() == "100-continue":
            self.send_response_only(100); self.end_headers()
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        body = json.dumps({"service": sys.argv[2], "path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)
    do_GET = do_POST = _answer
    def log_message(self, *args): pass

HTTPServer(("127.0.0.1", int(sys.argv[1])), Echo).serve_forever()
PY

docker run -d --name "$NAME" -p 127.0.0.1:28080:80 -p 127.0.0.1:28443:443 \
  -v "$WORK/httpd.conf:/usr/local/apache2/conf/httpd.conf:ro" -v "$HERE/apache-crm.seweb.co.conf:/crm/apache-crm.seweb.co.conf:ro" \
  -v "$WORK/edge:/etc/seweb-crm/edge:ro" httpd:2.4 >/dev/null
# The stand-ins share Apache's network, so 127.0.0.1:18000 and :18080 are where the virtual host expects them.
PY_IMAGE="${PY_IMAGE:-python:3.13-slim}"
docker run -d --name "$NAME-api" --network "container:$NAME" -v "$WORK/echo.py:/echo.py:ro" "$PY_IMAGE" python /echo.py 18000 api >/dev/null
docker run -d --name "$NAME-web" --network "container:$NAME" -v "$WORK/echo.py:/echo.py:ro" "$PY_IMAGE" python /echo.py 18080 pages >/dev/null
sleep 3
docker exec "$NAME" httpd -t >/dev/null 2>&1 || { docker logs "$NAME" 2>&1 | tail -5; fail "Apache does not accept the virtual host"; }
echo "ok  Apache accepts the virtual host"

URL="https://crm.seweb.co:28443"
AS_CF="--silent --max-time 10 --insecure --resolve crm.seweb.co:28443:127.0.0.1 --cert $WORK/client/client.pem --key $WORK/client/client.key"
NO_CERT="--silent --max-time 10 --insecure --resolve crm.seweb.co:28443:127.0.0.1"
code() { curl "$@" --output /dev/null --write-out '%{http_code}' || true; }

[ "$(code $NO_CERT "$URL/readyz")" = "000" ] || fail "a connection without Cloudflare's certificate was accepted"
[ "$(code $NO_CERT --cert "$WORK/client/other.pem" --key "$WORK/client/other.key" "$URL/readyz")" = "000" ] || fail "a connection with someone else's certificate was accepted"
echo "ok  only a client presenting the trusted certificate can connect"

api="$(curl $AS_CF -H 'CF-Connecting-IP: 203.0.113.7' -H 'X-Forwarded-For: 6.6.6.6' "$URL/api/v1/system/info?x=1")"
echo "$api" | grep -q '"service": "api"' || fail "/api/ did not reach the API"
echo "$api" | grep -q '"path": "/api/v1/system/info?x=1"' || fail "the path or query was changed on the way to the API"
echo "$api" | grep -q '"x-forwarded-for": "203.0.113.7"' || fail "the visitor's address is not the one Cloudflare reported (a forged header may have passed)"
echo "$api" | grep -q '"x-forwarded-proto": "https"' || fail "the API is not told the request was HTTPS"
# The test reaches Apache on a spare port, so the name arrives with that port attached.
echo "$api" | grep -q '"host": "crm.seweb.co:28443"' || fail "the host name was not preserved"
echo "ok  /api/ reaches the API with the path, host, scheme and the visitor's address from Cloudflare"

curl $AS_CF "$URL/readyz" | grep -q '"service": "api"' || fail "/readyz did not reach the API"
curl $AS_CF "$URL/healthz" | grep -q '"service": "api"' || fail "/healthz did not reach the API"
curl $AS_CF "$URL/" | grep -q '"service": "pages"' || fail "/ did not reach the pages"
curl $AS_CF "$URL/prospects/123" | grep -q '"service": "pages"' || fail "a page address did not reach the pages"
echo "ok  health addresses go to the API and everything else to the pages"

[ "$(code $AS_CF "$URL/ops/metrics")" = "404" ] || fail "/ops/metrics is reachable from outside"
[ "$(code $AS_CF "$URL/ops")" = "404" ] || fail "/ops is reachable from outside"
echo "ok  monitoring figures are not served"

headers="$(curl $AS_CF --head "$URL/")"
for header in "strict-transport-security: max-age=31536000" "x-content-type-options: nosniff" "x-frame-options: deny" "content-security-policy: default-src 'self'"; do
  echo "$headers" | tr 'A-Z' 'a-z' | grep -q "$header" || fail "missing header: $header"
done
echo "ok  security headers are set"

head -c 27000000 /dev/zero > "$WORK/big"
big_code="$(code $AS_CF -X POST --data-binary "@$WORK/big" "$URL/api/v1/imports")"; [ "$big_code" = "413" ] || fail "an upload over 25 MB was not refused (HTTP $big_code)"
head -c 1000000 /dev/zero > "$WORK/small"
[ "$(code $AS_CF -X POST --data-binary "@$WORK/small" "$URL/api/v1/imports")" = "200" ] || fail "an ordinary upload was refused"
echo "ok  uploads over 25 MB are refused, ordinary ones pass"

redirect="$(curl --silent --max-time 10 --output /dev/null --write-out '%{http_code} %{redirect_url}' -H 'Host: crm.seweb.co' http://127.0.0.1:28080/prospects)"
[ "$redirect" = "301 https://crm.seweb.co/prospects" ] || fail "port 80 does not redirect to HTTPS ($redirect)"
echo "ok  port 80 redirects to HTTPS"
echo "Apache virtual host test passed"
