#!/usr/bin/env bash
# Build the wheel, install it alone in a fresh venv, and drive it the way a user would: the CLI
# makes the credentials, `serve` starts, an upload lands over /api and opens over its link, and
# an MCP client lists the tools. This is what PyPI ships, so it is tested as shipped.
set -euo pipefail

work="$(mktemp -d)"
pid=""
cleanup() {
    if [ -n "$pid" ]; then
        kill "$pid" 2>/dev/null || true
        wait "$pid" 2>/dev/null || true
    fi
    rm -rf "$work"
}
trap cleanup EXIT

uv build --quiet --out-dir "$work/dist"
uv venv --quiet --python 3.15 "$work/venv"
# --no-sources and a fresh venv: nothing from this checkout is importable, only the wheel.
uv pip install --quiet --no-sources --python "$work/venv" "$work"/dist/*.whl
bin="$work/venv/bin"

expected="$(sed -n 's/^version = "\(.*\)"$/\1/p' pyproject.toml | head -n 1)"
actual="$("$bin/mcp-drop" --version)"
if [ "$actual" != "mcp-drop $expected" ]; then
    echo "wheel-test: --version says '$actual', pyproject.toml says $expected" >&2
    exit 1
fi

port="$("$bin/python" -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')"
base="http://127.0.0.1:$port"
secret="$("$bin/mcp-drop" secret new)"
issued="$("$bin/mcp-drop" token new wheel --scopes read,write 2>/dev/null)"
token="$(awk '/^token:/ {print $2}' <<<"$issued")"
entry="$(awk '/^entry:/ {print $2}' <<<"$issued")"

DROP_SECRET="$secret" DROP_TOKENS="$entry" DROP_DATA_DIR="$work/data" \
    DROP_PORT="$port" DROP_PUBLIC_BASE_URL="$base" \
    "$bin/mcp-drop" serve >"$work/serve.log" 2>&1 &
pid=$!

for _ in $(seq 1 100); do
    if curl --silent --fail "$base/healthz" >/dev/null; then break; fi
    sleep 0.1
done
curl --silent --fail "$base/healthz" >/dev/null || { cat "$work/serve.log" >&2; exit 1; }

printf '<h1>wheel</h1>' >"$work/index.html"
created="$(curl --silent --fail --header "Authorization: Bearer $token" \
    --form "title=Wheel test" --form "file=@$work/index.html;filename=index.html" \
    "$base/api/artifacts")"
url="$("$bin/python" -c 'import json, sys; print(json.load(sys.stdin)["url"])' <<<"$created")"
body="$(curl --silent --fail "$url")"
if [ "$body" != "<h1>wheel</h1>" ]; then
    echo "wheel-test: the private link served '$body'" >&2
    exit 1
fi

# The tools come from the installed wheel's own FastMCP, over streamable HTTP with the token.
DROP_URL="$base" DROP_TOKEN="$token" "$bin/python" - <<'PY'
import asyncio
import os

from fastmcp import Client
from fastmcp.client.auth import BearerAuth


async def main() -> None:
    async with Client(f"{os.environ['DROP_URL']}/mcp", auth=BearerAuth(os.environ["DROP_TOKEN"])) as client:
        names = {tool.name for tool in await client.list_tools()}
    # read+write: every tool except the share and publish ones.
    expected = {
        "artifact_create",
        "artifact_update",
        "artifact_get",
        "artifact_list",
        "artifact_upload_url",
        "artifact_delete",
    }
    if names != expected:
        raise SystemExit(f"wheel-test: MCP listed {sorted(names)}")


asyncio.run(main())
PY

echo "wheel-test: mcp-drop $expected installs from its wheel, serves, uploads and lists its tools"
