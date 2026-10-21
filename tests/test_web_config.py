from pathlib import Path

import pytest
import yaml
from aihub_web.config import Config, ConfigError, ImagesConfig, load_config, secret

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
    assert load_config(path, {"AIHUB_PORT": "65535"}).port == 65535
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


class TestImages:
    def test_image_generation_is_off_until_a_socket_is_named(self) -> None:
        assert Config().images.socket is None

    def test_the_shipped_models(self) -> None:
        zimage, klein = ImagesConfig().models
        assert (zimage.id, zimage.steps, zimage.sample_method, zimage.edits) == (
            "zimage",
            8,
            None,
            False,
        )
        assert (klein.id, klein.steps, klein.sample_method, klein.edits) == (
            "klein4b",
            4,
            "euler",
            True,
        )
        assert zimage.est_s.model_dump() == {"square": 23, "wide": 12, "tall": 12}
        assert klein.est_s.model_dump() == {"square": 8, "wide": 5, "tall": 5}

    def test_a_socket_and_models_load_from_the_file(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            {
                "images": {
                    "socket": "/run/user/1000/aihub-images/engine.sock",
                    "models": [
                        {
                            "id": "fast-one",
                            "name": "Fast one",
                            "steps": 2,
                            "est_s": {"square": 3, "wide": 2.5, "tall": 2.5},
                        }
                    ],
                }
            },
        )
        images = load_config(path, {}).images
        assert images.socket == "/run/user/1000/aihub-images/engine.sock"
        [model] = images.models
        assert (model.id, model.edits, model.note, model.sample_method) == (
            "fast-one",
            False,
            "",
            None,
        )
        assert model.est_s.wide == 2.5

    @pytest.mark.parametrize(
        "socket", ["engine.sock", "~/engine.sock", "./run/engine.sock", ""]
    )
    def test_the_socket_must_be_an_absolute_path(
        self, tmp_path: Path, socket: str
    ) -> None:
        with pytest.raises(ConfigError, match="images.socket"):
            load_config(write(tmp_path, {"images": {"socket": socket}}), {})

    @pytest.mark.parametrize(
        "model_id", ["", "Zimage", "z_image", "a/b", "../etc", "x" * 33, "a b"]
    )
    def test_model_ids_are_short_lowercase_words(
        self, tmp_path: Path, model_id: str
    ) -> None:
        model = {
            "id": model_id,
            "name": "M",
            "steps": 4,
            "est_s": {"square": 1, "wide": 1, "tall": 1},
        }
        with pytest.raises(ConfigError, match=r"images\.models\.0\.id"):
            load_config(write(tmp_path, {"images": {"models": [model]}}), {})

    def test_model_ids_are_unique(self, tmp_path: Path) -> None:
        model = {
            "id": "same",
            "name": "M",
            "steps": 4,
            "est_s": {"square": 1, "wide": 1, "tall": 1},
        }
        path = write(tmp_path, {"images": {"models": [model, model]}})
        with pytest.raises(ConfigError, match="same"):
            load_config(path, {})

    @pytest.mark.parametrize(
        "est_s",
        [
            {"square": 1, "wide": 1},
            {"square": 1, "wide": 1, "tall": 1, "huge": 1},
            {"square": 0, "wide": 1, "tall": 1},
        ],
    )
    def test_every_shape_needs_a_positive_estimate(
        self, tmp_path: Path, est_s: dict
    ) -> None:
        model = {"id": "m", "name": "M", "steps": 4, "est_s": est_s}
        with pytest.raises(ConfigError, match="est_s"):
            load_config(write(tmp_path, {"images": {"models": [model]}}), {})

    def test_typos_in_the_images_block_are_errors(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="sockett"):
            load_config(write(tmp_path, {"images": {"sockett": "/x"}}), {})

    @pytest.mark.parametrize(
        "default_models",
        [["images/zimage"], ["ollama/qwen3:8b", "images/klein4b"]],
    )
    def test_an_image_model_can_never_be_the_default(
        self, tmp_path: Path, default_models: list[str]
    ) -> None:
        path = write(tmp_path, {"default_models": default_models})
        with pytest.raises(ConfigError, match="default_models can't include an image"):
            load_config(path, {})
