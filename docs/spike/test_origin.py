"""Host/Origin guard (DNS-rebinding defence) against a real uvicorn bound to 0.0.0.0, as in the pod.

Starlette's TestClient is not usable for this: it reports the requested host as the server address,
which FastMCP adds to the allowlist, so every Host passes.
"""
import hashlib, os, secrets, subprocess, sys, time

import httpx

tok = secrets.token_urlsafe(16)
env = dict(os.environ, DROP_SECRET=secrets.token_hex(32),
           DROP_TOKENS="t:" + hashlib.sha256(tok.encode()).hexdigest())
srv = subprocess.Popen([sys.executable, "-m", "uvicorn", "origin_app:app", "--host", "0.0.0.0", "--port", "8766",
                        "--log-level", "warning"], env=env, cwd=os.path.dirname(os.path.abspath(__file__)))
rc = 1
try:
    for _ in range(60):
        try:
            httpx.get("http://127.0.0.1:8766/healthz", headers={"Host": "drop.example.com"})
            break
        except Exception:
            time.sleep(0.25)
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
    H = {"Accept": "application/json, text/event-stream", "Authorization": f"Bearer {tok}"}

    def post(host, origin=None):
        h = {**H, "Host": host, **({"Origin": origin} if origin else {})}
        return httpx.post("http://127.0.0.1:8766/mcp", json=body, headers=h).status_code

    def probe(host):
        return httpx.get("http://127.0.0.1:8766/healthz", headers={"Host": host}).status_code

    checks = [
        ("allowed Host drop.example.com -> 200", post("drop.example.com") == 200),
        ("DNS rebinding, foreign Host -> 421", post("evil.example") == 421),
        ("DNS rebinding, Host=Origin=evil -> 421", post("evil.example", "http://evil.example") == 421),
        ("foreign Origin on allowed Host -> 403", post("drop.example.com", "http://evil.example") == 403),
        ("kubelet-style probe (Host=pod IP) -> 421: probes need a Host header", probe("10.42.1.7:8080") == 421),
        ("probe with Host: drop.example.com -> 200", probe("drop.example.com") == 200),
    ]
    for name, ok in checks:
        print(("PASS " if ok else "FAIL ") + name)
    rc = 0 if all(ok for _, ok in checks) else 1
finally:
    srv.terminate()
    srv.wait()
sys.exit(rc)
