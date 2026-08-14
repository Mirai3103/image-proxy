"""Native launcher for running the image proxy under mitmdump."""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess
import sys
from collections.abc import Sequence

from image_proxy.config import AppConfig, ConfigError, load_config


def build_mitmdump_command(
    config_path: Path,
    config: AppConfig,
    executable: str,
    script_path: Path,
) -> list[str]:
    """Build the mitmdump command from already validated configuration."""
    return [
        executable,
        "--listen-host",
        config.proxy.host,
        "--listen-port",
        str(config.proxy.port),
        "--set",
        f"image_proxy_config={config_path.resolve()}",
        "-s",
        str(script_path.resolve()),
    ]


def main(argv: Sequence[str] | None = None) -> int:
    """Validate configuration and run mitmdump."""
    parser = argparse.ArgumentParser(prog="image-proxy")
    parser.add_argument("--config", required=True, help="Path to config YAML")
    args = parser.parse_args(argv)

    config_path = Path(args.config).resolve()
    try:
        config = load_config(config_path)
        executable = shutil.which("mitmdump")
        if executable is None:
            print(
                "mitmdump executable not found; run `uv sync --all-extras` "
                "to install project dependencies.",
                file=sys.stderr,
            )
            return 2
        script_path = Path(__file__).with_name("mitm_script.py")
        command = build_mitmdump_command(config_path, config, executable, script_path)
        return subprocess.run(command, check=False).returncode
    except (ConfigError, FileNotFoundError) as exc:
        print(exc, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
