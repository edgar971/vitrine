"""The extension allowlist: what a file is served as, and which files read back as text (§ 7.2)."""

from __future__ import annotations

from pathlib import PurePosixPath

OCTET_STREAM = "application/octet-stream"

TEXT_TYPES: dict[str, str] = {
    "html": "text/html",
    "htm": "text/html",
    "css": "text/css",
    "js": "text/javascript",
    "mjs": "text/javascript",
    "json": "application/json",
    "md": "text/markdown",
    "txt": "text/plain",
    "csv": "text/csv",
    "tsv": "text/tab-separated-values",
    "xml": "application/xml",
    "svg": "image/svg+xml",
}

BINARY_TYPES: dict[str, str] = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "avif": "image/avif",
    "ico": "image/x-icon",
    "pdf": "application/pdf",
    "mp4": "video/mp4",
    "webm": "video/webm",
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "ogg": "audio/ogg",
    "woff": "font/woff",
    "woff2": "font/woff2",
}


def content_type(path: str) -> str:
    """The allowlisted type for ``path``'s extension, or ``application/octet-stream``."""
    extension = PurePosixPath(path).suffix.removeprefix(".").lower()
    return TEXT_TYPES.get(extension) or BINARY_TYPES.get(extension) or OCTET_STREAM


def is_text(path: str) -> bool:
    """Whether ``path`` is one of the text types a resource may return (spec § 5.2)."""
    return PurePosixPath(path).suffix.removeprefix(".").lower() in TEXT_TYPES
