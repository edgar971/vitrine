"""The ``data_dir`` layout: one directory per artifact, plus refs, tmp and audit (spec § 4.1)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from mcp_vitrine.domain.errors import ConfigError, NotFound
from mcp_vitrine.domain.ids import is_valid_id
from mcp_vitrine.domain.paths import validate_path

ARTIFACTS_DIR = "artifacts"
REFS_DIR = "refs"
TMP_DIR = "tmp"
AUDIT_DIR = "audit"
VIEWS_DIR = "views"
META_FILE = "meta.json"
VERSION_PREFIX = "v"


def ensure(root: Path) -> None:
    """Create every directory the layout needs, refusing a ``tmp`` on another filesystem."""
    check_root(root)
    for name in (ARTIFACTS_DIR, REFS_DIR, TMP_DIR, AUDIT_DIR, VIEWS_DIR):
        (root / name).mkdir(parents=True, exist_ok=True)


def check_root(root: Path) -> None:
    """Refuse a root whose staging directory lands on another filesystem (spec § 4.1, § 8.2)."""
    if _device(root) != _device(root / TMP_DIR):
        raise ConfigError(f"{root} and its {TMP_DIR}/ must be on one filesystem")


def artifact_dir(root: Path, artifact_id: str) -> Path:
    """The directory of one artifact; a malformed id is not found before any filesystem access."""
    if not is_valid_id(artifact_id):
        raise NotFound()
    return root / ARTIFACTS_DIR / artifact_id


def meta_file(root: Path, artifact_id: str) -> Path:
    """The ``meta.json`` of one artifact."""
    return artifact_dir(root, artifact_id) / META_FILE


def version_dir(root: Path, artifact_id: str, version: int) -> Path:
    """The immutable directory holding version ``version``."""
    return artifact_dir(root, artifact_id) / f"{VERSION_PREFIX}{version}"


def ref_dir(root: Path, client: str) -> Path:
    """The idempotency records of one client."""
    return root / REFS_DIR / validate_path(client)


def audit_file(root: Path, when: datetime) -> Path:
    """The audit log of one month."""
    return root / AUDIT_DIR / f"{when:%Y-%m}.jsonl"


def views_file(root: Path, when: datetime) -> Path:
    """The view counts of one month (spec § 11.4)."""
    return root / VIEWS_DIR / f"{when:%Y-%m}.jsonl"


def _device(path: Path) -> int:
    """The device of the nearest existing ancestor, so a tree that is not there yet still checks."""
    candidate = path
    while not candidate.exists() and candidate.parent != candidate:
        candidate = candidate.parent
    return candidate.stat().st_dev
