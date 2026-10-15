"""FastMCP 4.1 conventions from design § 4: LocalProvider + decorator, annotations, Annotated params,
Depends/CurrentAccessToken injection, per-tool scopes. Run: python test_patterns.py"""
import asyncio, sys
from typing import Annotated
from pydantic import Field
from fastmcp import FastMCP, Client
from fastmcp.server.providers import LocalProvider
from fastmcp.dependencies import Depends, CurrentAccessToken
from fastmcp.server.auth import AccessToken, TokenVerifier, require_scopes
from mcp.types import ToolAnnotations

class Service:
    def create(self, title: str, source: str) -> dict:
        return {"title": title, "source": source}

def get_service() -> Service:
    return Service()

artifacts = LocalProvider()

@artifacts.tool(
    annotations=ToolAnnotations(title="Create artifact", read_only_hint=False, destructive_hint=False,
                                idempotent_hint=False, open_world_hint=False),
    tags={"write"},
    auth=require_scopes("drop:write"),
)
def artifact_create(
    title: Annotated[str, Field(description="Shown in listings and the confirmation prompt.", max_length=200)],
    service: Service = Depends(get_service),
    token: AccessToken = CurrentAccessToken(),
) -> dict:
    """Publish text files as a private artifact."""
    return service.create(title, token.client_id)

@artifacts.tool(annotations=ToolAnnotations(read_only_hint=True), auth=require_scopes("drop:read"))
def artifact_list() -> list[str]:
    """List artifacts."""
    return []

class V(TokenVerifier):
    async def verify_token(self, token):
        scopes = {"rw": ["drop:read", "drop:write"], "ro": ["drop:read"]}.get(token)
        return AccessToken(token=token, client_id=f"client-{token}", scopes=scopes) if scopes else None

mcp = FastMCP("drop", providers=[artifacts], auth=V())
app = mcp.http_app(path="/mcp")

async def main():
    import uvicorn, threading, time
    srv = uvicorn.Server(uvicorn.Config(app, port=8790, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True); t.start()
    while not srv.started: await asyncio.sleep(0.05)
    from fastmcp.client.transports import StreamableHttpTransport as T
    checks = []
    async with Client(T("http://127.0.0.1:8790/mcp", headers={"Authorization": "Bearer rw"})) as c:
        tools = {t.name: t for t in await c.list_tools()}
        schema = tools["artifact_create"].input_schema
        ann = tools["artifact_create"].annotations
        checks += [
            ("injected params (Depends, CurrentAccessToken) hidden from schema", list(schema["properties"]) == ["title"]),
            ("Annotated Field description in schema", schema["properties"]["title"].get("description", "").startswith("Shown")),
            ("Annotated Field constraint in schema", schema["properties"]["title"].get("maxLength") == 200),
            ("ToolAnnotations hints exposed", ann.title == "Create artifact" and ann.open_world_hint is False),
            ("caller identity injected", (await c.call_tool("artifact_create", {"title": "x"})).data["source"] == "client-rw"),
        ]
    async with Client(T("http://127.0.0.1:8790/mcp", headers={"Authorization": "Bearer ro"})) as c:
        names = sorted(t.name for t in await c.list_tools())
        checks.append(("token without drop:write does not see the write tool", names == ["artifact_list"]))
        try:
            await c.call_tool("artifact_create", {"title": "x"})
            checks.append(("token without drop:write cannot call it", False))
        except Exception:
            checks.append(("token without drop:write cannot call it", True))
    for name, ok in checks:
        print(("PASS " if ok else "FAIL ") + name)
    rc = 0 if all(ok for _, ok in checks) else 1
    srv.should_exit = True
    return rc

sys.exit(asyncio.run(main()))
