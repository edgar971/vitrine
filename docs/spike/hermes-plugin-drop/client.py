"""HTTP client for drop's /api routes (bearer token from DROP_TOKEN)."""
from __future__ import annotations

import os
from pathlib import Path

import httpx

MAX_FILES = 200


def _client() -> httpx.Client:
    return httpx.Client(base_url=os.environ["DROP_URL"], timeout=120,
                        headers={"Authorization": f"Bearer {os.environ['DROP_TOKEN']}"})


def _collect(paths: list[str]) -> list[tuple[str, Path]]:
    out: list[tuple[str, Path]] = []
    for raw in paths:
        p = Path(raw).expanduser().resolve()
        if p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file() and not f.is_symlink() and not any(part.startswith(".") for part in f.relative_to(p).parts):
                    out.append((f.relative_to(p).as_posix(), f))
        elif p.is_file():
            out.append((p.name, p))
        else:
            raise FileNotFoundError(raw)
    if not out or len(out) > MAX_FILES:
        raise ValueError(f"need 1..{MAX_FILES} files, got {len(out)}")
    return out


def publish_paths(paths: list[str], title: str | None = None, id: str | None = None,
                  entry: str | None = None, expires_in: str | None = None) -> dict:
    files = _collect(paths)
    data = {k: v for k, v in {"title": title or files[0][0], "entry": entry, "expires_in": expires_in}.items() if v}
    handles = [("file", (rel, f.open("rb"))) for rel, f in files]
    try:
        with _client() as c:
            r = c.put(f"/api/artifacts/{id}", data=data, files=handles) if id else \
                c.post("/api/artifacts", data=data, files=handles)
    finally:
        for _, (_, fh) in handles:
            fh.close()
    r.raise_for_status()
    return r.json()


def list_artifacts(query: str | None = None, limit: int = 20) -> list[dict]:
    with _client() as c:
        r = c.get("/api/artifacts", params={k: v for k, v in {"q": query, "limit": limit}.items() if v})
    r.raise_for_status()
    return r.json()["items"]
