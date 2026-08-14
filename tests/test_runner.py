from pathlib import Path
import re

from image_proxy.config import load_config
from image_proxy import runner
from image_proxy.runner import build_mitmdump_command, domain_glob_to_allow_hosts_regex


def test_domain_globs_become_anchored_host_port_regexes() -> None:
    exact = domain_glob_to_allow_hosts_regex("zs.wtcdn.xyz")
    wildcard = domain_glob_to_allow_hosts_regex("*.cdn.test")

    assert re.search(exact, "zs.wtcdn.xyz:443")
    assert not re.search(exact, "evil-zs.wtcdn.xyz:443")
    assert not re.search(exact, "zs.wtcdn.xyz.evil:443")
    assert re.search(wildcard, "img.cdn.test:8443")
    assert not re.search(wildcard, "img.cdn.test.evil:8443")


def test_build_command_uses_listener_allowlist_and_absolute_paths(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        Path("config.example.yaml")
        .read_text()
        .replace(
            '    - "*.example-cdn.com"',
            '    - "zs.wtcdn.xyz"\n    - "*.example-cdn.com"',
        )
    )
    config = load_config(config_path)
    command = build_mitmdump_command(
        config_path, config, "/venv/bin/mitmdump", Path("/package/mitm_script.py")
    )
    assert command == [
        "/venv/bin/mitmdump",
        "--listen-host",
        "0.0.0.0",
        "--listen-port",
        "8080",
        "--allow-hosts",
        domain_glob_to_allow_hosts_regex("zs.wtcdn.xyz"),
        "--allow-hosts",
        domain_glob_to_allow_hosts_regex("*.example-cdn.com"),
        "--set",
        f"image_proxy_config={config_path.resolve()}",
        "-s",
        "/package/mitm_script.py",
    ]


def test_main_reports_invalid_config(capsys) -> None:
    assert runner.main(["--config", "missing.yaml"]) == 2
    assert "missing.yaml" in capsys.readouterr().err


def test_main_reports_missing_mitmdump(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(Path("config.example.yaml").read_text())
    monkeypatch.setattr(runner.shutil, "which", lambda _: None)
    assert runner.main(["--config", str(config_path)]) == 2
    assert "uv sync --all-extras" in capsys.readouterr().err


def test_main_runs_mitmdump_command_and_returns_exit_code(
    tmp_path: Path, monkeypatch
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(Path("config.example.yaml").read_text())
    run_calls: list[tuple[list[str], bool]] = []

    class CompletedProcess:
        returncode = 7

    def run_command(command: list[str], *, check: bool) -> CompletedProcess:
        run_calls.append((command, check))
        return CompletedProcess()

    monkeypatch.setattr(runner.shutil, "which", lambda _: "/venv/bin/mitmdump")
    monkeypatch.setattr(runner.subprocess, "run", run_command)

    assert runner.main(["--config", str(config_path)]) == 7
    assert run_calls == [
        (
            [
                "/venv/bin/mitmdump",
                "--listen-host",
                "0.0.0.0",
                "--listen-port",
                "8080",
                "--allow-hosts",
                domain_glob_to_allow_hosts_regex("*.example-cdn.com"),
                "--set",
                f"image_proxy_config={config_path.resolve()}",
                "-s",
                str(Path(runner.__file__).with_name("mitm_script.py").resolve()),
            ],
            False,
        )
    ]


def test_main_reports_disappearing_mitmdump(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(Path("config.example.yaml").read_text())

    def run_command(command: list[str], *, check: bool):
        raise FileNotFoundError("mitmdump disappeared")

    monkeypatch.setattr(runner.shutil, "which", lambda _: "/venv/bin/mitmdump")
    monkeypatch.setattr(runner.subprocess, "run", run_command)

    assert runner.main(["--config", str(config_path)]) == 2
    assert "mitmdump disappeared" in capsys.readouterr().err


def test_main_returns_130_on_keyboard_interrupt(
    tmp_path: Path, monkeypatch
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(Path("config.example.yaml").read_text())

    def run_command(command: list[str], *, check: bool):
        raise KeyboardInterrupt

    monkeypatch.setattr(runner.shutil, "which", lambda _: "/venv/bin/mitmdump")
    monkeypatch.setattr(runner.subprocess, "run", run_command)

    assert runner.main(["--config", str(config_path)]) == 130
