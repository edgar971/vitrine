"""Prepare a release commit: bump every copy of the version and write its changelog section.

Run through ``just release <version>``, which then re-locks and runs every gate. Nothing here
tags or pushes: the tag is a separate, deliberate step (CONTRIBUTING.md § Releasing).

- A pre-release (``0.2.0rc1``) only bumps the versions; its GitHub notes are rendered from the
  commits by the release workflow, and CHANGELOG.md is left alone.
- A final version gets a ``## [x.y.z] - date`` section: the hand-written ``[Unreleased]`` notes
  for the first release, and git-cliff's rendering of the commits since the last final version
  for every one after it.
"""

from __future__ import annotations

import datetime as dt
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GIT_CLIFF = "git-cliff@2.14.2"
# PEP 440, restricted to what a tag may carry: a final version or an a/b/rc pre-release.
VERSION = re.compile(r"^(?P<base>\d+\.\d+\.\d+)(?P<pre>(?:a|b|rc)\d+)?$")
FINAL_TAG = re.compile(r"^v\d+\.\d+\.\d+$")
UNRELEASED = "## [Unreleased]"
IMAGE = "ghcr.io/edgar971/vitrine:"

# Each file that repeats the version, and how its copy is written. The Hermes plugin carries the
# base version: its manifest is not a Python package and has no pre-release form.
PYPROJECT = Path("pyproject.toml")
IMAGE_COPIES = (Path("examples/compose.yaml"), Path("examples/kubernetes/deployment.yaml"))
PLUGIN_COPIES = (
    Path("integrations/hermes/vitrine/plugin.yaml"),
    Path("integrations/hermes/vitrine/SKILL.md"),
)


class ReleaseError(Exception):
    """The release cannot be prepared as asked."""


def parse(version: str) -> re.Match[str]:
    """Check ``version`` is one a tag may name."""
    match = VERSION.fullmatch(version)
    if match is None:
        raise ReleaseError(f"{version!r} is not x.y.z or x.y.z(a|b|rc)N")
    return match


def bump_pyproject(text: str, version: str) -> str:
    """Set the project's ``version = "…"`` line."""
    return _replace_once(text, r'^version = "[^"]*"$', f'version = "{version}"')


def bump_image(text: str, version: str) -> str:
    """Point the example's image at this version's tag."""
    return _replace_once(text, rf"{re.escape(IMAGE)}[^\s\"']+", f"{IMAGE}{version}")


def bump_plugin(text: str, version: str) -> str:
    """Set the manifest's ``version:`` line to the base version."""
    return _replace_once(text, r"^version: .*$", f"version: {parse(version)['base']}")


def roll_changelog(text: str, version: str, *, section: str | None, today: dt.date) -> str:
    """Turn ``[Unreleased]`` into this version's section, leaving an empty one above it.

    ``section`` is git-cliff's rendering for a release after the first; ``None`` keeps the
    hand-written ``[Unreleased]`` notes, which is how the first release is described.
    """
    head, mark, rest = text.partition(UNRELEASED)
    if not mark:
        raise ReleaseError(f"CHANGELOG.md has no {UNRELEASED} section")
    if f"## [{version}]" in rest:
        raise ReleaseError(f"CHANGELOG.md already has a [{version}] section")
    body, next_heading, older = rest.partition("\n## [")
    if section is None:
        if not body.strip():
            raise ReleaseError(f"{UNRELEASED} is empty: there is nothing to release")
        section = f"## [{version}] - {today.isoformat()}\n\n{body.strip()}\n"
    elif body.strip():
        raise ReleaseError(
            f"{UNRELEASED} has hand-written notes; after the first release the section comes "
            "from the commits, so move them into commit messages or remove them"
        )
    tail = f"\n{next_heading.strip()}" + older if next_heading else ""
    return f"{head}{UNRELEASED}\n\n{section.strip()}\n{tail}".rstrip() + "\n"


def _replace_once(text: str, pattern: str, replacement: str) -> str:
    """Replace the one match of ``pattern``; none, or more than one, is an error."""
    updated, count = re.subn(pattern, replacement, text, flags=re.MULTILINE)
    if count != 1:
        raise ReleaseError(f"expected one match for {pattern!r}, found {count}")
    return updated


def _final_tags() -> list[str]:
    """The final-version tags that exist, so the first release is told apart from later ones."""
    tags = subprocess.run(
        ["git", "tag", "--list", "v*"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.split()
    return [tag for tag in tags if FINAL_TAG.fullmatch(tag)]


def _rendered(version: str) -> str:
    """git-cliff's section for the commits since the last final version."""
    return subprocess.run(
        ["uvx", GIT_CLIFF, "--unreleased", "--tag", f"v{version}", "--strip", "all"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _rewrite(path: Path, change: Callable[[str], str]) -> None:
    """Apply ``change`` to one file under the repository root."""
    target = ROOT / path
    target.write_text(change(target.read_text(encoding="utf-8")), encoding="utf-8")


def main(argv: list[str]) -> int:
    """``release.py <version>``: bump the copies, then roll the changelog for a final version."""
    if len(argv) != 1:
        print("usage: release.py <version>", file=sys.stderr)
        return 2
    version = argv[0]
    try:
        final = parse(version)["pre"] is None
        _rewrite(PYPROJECT, lambda text: bump_pyproject(text, version))
        for path in IMAGE_COPIES:
            _rewrite(path, lambda text: bump_image(text, version))
        for path in PLUGIN_COPIES:
            _rewrite(path, lambda text: bump_plugin(text, version))
        if final:
            section = _rendered(version) if _final_tags() else None
            _rewrite(
                Path("CHANGELOG.md"),
                lambda text: roll_changelog(text, version, section=section, today=dt.date.today()),
            )
    except ReleaseError as error:
        print(f"release: {error}", file=sys.stderr)
        return 1
    print(f"release: prepared {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
