"""Configuration: precedence, formats and the refusals of spec § 8."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from mcp_drop.config import (
    LogLevel,
    Settings,
    load_settings,
    parse_duration,
    parse_rate,
    parse_size,
)
from mcp_drop.domain import layout
from mcp_drop.domain.errors import ConfigError
from mcp_drop.domain.models import PublicMode, Scope

GIGABYTE = 1024**3


@pytest.fixture(autouse=True)
def _valid_environment(environment: None) -> None:
    """Every configuration spec starts from the valid environment in ``tests/conftest.py``."""


def _config_file(tmp_path: Path, body: str) -> Path:
    """Write a TOML file for the spec to load."""
    path = tmp_path / "drop.toml"
    path.write_text(body, encoding="utf-8")
    return path


class DescribeSizeParsing:
    class WhenItCarriesAUnit:
        def it_reads_binary_units(self) -> None:
            assert parse_size("512KB") == 512 * 1024
            assert parse_size("100MB") == 100 * 1024**2
            assert parse_size("10GB") == 10 * GIGABYTE

    class WhenItIsABareNumber:
        def it_reads_bytes(self) -> None:
            assert parse_size(4096) == 4096

    class WhenItMakesNoSense:
        def it_refuses_it(self) -> None:
            with pytest.raises(ConfigError):
                parse_size("10 gigabytes")


class DescribeDurationParsing:
    class WhenItCarriesAUnit:
        def it_reads_seconds_minutes_hours_and_days(self) -> None:
            assert parse_duration("30s") == timedelta(seconds=30)
            assert parse_duration("15m") == timedelta(minutes=15)
            assert parse_duration("1h") == timedelta(hours=1)
            assert parse_duration("7d") == timedelta(days=7)

    class WhenItIsABareNumber:
        def it_reads_seconds(self) -> None:
            assert parse_duration(90) == timedelta(seconds=90)

    class WhenItIsZeroOrNegative:
        def it_refuses_it(self) -> None:
            with pytest.raises(ConfigError):
                parse_duration("0s")
            with pytest.raises(ConfigError):
                parse_duration(-5)


class DescribeRateParsing:
    class WhenItNamesAPeriod:
        def it_reads_the_count_and_the_window(self) -> None:
            minute = parse_rate("60/m")
            assert minute is not None
            assert (minute.count, minute.seconds) == (60, 60)

        def it_reads_the_longer_windows(self) -> None:
            hour = parse_rate("1200/h")
            assert hour is not None
            assert hour.seconds == 3600

    class WhenItIsOff:
        def it_disables_the_bucket(self) -> None:
            assert parse_rate("off") is None

    class WhenTheWindowIsUnknown:
        def it_refuses_it(self) -> None:
            with pytest.raises(ConfigError):
                parse_rate("60/fortnight")


class DescribeDefaults:
    class WhenNothingElseSetsAKey:
        def it_uses_the_values_spec_8_2_documents(self) -> None:
            settings = load_settings()
            assert (settings.host, settings.port) == ("127.0.0.1", 8080)
            assert settings.public_base_url == "http://localhost:8080"
            assert settings.public_mode is PublicMode.CONFIRM
            assert settings.max_upload == 100 * 1024**2
            assert settings.max_mcp_body == 4 * 1024**2
            assert settings.quota == 10 * GIGABYTE
            assert settings.retain_versions == 10
            assert settings.ref_ttl == timedelta(hours=24)
            assert settings.link_ttl == timedelta(days=7)
            assert settings.upload_ttl == timedelta(minutes=15)
            assert settings.sweep_interval == timedelta(hours=1)
            assert settings.audit_retention == timedelta(days=365)
            assert settings.metrics_port == 0
            assert settings.log_level is LogLevel.INFO
            assert settings.forwarded_allow_ips == ["127.0.0.1"]


class DescribePrecedence:
    class WhenOnlyTheConfigFileSetsAKey:
        def it_takes_the_file_value(self, tmp_path: Path) -> None:
            settings = load_settings(
                config_path=_config_file(tmp_path, '[drop]\nhost = "0.0.0.0"\n')
            )
            assert settings.host == "0.0.0.0"

    class WhenTheFileUsesTopLevelKeys:
        def it_reads_them(self, tmp_path: Path) -> None:
            settings = load_settings(config_path=_config_file(tmp_path, 'host = "0.0.0.0"\n'))
            assert settings.host == "0.0.0.0"

    class WhenBothTheFileAndTheEnvironmentSetAKey:
        def it_prefers_the_environment(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
        ) -> None:
            monkeypatch.setenv("DROP_HOST", "10.0.0.5")
            settings = load_settings(
                config_path=_config_file(tmp_path, '[drop]\nhost = "0.0.0.0"\n')
            )
            assert settings.host == "10.0.0.5"

    class WhenAFlagSetsAKeyTheEnvironmentAlsoSets:
        def it_prefers_the_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
            monkeypatch.setenv("DROP_PORT", "1111")
            settings = load_settings(flags={"port": 3333})
            assert settings.port == 3333

    class WhenTheFileMixesBothLevels:
        def it_refuses_the_ambiguity(self, tmp_path: Path) -> None:
            path = _config_file(tmp_path, 'host = "0.0.0.0"\n[drop]\nport = 9000\n')
            with pytest.raises(ConfigError):
                load_settings(config_path=path)


class DescribeEnvironmentFormats:
    class WhenAListIsCommaSeparated:
        def it_splits_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
            monkeypatch.setenv("DROP_ALLOWED_HOSTS", "drop.example.com, drop.local")
            assert load_settings().allowed_hosts == ["drop.example.com", "drop.local"]

    class WhenASizeCarriesAUnit:
        def it_reads_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
            monkeypatch.setenv("DROP_QUOTA", "20GB")
            assert load_settings().quota == 20 * GIGABYTE

    class WhenARateIsOff:
        def it_disables_the_bucket(self, monkeypatch: pytest.MonkeyPatch) -> None:
            monkeypatch.setenv("DROP_RATE_WRITES", "off")
            assert load_settings().rate_writes is None

    class WhenTheLogLevelIsLowercase:
        def it_reads_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
            monkeypatch.setenv("DROP_LOG_LEVEL", "debug")
            assert load_settings().log_level is LogLevel.DEBUG


class DescribeSecrets:
    class WhenTheyComeFromTheEnvironment:
        def it_reads_both_of_them(self, monkeypatch: pytest.MonkeyPatch) -> None:
            monkeypatch.setenv("DROP_TOKENS", f"hermes:{'b' * 64}:read,ci-bot:{'c' * 64}")
            settings = load_settings()
            assert settings.tokens["hermes"].scopes == {Scope.READ}
            assert settings.tokens["ci-bot"].scopes == frozenset(Scope)

    class WhenTheConfigFileSetsTheSecret:
        def it_refuses_the_file(self, tmp_path: Path) -> None:
            path = _config_file(tmp_path, '[drop]\nsecret = "not-a-place-for-secrets"\n')
            with pytest.raises(ConfigError):
                load_settings(config_path=path)

    class WhenTheConfigFileSetsTheTokens:
        def it_refuses_the_file(self, tmp_path: Path) -> None:
            path = _config_file(tmp_path, f'[drop]\ntokens = "hermes:{"b" * 64}"\n')
            with pytest.raises(ConfigError):
                load_settings(config_path=path)

    class WhenTheSecretIsShorterThanTheMinimum:
        def it_refuses_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
            monkeypatch.setenv("DROP_SECRET", "too short")
            with pytest.raises(ConfigError):
                load_settings()

    class WhenATokenHashIsNotASha256:
        def it_refuses_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
            monkeypatch.setenv("DROP_TOKENS", "hermes:not-a-hash")
            with pytest.raises(ConfigError):
                load_settings()

    class WhenATokenNameIsRepeated:
        def it_refuses_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
            monkeypatch.setenv("DROP_TOKENS", f"hermes:{'b' * 64},hermes:{'c' * 64}")
            with pytest.raises(ConfigError):
                load_settings()

    class WhenATokenNameIsNotALowercaseIdentifier:
        def it_refuses_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
            monkeypatch.setenv("DROP_TOKENS", f"Hermes:{'b' * 64}")
            with pytest.raises(ConfigError):
                load_settings()

    class WhenNoTokenIsConfigured:
        def it_refuses_the_configuration(self, monkeypatch: pytest.MonkeyPatch) -> None:
            monkeypatch.setenv("DROP_TOKENS", "")
            with pytest.raises(ConfigError):
                load_settings()


class DescribeValidation:
    class WhenTheLinkTtlExceedsItsCeiling:
        def it_refuses_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
            monkeypatch.setenv("DROP_LINK_TTL", "30d")
            monkeypatch.setenv("DROP_LINK_TTL_MAX", "7d")
            with pytest.raises(ConfigError):
                load_settings()

    class WhenTheUploadTtlExceedsItsCeiling:
        def it_refuses_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
            monkeypatch.setenv("DROP_UPLOAD_TTL", "2h")
            monkeypatch.setenv("DROP_UPLOAD_TTL_MAX", "15m")
            with pytest.raises(ConfigError):
                load_settings()

    class WhenTheArtifactLimitExceedsTheQuota:
        def it_refuses_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
            monkeypatch.setenv("DROP_MAX_ARTIFACT", "20GB")
            monkeypatch.setenv("DROP_QUOTA", "1GB")
            with pytest.raises(ConfigError):
                load_settings()

    class WhenTheBaseUrlCarriesAPath:
        def it_refuses_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
            monkeypatch.setenv("DROP_PUBLIC_BASE_URL", "https://drop.example.com/drop")
            with pytest.raises(ConfigError):
                load_settings()

    class WhenTheBaseUrlIsNotHttp:
        def it_refuses_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
            monkeypatch.setenv("DROP_PUBLIC_BASE_URL", "ftp://drop.example.com")
            with pytest.raises(ConfigError):
                load_settings()

    class WhenTheBaseUrlCarriesCredentials:
        def it_refuses_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
            monkeypatch.setenv("DROP_PUBLIC_BASE_URL", "https://user:pass@drop.example.com")
            with pytest.raises(ConfigError):
                load_settings()

    class WhenTheBaseUrlEndsInASlash:
        def it_normalises_it_to_an_origin(self, monkeypatch: pytest.MonkeyPatch) -> None:
            monkeypatch.setenv("DROP_PUBLIC_BASE_URL", "https://drop.example.com/")
            assert load_settings().public_base_url == "https://drop.example.com"

    class WhenARendererIsUnknown:
        def it_refuses_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
            monkeypatch.setenv("DROP_RENDERERS", "md,docx")
            with pytest.raises(ConfigError):
                load_settings()

    class WhenTheFileNamesAKeyThatDoesNotExist:
        def it_refuses_the_file(self, tmp_path: Path) -> None:
            path = _config_file(tmp_path, '[drop]\nmax_uplaod = "1MB"\n')
            with pytest.raises(ConfigError, match="max_uplaod"):
                load_settings(config_path=path)

    class WhenTheFileIsNotToml:
        def it_refuses_the_file(self, tmp_path: Path) -> None:
            path = _config_file(tmp_path, "this is not toml")
            with pytest.raises(ConfigError):
                load_settings(config_path=path)

    class WhenTheFileIsNotThere:
        def it_refuses_to_start(self, tmp_path: Path) -> None:
            with pytest.raises(ConfigError):
                load_settings(config_path=tmp_path / "missing.toml")


class DescribeTheEnvironmentItself:
    class WhenAVariableNamesAKeyThatDoesNotExist:
        def it_refuses_to_start(self, monkeypatch: pytest.MonkeyPatch) -> None:
            monkeypatch.setenv("DROP_MAX_UPLAOD", "1MB")
            with pytest.raises(ConfigError, match="DROP_MAX_UPLAOD"):
                load_settings()


class DescribeTheDataDirectoryCheck:
    class WhenTheStagingDirectoryIsOnAnotherFilesystem:
        def it_refuses_to_start(self, monkeypatch: pytest.MonkeyPatch) -> None:
            monkeypatch.setattr(layout, "_device", lambda path: 1 if path.name == "tmp" else 0)
            with pytest.raises(ConfigError):
                load_settings()

    class WhenBothDirectoriesShareAFilesystem:
        def it_starts(self) -> None:
            assert isinstance(load_settings(), Settings)
