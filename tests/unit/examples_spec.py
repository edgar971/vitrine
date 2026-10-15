"""``examples/``: the configuration files load under the same rules the server applies."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from mcp_drop.config import Settings, load_settings, read_config_file
from mcp_drop.domain.models import PublicMode

EXAMPLES = Path(__file__).parents[2] / "examples"
CONFIGMAP = EXAMPLES / "kubernetes" / "configmap.yaml"


def _configmap_toml(target: Path) -> Path:
    """The ``drop.toml`` the Kubernetes ConfigMap embeds, written out to ``target``."""
    lines = CONFIGMAP.read_text(encoding="utf-8").split("drop.toml: |\n", maxsplit=1)[1]
    target.write_text(textwrap.dedent(lines), encoding="utf-8")
    return target


@pytest.fixture
def anywhere(environment: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """No data_dir from the environment, so the file's own applies; the scratch dir for files."""
    monkeypatch.delenv("DROP_DATA_DIR")
    return tmp_path


class DescribeTheExampleConfigFile:
    class WhenItIsLoaded:
        def it_is_a_valid_configuration(self, anywhere: Path) -> None:
            settings = load_settings(config_path=EXAMPLES / "drop.toml")
            assert settings.public_mode is PublicMode.CONFIRM

        def it_sets_every_key_it_names_to_a_known_setting(self) -> None:
            assert set(read_config_file(EXAMPLES / "drop.toml")) <= set(Settings.model_fields)


class DescribeTheKubernetesConfigMap:
    class WhenItsTomlIsLoaded:
        def it_is_a_valid_configuration(self, anywhere: Path) -> None:
            settings = load_settings(config_path=_configmap_toml(anywhere / "drop.toml"))
            assert settings.data_dir == Path("/data")

        def it_listens_beyond_loopback_so_the_service_reaches_it(self, anywhere: Path) -> None:
            settings = load_settings(config_path=_configmap_toml(anywhere / "drop.toml"))
            assert settings.host == "0.0.0.0"
