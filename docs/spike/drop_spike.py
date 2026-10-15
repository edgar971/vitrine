"""Throwaway spike for the drop design: proves the load-bearing mechanisms, not production code."""
import base64, hashlib, hmac, json, os, secrets, time, uuid
from pathlib import Path, PurePosixPath

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import TokenVerifier
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.dependencies import get_access_token
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response

DATA = Path(os.environ.get("DROP_DATA", "./data")).resolve()
SECRET = os.environ["DROP_SECRET"].encode()
# DROP_TOKENS = "name:sha256hex,name2:sha256hex"
TOKENS = {h: n for n, h in (p.split(":") for p in os.environ["DROP_TOKENS"].split(","))}
BASE = os.environ.get("DROP_PUBLIC_BASE", "http://127.0.0.1:8765")
MAX_UPLOAD = int(os.environ.get("DROP_MAX_UPLOAD", 1_000_000))
ID_ALPHA = "0123456789abcdefghjkmnpqrstvwxyz"  # Crockford base32, lowercase
MIME = {".html": "text/html; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
        ".css": "text/css", ".js": "text/javascript", ".pdf": "application/pdf", ".txt": "text/plain; charset=utf-8"}
SANDBOX = "sandbox allow-scripts allow-forms allow-popups allow-modals allow-downloads"


# ---- auth ---------------------------------------------------------------------------------
def token_name(raw: str) -> str | None:
    digest = hashlib.sha256(raw.encode()).hexdigest()
    for h, name in TOKENS.items():
        if hmac.compare_digest(h, digest):
            return name
    return None


class HashedTokenVerifier(TokenVerifier):
    async def verify_token(self, token: str) -> AccessToken | None:
        name = token_name(token)
        return AccessToken(token=token, client_id=name, scopes=["drop"]) if name else None


# ---- ids / signed links -----------------------------------------------------------------
def new_id() -> str:
    return "".join(secrets.choice(ID_ALPHA) for _ in range(16))


def b64(b: bytes) -> str: return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
def unb64(s: str) -> bytes: return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign(kind: str, aid: str, exp: int, epoch: int) -> str:
    body = f"{kind}|{aid}|{exp}|{epoch}".encode()
    return b64(body) + "." + b64(hmac.new(SECRET, body, hashlib.sha256).digest()[:16])


def verify(kind: str, tok: str) -> tuple[str, int] | None:
    try:
        body_s, mac_s = tok.split(".")
        body = unb64(body_s)
        if not hmac.compare_digest(unb64(mac_s), hmac.new(SECRET, body, hashlib.sha256).digest()[:16]):
            return None
        k, aid, exp, epoch = body.decode().split("|")
    except Exception:
        return None
    if k != kind or int(exp) < time.time():
        return None
    return aid, int(epoch)


# ---- store --------------------------------------------------------------------------------
def adir(aid: str) -> Path: return DATA / "artifacts" / aid


def safe_rel(p: str) -> PurePosixPath:
    rel = PurePosixPath(p)
    if rel.is_absolute() or ".." in rel.parts or not rel.parts or "\\" in p or "\x00" in p:
        raise ToolError(f"unsafe path: {p!r}")
    return rel


def read_meta(aid: str) -> dict | None:
    if len(aid) != 16 or any(c not in ID_ALPHA for c in aid):
        return None
    f = adir(aid) / "meta.json"
    return json.loads(f.read_text()) if f.exists() else None


def write_meta(meta: dict) -> None:
    tmp = DATA / "tmp" / f"{uuid.uuid4()}.json"
    tmp.write_text(json.dumps(meta, indent=2))
    os.replace(tmp, adir(meta["id"]) / "meta.json")


def put_version(meta: dict, files: dict[str, bytes]) -> dict:
    stage = DATA / "tmp" / str(uuid.uuid4())
    for p, data in files.items():
        dst = stage / safe_rel(p)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)
    n = meta.get("version", 0) + 1
    adir(meta["id"]).mkdir(parents=True, exist_ok=True)
    os.rename(stage, adir(meta["id"]) / f"v{n}")  # atomic on same fs
    meta["version"] = n
    meta["updated_at"] = time.time()
    write_meta(meta)
    return meta


def url_for(meta: dict, ttl: int = 7 * 86400) -> str:
    if meta["visibility"] == "public":
        return f"{BASE}/a/{meta['id']}/"
    return f"{BASE}/s/{sign('view', meta['id'], int(time.time()) + ttl, meta['link_epoch'])}/"


# ---- MCP ----------------------------------------------------------------------------------
mcp = FastMCP("drop", auth=HashedTokenVerifier())


@mcp.tool
def artifact_create(title: str, files: dict[str, str], entry: str = "index.html") -> dict:
    """Publish text files as a PRIVATE artifact. Only artifact_set_visibility can make it public."""
    tok = get_access_token()
    meta = {"id": new_id(), "title": title, "visibility": "private", "entry": entry,
            "link_epoch": 0, "source": tok.client_id if tok else None, "created_at": time.time()}
    (DATA / "tmp").mkdir(parents=True, exist_ok=True)
    put_version(meta, {k: v.encode() for k, v in files.items()})
    return {"id": meta["id"], "url": url_for(meta), "version": meta["version"], "source": meta["source"]}


@mcp.tool
def artifact_update(id: str, files: dict[str, str]) -> dict:
    """Add a new version; the id (and public URL) stay the same."""
    meta = read_meta(id) or (_ for _ in ()).throw(ToolError("not found"))
    put_version(meta, {k: v.encode() for k, v in files.items()})
    return {"id": id, "url": url_for(meta), "version": meta["version"]}


@mcp.tool
def artifact_revoke_links(id: str) -> dict:
    meta = read_meta(id) or (_ for _ in ()).throw(ToolError("not found"))
    meta["link_epoch"] += 1
    write_meta(meta)
    return {"id": id, "link_epoch": meta["link_epoch"]}


import mcp.types as mt
from pydantic import BaseModel


class PublicConsent(BaseModel):
    """Consent form. The default lets approve-only clients (Hermes sends accept + {}) validate."""
    confirm: bool = True

# Approve/deny only: Hermes answers elicitations as consent (accept with empty content), so the
# decision is the action alone; no form field is required or read.
CONFIRM_SCHEMA = {"type": "object", "properties": {"confirm": {"type": "boolean", "default": True}}}


async def confirm_public(ctx: Context, title: str):
    """Human confirmation before anything goes public. Returns True, or an InputRequiredResult to
    return from the tool (2026-07-28 guard pattern). Raises ToolError on decline / no support (fail closed)."""
    msg = f"Make '{title}' PUBLIC? Anyone with the link can view it."
    if ctx._is_modern_protocol():                      # 2026-07-28: multi-round-trip guard pattern
        answers = ctx.input_responses
        if answers is None:
            return mt.InputRequiredResult(input_requests={"confirm": mt.ElicitRequest(
                params=mt.ElicitRequestFormParams(message=msg, requested_schema=CONFIRM_SCHEMA))})
        r = answers.get("confirm")
        ok = r is not None and r.action == "accept" and (r.content or {}).get("confirm", True) is not False
    else:                                              # handshake era (Hermes today): server-initiated elicit
        try:
            r = await ctx.elicit(msg, response_type=PublicConsent)
        except Exception as e:
            raise ToolError(f"public publishing needs human confirmation; client cannot confirm ({e})")
        ok = r.action == "accept" and r.data.confirm is not False
    if not ok:
        raise ToolError("human declined: artifact stays private")
    return True


@mcp.tool
async def artifact_set_visibility(id: str, visibility: str, ctx: Context):
    """Change visibility. Going public asks the human to confirm; fails closed."""
    meta = read_meta(id) or (_ for _ in ()).throw(ToolError("not found"))
    if visibility == "public" and meta["visibility"] != "public":
        ok = await confirm_public(ctx, meta["title"])
        if ok is not True:
            return ok
    meta["visibility"] = visibility
    meta["link_epoch"] += 1  # visibility change also invalidates old private links
    write_meta(meta)
    return {"id": id, "visibility": visibility, "url": url_for(meta)}


@mcp.resource("artifact://{id}")
def artifact_resource(id: str) -> str:
    return json.dumps(read_meta(id))


# ---- HTTP views ---------------------------------------------------------------------------
HEADERS = {"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
           "X-Robots-Tag": "noindex, nofollow", "Cache-Control": "private, no-store"}


def serve(meta: dict, path: str) -> Response:
    rel = safe_rel(path or meta["entry"]) if path else PurePosixPath(meta["entry"])
    root = (adir(meta["id"]) / f"v{meta['version']}").resolve()
    f = (root / rel).resolve()
    if not f.is_relative_to(root) or not f.is_file() or f.is_symlink():
        return Response("not found", 404, headers=HEADERS)
    ctype = MIME.get(f.suffix.lower())
    h = dict(HEADERS)
    if ctype is None:
        ctype = "application/octet-stream"
        h["Content-Disposition"] = "attachment"
    if f.suffix.lower() in (".html", ".svg"):
        h["Content-Security-Policy"] = SANDBOX
    return FileResponse(f, media_type=ctype, headers=h)


@mcp.custom_route("/a/{aid}/{path:path}", methods=["GET"])
async def public_view(request: Request) -> Response:
    meta = read_meta(request.path_params["aid"])
    if not meta or meta["visibility"] != "public":
        return Response("not found", 404, headers=HEADERS)  # never 403: don't confirm private ids
    try:
        return serve(meta, request.path_params["path"])
    except ToolError:
        return Response("not found", 404, headers=HEADERS)


@mcp.custom_route("/s/{sig}/{path:path}", methods=["GET"])
async def private_view(request: Request) -> Response:
    v = verify("view", request.path_params["sig"])
    meta = read_meta(v[0]) if v else None
    if not meta or v[1] != meta["link_epoch"]:
        return Response("not found", 404, headers=HEADERS)
    try:
        return serve(meta, request.path_params["path"])
    except ToolError:
        return Response("not found", 404, headers=HEADERS)


@mcp.custom_route("/api/upload", methods=["POST"])
async def upload(request: Request) -> Response:
    # custom routes are NOT covered by the MCP auth provider: check the bearer here
    auth = request.headers.get("authorization", "")
    name = token_name(auth[7:]) if auth.lower().startswith("bearer ") else None
    if not name:
        return JSONResponse({"error": "unauthorized"}, 401)
    declared = int(request.headers.get("content-length") or 0)
    if declared > MAX_UPLOAD:
        return JSONResponse({"error": "too large"}, 413)
    total, chunks = 0, []
    async for chunk in request.stream():  # enforce while streaming: Content-Length can lie / be absent
        total += len(chunk)
        if total > MAX_UPLOAD:
            return JSONResponse({"error": "too large"}, 413)
        chunks.append(chunk)
    filename = request.query_params.get("filename", "file.bin")
    meta = {"id": new_id(), "title": filename, "visibility": "private", "entry": filename,
            "link_epoch": 0, "source": name, "created_at": time.time()}
    (DATA / "tmp").mkdir(parents=True, exist_ok=True)
    try:
        put_version(meta, {filename: b"".join(chunks)})
    except ToolError as e:
        return JSONResponse({"error": str(e)}, 400)
    return JSONResponse({"id": meta["id"], "url": url_for(meta)}, 201)


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_: Request) -> Response:
    return JSONResponse({"ok": True})


app = mcp.http_app(path="/mcp")
