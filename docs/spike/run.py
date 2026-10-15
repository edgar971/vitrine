"""Start the spike server, run every black-box check, exit non-zero on any FAIL.

    cd docs/spike
    uv run --python 3.15 --with fastmcp==4.1.0 --with uvicorn --with httpx --with playwright python run.py

Chromium: uses /opt/pw-browsers/chromium when present, else Playwright's own (`playwright install chromium`).
"""
import hashlib, os, secrets, shutil, subprocess, sys, tempfile, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
token = secrets.token_urlsafe(32)
env = dict(os.environ,
           DROP_TOKENS=f"hermes:{hashlib.sha256(token.encode()).hexdigest()}",
           DROP_SECRET=secrets.token_hex(32),
           DROP_DATA=tempfile.mkdtemp(prefix="drop-spike-"),
           T_TOKEN=token)
py = sys.executable
srv = subprocess.Popen([py, "-m", "uvicorn", "drop_spike:app", "--port", "8765", "--log-level", "warning"],
                       cwd=HERE, env=env)
try:
    for _ in range(60):
        try:
            urllib.request.urlopen("http://127.0.0.1:8765/healthz", timeout=1)
            break
        except Exception:
            time.sleep(0.25)
    rc = (subprocess.call([py, "test_spike.py"], cwd=HERE, env=env)
          or subprocess.call([py, "test_handshake.py"], cwd=HERE, env=env))
finally:
    srv.terminate()
    srv.wait()
rc = rc or subprocess.call([py, "test_origin.py"], cwd=HERE, env=env)  # starts its own guarded server
rc = rc or subprocess.call([py, "test_patterns.py"], cwd=HERE, env=env)  # design § 4 conventions
shutil.rmtree(env["DROP_DATA"], ignore_errors=True)
sys.exit(rc)
