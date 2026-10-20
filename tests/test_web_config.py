from pathlib import Path

import pytest
import yaml
from aihub_web.config import Config, ConfigError, load_config, secret

EXAMPLE = Path(__file__).parents[1] / "config" / "web.example.yaml"


def write(tmp_path: Path, data: object) -> Path:
    path = tmp_path / "web.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_missing_file_means_defaults(tmp_path: Path) -> None:
    config = load_config(tmp_path / "absent.yaml", {})
    assert config == Config()
    assert config.retention_days == 30


def test_shipped_example_loads() -> None:
    config = load_config(EXAMPLE, {})
    assert config.retention_days == 30
    assert config.host == "127.0.0.1"


def test_shipped_example_matches_the_defaults() -> None:
    """The example documents the defaults; if they drift apart, one of them is lying."""
    loaded = load_config(EXAMPLE, {"AIHUB_STATE_DIR": str(Config().state_dir)})
    assert loaded == Config()


def test_state_dir_comes_from_the_environment(tmp_path: Path) -> None:
    config = load_config(tmp_path / "absent.yaml", {"AIHUB_STATE_DIR": "~/somewhere"})
    assert config.state_dir == Path("~/somewhere").expanduser()


def test_port_zero_asks_the_system_for_a_free_port(tmp_path: Path) -> None:
    assert load_config(write(tmp_path, {"port": 0}), {}).port == 0


def test_the_port_comes_from_the_environment_when_it_is_set(tmp_path: Path) -> None:
    path = write(tmp_path, {"port": 9000})
    assert load_config(path, {}).port == 9000
    assert load_config(path, {"AIHUB_PORT": "9100"}).port == 9100
    assert load_config(path, {"AIHUB_PORT": "0"}).port == 0
    assert load_config(tmp_path / "absent.yaml", {"AIHUB_PORT": "9100"}).port == 9100


def test_a_blank_port_variable_changes_nothing(tmp_path: Path) -> None:
    assert load_config(write(tmp_path, {"port": 9000}), {"AIHUB_PORT": ""}).port == 9000


@pytest.mark.parametrize("value", ["abc", "80.5", "8080x", "0x1f90", "1e3"])
def test_a_port_variable_that_is_not_an_integer_is_a_config_error(
    tmp_path: Path, value: str
) -> None:
    with pytest.raises(
        ConfigError, match=r"\.yaml: port: Input should be a valid integer"
    ):
        load_config(tmp_path / "absent.yaml", {"AIHUB_PORT": value})


@pytest.mark.parametrize("value", ["-1", "65536", "99999"])
def test_a_port_variable_outside_the_range_is_a_config_error(
    tmp_path: Path, value: str
) -> None:
    with pytest.raises(ConfigError, match=r"\.yaml: port: Input should be"):
        load_config(tmp_path / "absent.yaml", {"AIHUB_PORT": value})


@pytest.mark.parametrize("port", [-1, 65536])
def test_a_port_outside_the_range_in_the_file_is_a_config_error(
    tmp_path: Path, port: int
) -> None:
    with pytest.raises(ConfigError, match=r"\.yaml: port: Input should be"):
        load_config(write(tmp_path, {"port": port}), {})


@pytest.mark.parametrize(
    "default_models",
    [
        ["anthropic/claude-haiku-5-5"],
        ["openrouter/deepseek/deepseek-v4.1-flash-1017"],
        ["ollama/qwen3:8b", "anthropic/claude-opus-5-5"],
        [],
    ],
)
def test_defaults_must_be_free_or_local(
    tmp_path: Path, default_models: list[str]
) -> None:
    with pytest.raises(ConfigError, match="default_models"):
        load_config(write(tmp_path, {"default_models": default_models}), {})


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "example.com"])
def test_only_loopback_hosts_are_allowed(tmp_path: Path, host: str) -> None:
    with pytest.raises(ConfigError, match="loopback"):
        load_config(write(tmp_path, {"host": host}), {})


def test_typos_are_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="retention_day"):
        load_config(write(tmp_path, {"retention_day": 7}), {})


def test_every_problem_is_listed(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as err:
        load_config(write(tmp_path, {"retention_days": 0, "port": 99999}), {})
    assert "retention_days" in str(err.value) and "port" in str(err.value)


def test_a_non_mapping_file_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "web.yaml"
    path.write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="mapping"):
        load_config(path, {})


def test_secret_ignores_blank_values() -> None:
    assert secret("K", {"K": "  sk-1  "}) == "sk-1"
    assert secret("K", {"K": "   "}) is None
    assert secret("K", {}) is None
