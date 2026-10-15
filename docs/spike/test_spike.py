"""Black-box checks against a running drop_spike (uvicorn on :8765). Prints PASS/FAIL per claim."""
import asyncio, hashlib, json, os, sys, time
import httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from playwright.async_api import async_playwright

BASE = "http://127.0.0.1:8765"
TOKEN = os.environ["T_TOKEN"]
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))


async def mcp_client(token, elicitation_handler=None):
    return Client(StreamableHttpTransport(f"{BASE}/mcp", headers={"Authorization": f"Bearer {token}"} if token else {}),
                  elicitation_handler=elicitation_handler)


async def auto_accept(message, rtype, params, ctx):
    from fastmcp.client.elicitation import ElicitResult
    return ElicitResult(action="accept", content={})


async def main():
    h = httpx.AsyncClient(base_url=BASE)
    # --- auth on /mcp
    for label, tok in (("no token", None), ("wrong token", "nope")):
        try:
            async with await mcp_client(tok) as c:
                await c.list_tools()
            check(f"/mcp rejects {label}", False)
        except Exception as e:
            check(f"/mcp rejects {label}", True)
    r = await h.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                     headers={"Accept": "application/json, text/event-stream"})
    check("/mcp raw POST without token -> 401", r.status_code == 401, r.status_code)

    async with await mcp_client(TOKEN, auto_accept) as c:
        tools = {t.name for t in await c.list_tools()}
        check("tools listed with valid token", {"artifact_create", "artifact_update", "artifact_revoke_links"} <= tools, tools)

        html = ('<html><body><h1 id=t>init</h1><img id=i src="img/dot.svg">'
                '<script>document.getElementById("t").textContent="js-ran";'
                'let c="ok";try{c=document.cookie;c="READ:"+c}catch(e){c="blocked:"+e.name}'
                'let l="ok";try{localStorage.setItem("x","1");l="WROTE"}catch(e){l="blocked:"+e.name}'
                'document.title=JSON.stringify({c,l,o:window.origin});</script></body></html>')
        svg = '<svg xmlns="http://www.w3.org/2000/svg" width="4" height="4"><rect width="4" height="4"/></svg>'
        try:
            await c.call_tool("artifact_create", {"title": "x", "files": {"a.txt": "x"}, "visibility": "public"})
            check("artifact_create has no visibility parameter (single gate)", False)
        except Exception:
            check("artifact_create has no visibility parameter (single gate)", True)
        pub = (await c.call_tool("artifact_create", {"title": "pub", "files": {"index.html": html, "img/dot.svg": svg}})).data
        prv = (await c.call_tool("artifact_create", {"title": "prv", "files": {"index.html": html, "img/dot.svg": svg}})).data
        check("default visibility is private (signed /s/ url)", "/s/" in prv["url"] and "/s/" in pub["url"], prv["url"])
        pub.update((await c.call_tool("artifact_set_visibility", {"id": pub["id"], "visibility": "public"})).data)
        check("public only via artifact_set_visibility (confirmed)", "/a/" in pub["url"], pub)
        check("source recorded from token name", pub["source"] == "hermes", pub)

        # --- public view
        r = await h.get(f"/a/{pub['id']}/")
        check("public artifact served 200", r.status_code == 200, r.status_code)
        check("HTML gets CSP sandbox without allow-same-origin",
              "sandbox" in r.headers.get("content-security-policy", "") and "allow-same-origin" not in r.headers["content-security-policy"])
        check("nosniff + no-referrer + noindex headers",
              r.headers.get("x-content-type-options") == "nosniff" and r.headers.get("referrer-policy") == "no-referrer"
              and "noindex" in r.headers.get("x-robots-tag", ""))
        r = await h.get(f"/a/{pub['id']}/img/dot.svg")
        check("SVG also sandboxed", "sandbox" in r.headers.get("content-security-policy", ""))
        r = await h.get(f"/a/{prv['id']}/")
        check("private id on public path -> 404 (not 403)", r.status_code == 404, r.status_code)

        # --- private signed links
        path = prv["url"].replace(BASE, "")
        r = await h.get(path)
        check("private signed link -> 200", r.status_code == 200, r.status_code)
        r = await h.get(path + "img/dot.svg")
        check("relative asset under signed path -> 200", r.status_code == 200, r.status_code)
        sig = path.split("/")[2]
        body, mac = sig.split(".")
        tampered = f"/s/{body}.{mac[:-2] + ('AA' if mac[-2:] != 'AA' else 'BB')}/"
        check("tampered signature -> 404", (await h.get(tampered)).status_code == 404)
        import base64
        b = base64.urlsafe_b64decode(body + "==").decode().split("|")
        forged = base64.urlsafe_b64encode(f"{b[0]}|{pub['id']}|{b[2]}|0".encode()).rstrip(b"=").decode()
        check("signature not transplantable to another id -> 404", (await h.get(f"/s/{forged}.{mac}/")).status_code == 404)

        # --- update keeps id
        up = (await c.call_tool("artifact_update", {"id": pub["id"], "files": {"index.html": "<p>v2</p>"}})).data
        r = await h.get(f"/a/{pub['id']}/")
        check("update keeps id + URL and serves new version", up["url"] == pub["url"] and up["version"] == 2 and "v2" in r.text)

        # --- traversal
        try:
            await c.call_tool("artifact_create", {"title": "x", "files": {"../../evil.html": "x"}})
            check("create rejects ../ paths", False)
        except Exception as e:
            check("create rejects ../ paths", "unsafe path" in str(e), str(e)[:100])
        for p in (f"/a/{pub['id']}/..%2f..%2fmeta.json", f"/a/{pub['id']}/%2e%2e/%2e%2e/meta.json",
                  f"/a/{pub['id']}/../v1/index.html", f"/a/{prv['id']}%2f..%2f{prv['id']}/"):
            r = await h.get(p)
            check(f"traversal blocked: {p[:48]}", r.status_code == 404 and "link_epoch" not in r.text, r.status_code)

        # --- resource read-back
        res = await c.read_resource(f"artifact://{pub['id']}")
        check("artifact:// resource readable", pub["id"] in res[0].text)

        # --- browser: sandbox really isolates (run before revoke)
        async with async_playwright() as p:
            br = await p.chromium.launch(executable_path="/opt/pw-browsers/chromium" if os.path.exists("/opt/pw-browsers/chromium") else None)
            ctx = await br.new_context()
            await ctx.add_cookies([{"name": "session", "value": "SECRET", "url": BASE}])
            page = await ctx.new_page()
            await page.goto(BASE + path)
            await page.wait_for_function("document.title.startsWith('{')")
            info = json.loads(await page.title())
            check("browser: scripts run inside sandbox", await page.text_content("#t") == "js-ran")
            check("browser: document.cookie unreadable (SecurityError)", info["c"].startswith("blocked"), info)
            check("browser: localStorage unavailable", info["l"].startswith("blocked"), info)
            check("browser: opaque origin ('null')", info["o"] == "null", info)
            check("browser: relative <img> under signed path loads",
                  await page.evaluate("document.getElementById('i').naturalWidth") == 4)
            await br.close()

        # --- expiry: a correctly signed but expired link
        os.environ.setdefault("DROP_DATA", "./data")
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import drop_spike
        expired = drop_spike.sign("view", prv["id"], int(time.time()) - 1, 0)
        check("expired signed link -> 404", (await h.get(f"/s/{expired}/")).status_code == 404)
        upsig = drop_spike.sign("upload", prv["id"], int(time.time()) + 600, 0)
        check("upload-kind signature cannot be used to view", (await h.get(f"/s/{upsig}/")).status_code == 404)

        # --- revoke
        await c.call_tool("artifact_revoke_links", {"id": prv["id"]})
        check("revoked link (epoch bump) -> 404", (await h.get(path)).status_code == 404)

    # --- public requires human confirmation via elicitation (fails closed)
    from fastmcp.client.elicitation import ElicitResult
    async def accept(message, rtype, params, ctx):
        print("   elicitation handler called with:", message[:50]); return ElicitResult(action="accept", content={})
    async def decline(message, rtype, params, ctx):
        return ElicitResult(action="decline")
    def cl(handler):
        return Client(StreamableHttpTransport(f"{BASE}/mcp", headers={"Authorization": f"Bearer {TOKEN}"}),
                      elicitation_handler=handler)
    async with cl(decline) as c:
        a = (await c.call_tool("artifact_create", {"title": "e", "files": {"index.html": "<p>e</p>"}})).data
        try:
            await c.call_tool("artifact_set_visibility", {"id": a["id"], "visibility": "public"})
            check("declined confirmation keeps artifact private", False)
        except Exception as e:
            check("declined confirmation keeps artifact private",
                  "declined" in str(e) and (await h.get(f"/a/{a['id']}/")).status_code == 404, str(e)[:100])
    async with await mcp_client(TOKEN) as c:
        try:
            await c.call_tool("artifact_set_visibility", {"id": a["id"], "visibility": "public"})
            check("client without elicitation support cannot go public (fail closed)", False)
        except Exception as e:
            check("client without elicitation support cannot go public (fail closed)",
                  (await h.get(f"/a/{a['id']}/")).status_code == 404, str(e)[:100])
    async with cl(accept) as c:
        r2 = (await c.call_tool("artifact_set_visibility", {"id": a["id"], "visibility": "public"})).data
        check("accepted confirmation makes it public", (await h.get(f"/a/{a['id']}/")).status_code == 200, r2)

    r = await h.get("/healthz")
    check("custom routes bypass MCP auth (healthz 200 w/o token) -> each needs its own check", r.status_code == 200)
    # --- upload route auth + size
    r = await h.post("/api/upload?filename=a.png", content=b"x")
    check("/api/upload without token -> 401 (custom routes need own auth)", r.status_code == 401, r.status_code)
    auth = {"Authorization": f"Bearer {TOKEN}"}
    r = await h.post("/api/upload?filename=a.png", content=b"x" * 2_000_000, headers=auth)
    check("oversize upload (Content-Length) -> 413", r.status_code == 413, r.status_code)

    async def gen():
        for _ in range(40):
            yield b"x" * 64_000
    r = await h.post("/api/upload?filename=a.png", content=gen(), headers=auth)
    check("oversize chunked upload (no Content-Length) -> 413", r.status_code == 413, r.status_code)
    r = await h.post("/api/upload?filename=../x.png", content=b"x", headers=auth)
    check("upload filename traversal -> 400", r.status_code == 400, r.status_code)
    r = await h.post("/api/upload?filename=report.pdf", content=b"%PDF-1.4 x", headers=auth)
    check("valid upload -> 201 private link", r.status_code == 201 and "/s/" in r.json()["url"], r.text)
    r = await h.post("/api/upload?filename=run.exe", content=b"MZ", headers=auth)
    u = r.json()["url"].replace(BASE, "")
    r = await h.get(u + "run.exe")
    check("unknown type -> octet-stream + attachment",
          r.headers["content-type"] == "application/octet-stream" and r.headers.get("content-disposition") == "attachment")

    print(f"\n{sum(ok for _, ok in results)}/{len(results)} passed")
    sys.exit(0 if all(ok for _, ok in results) else 1)


asyncio.run(main())
