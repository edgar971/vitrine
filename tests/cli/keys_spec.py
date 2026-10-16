"""``token``, ``secret`` and ``config check``: making and checking credentials (spec § 9)."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

from mcp_vitrine.config import parse_tokens
from tests.cli.conftest import Runner
from tests.conftest import SECRET


def _field(output: str, name: str) -> str:
    """The value of one ``name: value`` line."""
    match = re.search(rf"^{name}: (\S+)$", output, re.MULTILINE)
    assert match is not None, output
    return match.group(1)


class DescribeTokenNew:
    class WhenScopesAreGiven:
        def it_prints_an_entry_holding_exactly_those_scopes(self, run: Runner) -> None:
            entry = _field(run("token", "new", "ci-bot", "--scopes", "write").out, "entry")
            assert entry.endswith(":write")

        def it_prints_an_entry_whose_hash_is_the_token(self, run: Runner) -> None:
            out = run("token", "new", "ci-bot", "--scopes", "read,write").out
            digest = hashlib.sha256(_field(out, "token").encode()).hexdigest()
            assert _field(out, "entry") == f"ci-bot:{digest}:read+write"

    class WhenNoScopesAreGiven:
        def it_names_all_four_so_config_check_has_nothing_to_warn_about(self, run: Runner) -> None:
            entry = _field(run("token", "new", "hermes").out, "entry")
            assert entry.endswith(":read+write+share+publish")

        def it_prints_an_entry_vitrine_tokens_accepts(self, run: Runner) -> None:
            entry = _field(run("token", "new", "hermes").out, "entry")
            assert list(parse_tokens(entry)) == ["hermes"]

    class WhenItRunsTwice:
        def it_prints_a_different_token(self, run: Runner) -> None:
            first = _field(run("token", "new", "hermes").out, "token")
            assert _field(run("token", "new", "hermes").out, "token") != first

    class WhenItRuns:
        def it_writes_nothing_to_disk(
            self, run: Runner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
        ) -> None:
            monkeypatch.chdir(tmp_path)
            run("token", "new", "hermes")
            assert list(tmp_path.iterdir()) == []

    class WhenTheNameIsInvalid:
        def it_exits_as_misconfigured(self, run: Runner) -> None:
            assert run("token", "new", "Not A Name").status == 2

    class WhenAScopeIsUnknown:
        def it_names_the_scope(self, run: Runner) -> None:
            assert "'admin'" in run("token", "new", "hermes", "--scopes", "admin").err


class DescribeTokenHash:
    class WhenGivenAToken:
        def it_prints_its_sha256(self, run: Runner) -> None:
            assert run("token", "hash", "abc").out.strip() == hashlib.sha256(b"abc").hexdigest()


class DescribeSecretNew:
    class WhenItRuns:
        def it_prints_32_random_bytes_in_hex(self, run: Runner) -> None:
            assert re.fullmatch(r"[0-9a-f]{64}\n", run("secret", "new").out)


class DescribeConfigCheck:
    class WhenTheConfigurationIsValid:
        def it_exits_zero(self, run: Runner, environment: None) -> None:
            assert run("config", "check").status == 0

        def it_prints_the_effective_values(self, run: Runner, environment: None) -> None:
            assert 'public_mode = "confirm"' in run("config", "check").out

        def it_never_prints_the_secret(self, run: Runner, environment: None) -> None:
            assert SECRET not in run("config", "check").out

        def it_never_prints_a_token_hash(self, run: Runner, environment: None) -> None:
            assert "a" * 64 not in run("config", "check").out

        def it_names_each_client_with_its_scopes(self, run: Runner, environment: None) -> None:
            assert "hermes (publish+read+share+write)" in run("config", "check").out

    class WhenATokenOmitsItsScopes:
        def it_warns_that_it_holds_all_four(
            self, run: Runner, environment: None, monkeypatch: pytest.MonkeyPatch
        ) -> None:
            monkeypatch.setenv("VITRINE_TOKENS", f"hermes:{'a' * 64}")
            assert "warning: token 'hermes' lists no scopes" in run("config", "check").err

    class WhenAValueIsInvalid:
        def it_exits_as_misconfigured(
            self, run: Runner, environment: None, monkeypatch: pytest.MonkeyPatch
        ) -> None:
            monkeypatch.setenv("VITRINE_SECRET", "short")
            assert run("config", "check").status == 2

    class WhenTheFileHoldsASecret:
        def it_names_the_problem_without_echoing_it(
            self, run: Runner, environment: None, tmp_path: Path
        ) -> None:
            config = tmp_path / "vitrine.toml"
            config.write_text('secret = "do-not-print-me-0123456789abcdef"\n')
            assert "do-not-print-me" not in run("config", "check", "--config", str(config)).err
