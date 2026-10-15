#!/usr/bin/env bash
# Smoke-test an image the way it is deployed: read-only root, no capabilities, non-root, data on
# a volume. Usage: docker/smoke.sh <image>. Exits non-zero on the first failed check.
set -euo pipefail

image="$1"
port="${SMOKE_PORT:-18080}"
name="drop-smoke-$$"
volume="drop-smoke-$$"
host="localhost:8080"

cleanup() {
    docker rm -f "$name" >/dev/null 2>&1 || true
    docker volume rm "$volume" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# The image's own CLI makes the credentials, so the commands are smoke-tested too.
secret="$(docker run --rm "$image" secret new)"
entry="$(docker run --rm "$image" token new smoke --scopes read 2>/dev/null | awk '/^entry:/ {print $2}')"

docker run --detach --name "$name" \
    --read-only --cap-drop ALL --security-opt no-new-privileges \
    --volume "$volume:/data" \
    --env DROP_SECRET="$secret" --env DROP_TOKENS="$entry" \
    --publish "127.0.0.1:$port:8080" \
    "$image" >/dev/null

for _ in $(seq 1 60); do
    if curl --silent --fail --header "Host: $host" "http://127.0.0.1:$port/healthz" >/dev/null; then
        break
    fi
    sleep 0.5
done

fail() { echo "smoke: $*" >&2; docker logs "$name" >&2 || true; exit 1; }

body="$(curl --silent --fail --header "Host: $host" "http://127.0.0.1:$port/healthz")" \
    || fail "/healthz did not answer"
[ "$body" = '{"ok":true}' ] || fail "/healthz answered $body"
[ "$(docker exec "$name" id -u)" = "10001" ] || fail "the server does not run as uid 10001"
docker exec "$name" python /app/healthcheck.py || fail "the HEALTHCHECK command fails"
if docker exec "$name" touch /app/written 2>/dev/null; then fail "the root filesystem is writable"; fi
echo "smoke: $image runs read-only, as uid 10001, and passes /healthz"
