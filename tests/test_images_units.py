"""The image engine units and models.json, checked as text.

The isolation itself is measured on a running user manager by scripts/images/isolation_probe.sh. These tests
keep a later edit from quietly removing what that isolation depends on. The two that run systemd-analyze
(`verify` and the offline security score) need the binary and the usual user-session environment, but no
running unit.
"""

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SERVICE = ROOT / "systemd" / "aihub-images.service"
SOCKET = ROOT / "systemd" / "aihub-images.socket"
MODELS = ROOT / "systemd" / "images" / "models.json"
LAUNCHER = ROOT / "systemd" / "images" / "launcher.py"
PROBE = ROOT / "scripts" / "images" / "isolation_probe.sh"
REPO_AT_HOME = "%h/work/Dev/aihub"

HARDENING = {
    # network and file system
    "PrivateNetwork": "yes",
    "PrivateUsers": "yes",
    "PrivateTmp": "yes",
    "ProtectSystem": "strict",
    "ProtectHome": "tmpfs",
    "BindReadOnlyPaths": f"%h/work/aihub-images {REPO_AT_HOME}/systemd/images",
    # devices
    "PrivateDevices": "yes",
    "BindPaths": "/dev/nvidiactl /dev/nvidia0 /dev/nvidia-uvm -/dev/nvidia-uvm-tools",
    "TemporaryFileSystem": (
        "/dev/shm /run:ro /var/log:ro /var/lib/systemd/coredump:ro /dev/pts:ro"
    ),
    # isolation
    "PrivatePIDs": "yes",
    "PrivateIPC": "yes",
    "NoNewPrivileges": "yes",
    "CapabilityBoundingSet": "",
    "RestrictNamespaces": "yes",
    "RestrictSUIDSGID": "yes",
    "LockPersonality": "yes",
    # kernel and clock
    "ProtectKernelTunables": "yes",
    "ProtectKernelModules": "yes",
    "ProtectKernelLogs": "yes",
    "ProtectControlGroups": "yes",
    "ProtectClock": "yes",
    "ProtectHostname": "yes",
    "RestrictRealtime": "yes",
    "SystemCallArchitectures": "native",
    "RestrictAddressFamilies": "AF_UNIX AF_INET",
    "KeyringMode": "private",
    # resources
    "LimitCORE": "0",
    "UMask": "0077",
    "MemoryMax": "12G",
    "OOMScoreAdjust": "500",
    "Nice": "5",
}


def parse_unit(path: Path) -> dict[str, list[tuple[str, str]]]:
    """Sections of a unit file, each a list of (key, value) in file order, so repeated keys survive."""
    text = path.read_text()
    # systemd ends a line at a NUL byte too and splitlines() does not, so a directive could hide behind one.
    assert not re.search(r"[^\t\n\x20-\x7e]", text), (
        f"{path.name}: tabs, newlines and printable ASCII only"
    )
    sections: dict[str, list[tuple[str, str]]] = {}
    current = None
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1], [])
            continue
        key, separator, value = line.partition("=")
        assert separator and current is not None, f"{path.name}:{number}: {raw!r}"
        current.append((key.strip(), value.strip()))
    return sections


def values(unit, section: str, key: str) -> list[str]:
    return [v for k, v in unit.get(section, []) if k == key]


@pytest.fixture(scope="module")
def service():
    return parse_unit(SERVICE)


@pytest.fixture(scope="module")
def socket_unit():
    return parse_unit(SOCKET)


def test_no_hook_runs_outside_the_private_network_namespace(service):
    # In a user manager only the main process gets PrivateNetwork's namespace: ExecStartPre, ExecStartPost
    # and a proxy joined with JoinsNamespaceOf run in the host's, and one ExecStartPre moves the main
    # process there too (docs/adr/0001-image-engine.md). The launcher is the only thing the unit runs.
    keys = [k for section in service.values() for k, _ in section]
    assert [k for k in keys if k.startswith("Exec")] == ["ExecStart"]
    assert "JoinsNamespaceOf" not in keys


def test_the_engine_has_no_network_and_no_devices_but_the_gpu(service):
    assert values(service, "Service", "PrivateNetwork") == ["yes"]
    assert values(service, "Service", "PrivateDevices") == ["yes"]
    [binds] = values(service, "Service", "BindPaths")
    assert {node.lstrip("-") for node in binds.split()} == {
        "/dev/nvidiactl",
        "/dev/nvidia0",
        "/dev/nvidia-uvm",
        "/dev/nvidia-uvm-tools",
    }


def test_the_unit_sets_no_directive_that_these_tests_do_not_know(service):
    # ReadOnlyPaths=/run, say, puts the host's /run back over the empty one, and the tests above look only at
    # the directives they name. MemoryDenyWriteExecute and SystemCallFilter are left out on purpose: CUDA's JIT
    # and driver ioctls have not been tried under them. A new directive goes into HARDENING with its check.
    known = {
        "Unit": {"Description", "StartLimitIntervalSec", "StartLimitBurst"},
        "Service": set(HARDENING)
        | {"Type", "ExecStart", "WorkingDirectory", "TimeoutStopSec", "Environment"},
    }
    assert set(service) == set(known)
    for section, entries in service.items():
        unknown = {key for key, _ in entries} - known[section]
        assert not unknown, (section, sorted(unknown))


@pytest.mark.parametrize(("key", "expected"), HARDENING.items())
def test_every_planned_directive_is_set_once(service, key, expected):
    assert values(service, "Service", key) == [expected]


def test_the_unit_runs_the_launcher_on_the_system_python(service):
    assert values(service, "Service", "Type") == ["exec"]
    assert values(service, "Service", "ExecStart") == [
        (
            f"/usr/bin/python3 -I {REPO_AT_HOME}/systemd/images/launcher.py "
            f"{REPO_AT_HOME}/systemd/images/models.json"
        )
    ]
    assert values(service, "Service", "WorkingDirectory") == [
        "%h/work/aihub-images/models"
    ]
    assert values(service, "Service", "TimeoutStopSec") == ["10"]


def test_the_paths_the_unit_names_exist_in_the_repo(service):
    [exec_start] = values(service, "Service", "ExecStart")
    for word in exec_start.split():
        if word.startswith(REPO_AT_HOME):
            assert (ROOT / word.removeprefix(REPO_AT_HOME + "/")).is_file(), word
    assert LAUNCHER.is_file() and MODELS.is_file()
    assert (ROOT / "systemd" / "images").is_dir()


def test_a_launcher_bug_stops_after_three_starts_in_a_minute(service):
    assert values(service, "Unit", "StartLimitIntervalSec") == ["60"]
    assert values(service, "Unit", "StartLimitBurst") == ["3"]
    # The launcher exits 0 by itself when idle and the socket starts it again, so nothing restarts it.
    assert values(service, "Service", "Restart") == []
    assert "Install" not in service


def test_the_engine_cannot_outlive_the_launcher(service):
    # KillMode=control-group, the default, ends every process of the unit when the main process exits.
    for key in ("KillMode", "SendSIGKILL", "KillSignal", "RemainAfterExit"):
        assert values(service, "Service", key) == [], key


def test_the_unit_sets_none_of_the_launcher_test_overrides(service):
    # The launcher reads AIHUB_IMAGES_*_S once, so that tests need not wait 5 minutes for the idle exit.
    # Whatever the unit sets would change the real timing.
    assert "AIHUB_IMAGES_" not in SERVICE.read_text()
    assert "AIHUB_IMAGES_" not in SOCKET.read_text()
    assert "AIHUB_IMAGES_" not in MODELS.read_text()
    assert values(service, "Service", "Environment") == ["CUDA_CACHE_DISABLE=1"]
    for key in ("EnvironmentFile", "PassEnvironment", "UnsetEnvironment"):
        assert values(service, "Service", key) == [], key


def test_the_socket_is_private_to_the_user(socket_unit):
    assert socket_unit["Socket"] == [
        ("ListenStream", "%t/aihub-images/engine.sock"),
        ("SocketMode", "0600"),
        ("DirectoryMode", "0700"),
        ("Accept", "no"),
    ]
    assert values(socket_unit, "Install", "WantedBy") == ["sockets.target"]


def _models():
    return json.loads(MODELS.read_text())


def test_models_json_never_asks_the_engine_to_log_prompts():
    # sd-server at -v or --log-level verbose logs prompts, and its stdout and stderr end up in the journal.
    config = _models()
    arguments = config["common_args"] + [
        a for args in config["models"].values() for a in args
    ]
    assert not [
        a for a in arguments if re.fullmatch(r"-v+|--verbose|--log-level(=.*)?", a)
    ]


def test_models_json_listens_on_loopback_only_and_names_models_by_a_short_id():
    config = _models()
    common = config["common_args"]
    assert common[common.index("--listen-ip") + 1] == "127.0.0.1"
    assert common[common.index("--listen-port") + 1] == "18234"
    assert set(config["models"]) == {"zimage", "klein4b"}
    assert all(
        re.fullmatch(r"[a-z0-9-]{1,32}", model_id) for model_id in config["models"]
    )
    for args in config["models"].values():
        assert "--listen-ip" not in args and "--listen-port" not in args


def test_models_json_names_the_binary_and_model_files_relative_to_the_models_directory():
    # WorkingDirectory is %h/work/aihub-images/models, so ../bin/sd-server is %h/work/aihub-images/bin/sd-server.
    config = _models()
    assert config["engine"] == "../bin/sd-server"
    for args in config["models"].values():
        for flag in ("--diffusion-model", "--llm", "--vae"):
            path = Path(args[args.index(flag) + 1])
            assert not path.is_absolute() and ".." not in path.parts, path


def test_systemd_accepts_every_directive():
    # systemd ignores a directive it does not know (a typo removes protection without an error at start).
    result = subprocess.run(
        ["systemd-analyze", "--user", "verify", str(SERVICE), str(SOCKET)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.stdout + result.stderr == ""


def test_the_offline_security_score_is_3_or_lower():
    result = subprocess.run(
        [
            "systemd-analyze",
            "--user",
            "security",
            "--offline=yes",
            "--no-pager",
            str(SERVICE),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=os.environ,
    )
    match = re.search(r"Overall exposure level for \S+: ([0-9.]+) ", result.stdout)
    assert match, result.stdout + result.stderr
    assert float(match[1]) <= 3.0, result.stdout


def test_the_isolation_probe_is_an_executable_script_that_parses():
    # What it finds on a running unit is the gate G1a check; this only keeps the file runnable.
    assert os.access(PROBE, os.X_OK)
    result = subprocess.run(
        ["bash", "-n", str(PROBE)], check=False, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
