"""Hermes-style client: raw mcp SDK ClientSession.initialize() (handshake era) + elicitation_callback."""
import asyncio, os, sys
import httpx2
import mcp.types as mt
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

BASE = "http://127.0.0.1:8765"
TOKEN = os.environ["T_TOKEN"]
ok_all = True


def check(name, cond, detail=""):
    global ok_all
    ok_all &= bool(cond)
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))


async def run(answer):
    calls = []

    async def on_elicit(ctx, params):
        calls.append(params.message)
        if answer is None:
            return mt.ElicitResult(action="decline")
        return mt.ElicitResult(action="accept", content={})  # exactly what Hermes sends on approve

    hc = httpx2.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"})
    async with streamable_http_client(f"{BASE}/mcp", http_client=hc) as streams:
        async with ClientSession(streams[0], streams[1], elicitation_callback=on_elicit) as s:
            init = await s.initialize()
            r = await s.call_tool("artifact_create", {"title": "hs", "files": {"index.html": "<p>hs</p>"}})
            aid = r.structured_content["id"]
            r2 = await s.call_tool("artifact_set_visibility", {"id": aid, "visibility": "public"})
            return init.protocol_version, calls, r2, aid


async def main():
    async with httpx2.AsyncClient() as h:
        ver, calls, res, aid = await run("Make public")
        check(f"handshake era negotiated ({ver})", ver and ver < "2026-07-28", ver)
        check("handshake: elicitation callback invoked", len(calls) == 1, calls)
        st = (await h.get(f"{BASE}/a/{aid}/")).status_code
        check("handshake: accepted -> public", not res.is_error and st == 200, (res, st))
        ver, calls, res, aid = await run(None)
        st = (await h.get(f"{BASE}/a/{aid}/")).status_code
        check("handshake: declined -> error and still private", res.is_error and st == 404, (res.content, st))
    sys.exit(0 if ok_all else 1)


asyncio.run(main())
