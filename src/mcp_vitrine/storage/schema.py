"""``meta.json`` schema versions: the startup check and forward migrations (operations § 4).

Migrations work on the raw JSON, never on ``Artifact``: the model refuses every version but the
current one, which is what stops a build from misreading data it does not understand.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from mcp_vitrine.domain import layout
from mcp_vitrine.domain.errors import SchemaMismatch
from mcp_vitrine.domain.models import SCHEMA_VERSION

type Meta = dict[str, object]
type Step = Callable[[Meta], Meta]


@dataclass(frozen=True, slots=True)
class Plan:
    """Where migrations lead (``target``) and how: ``steps[n]`` takes version ``n`` to ``n + 1``."""

    steps: Mapping[int, Step]
    target: int


def _add_schema2_defaults(meta: Meta) -> Meta:
    meta.setdefault("links", [])
    meta.setdefault("fallback", None)
    meta.setdefault("public_until", None)
    meta.setdefault("project", None)
    return meta


CURRENT = Plan(steps={}, target=SCHEMA_VERSION)


@dataclass(frozen=True, slots=True)
class Survey:
    """Which artifacts are behind or ahead of the schema this build reads."""

    older: tuple[str, ...] = ()
    newer: tuple[str, ...] = ()


def survey(root: Path, *, target: int = SCHEMA_VERSION) -> Survey:
    """Read every ``meta.json``'s ``schema_version``, without validating anything else."""
    older: list[str] = []
    newer: list[str] = []
    for artifact_id, meta in _metas(root):
        found = _version_of(artifact_id, meta)
        if found < target:
            older.append(artifact_id)
        elif found > target:
            newer.append(artifact_id)
    return Survey(older=tuple(older), newer=tuple(newer))


def require_current(root: Path, *, target: int = SCHEMA_VERSION) -> None:
    """Refuse to serve data this build would misread, naming the way out (operations § 4)."""
    found = survey(root, target=target)
    if found.newer:
        raise SchemaMismatch(
            f"{len(found.newer)} artifact(s) were written by a newer vitrine (schema_version above "
            f"{target}); restore the pre-upgrade backup instead of downgrading"
        )
    if found.older:
        raise SchemaMismatch(
            f"{len(found.older)} artifact(s) use an older schema_version; "
            "run `mcp-vitrine migrate` first"
        )


def migrate(root: Path, *, dry_run: bool = False, plan: Plan = CURRENT) -> tuple[str, ...]:
    """Upgrade every older ``meta.json`` to ``target``; the ids that changed (or would).

    Forward-only and idempotent: an up-to-date file is left alone, so a re-run after an
    interruption finishes the job. Each file is replaced in one rename, like a normal write.
    """
    found = survey(root, target=plan.target)
    if found.newer:
        raise SchemaMismatch("data is newer than this build; migrations only go forward")
    for artifact_id, meta in _metas(root):
        if artifact_id not in found.older:
            continue
        upgraded = _upgrade(artifact_id, meta, plan)
        if not dry_run:
            _replace(root, artifact_id, upgraded)
    return found.older


def _upgrade(artifact_id: str, meta: Meta, plan: Plan) -> Meta:
    """Apply each step in turn until ``meta`` is at the plan's target."""
    version = _version_of(artifact_id, meta)
    while version < plan.target:
        step = plan.steps.get(version)
        if step is None:
            raise SchemaMismatch(f"no migration from schema_version {version}")
        meta = step(dict(meta))
        version += 1
        meta["schema_version"] = version
    return meta


def _replace(root: Path, artifact_id: str, meta: Meta) -> None:
    """Stage the new ``meta.json`` under ``tmp/`` and rename it into place."""
    staged = root / layout.TMP_DIR / f"{uuid.uuid4().hex}.json"
    staged.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    staged.replace(layout.meta_file(root, artifact_id))


def _metas(root: Path) -> Iterator[tuple[str, Meta]]:
    """Every artifact's raw ``meta.json``, in id order."""
    artifacts = root / layout.ARTIFACTS_DIR
    if not artifacts.is_dir():
        return
    for directory in sorted(artifacts.iterdir()):
        path = directory / layout.META_FILE
        if not path.is_file():
            continue
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise SchemaMismatch(f"{directory.name}: meta.json is unreadable") from error
        if not isinstance(loaded, dict):
            raise SchemaMismatch(f"{directory.name}: meta.json is not an object")
        yield directory.name, loaded


def _version_of(artifact_id: str, meta: Meta) -> int:
    """The ``schema_version`` a raw ``meta.json`` declares."""
    version = meta.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise SchemaMismatch(f"{artifact_id}: meta.json has no schema_version")
    return version
