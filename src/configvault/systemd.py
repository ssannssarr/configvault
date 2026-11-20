"""systemd user service and timer generation (no daemon)."""

from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

from configvault.config import Config
from configvault.util import atomic_write_text

SERVICE = """\
[Unit]
Description=ConfigVault snapshot
Documentation=file:{readme}
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart={exec_start}
Nice=10
IOSchedulingClass=idle
"""

TIMER = """\
[Unit]
Description=Daily ConfigVault snapshot

[Timer]
OnCalendar=*-*-* 03:00:00
Persistent=true
RandomizedDelaySec=15m
Unit=configvault.service

[Install]
WantedBy=timers.target
"""


def systemd_user_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base).expanduser() / "systemd" / "user"
    return Path.home() / ".config" / "systemd" / "user"


def _systemd_quote(argument: str) -> str:
    # systemd uses double-quote/backslash quoting, not shell single quotes.
    escaped = argument.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
    if re.search(r'[\s"\\]', argument):
        return f'"{escaped}"'
    return escaped


def executable_command() -> str:
    found = shutil.which("configvault")
    if found:
        args = [found, "snapshot"]
    else:
        args = [sys.executable, "-m", "configvault", "snapshot"]
    return " ".join(_systemd_quote(arg) for arg in args)


def render_units() -> tuple[str, str]:
    readme = Path(__file__).resolve().parent.parent.parent / "README.md"
    service = SERVICE.format(
        readme=readme,
        exec_start=executable_command(),
    )
    return service, TIMER


def install_units(cfg: Config) -> list[Path]:
    service, timer = render_units()
    target = systemd_user_dir()
    target.mkdir(parents=True, exist_ok=True)
    service_path = target / "configvault.service"
    timer_path = target / "configvault.timer"
    atomic_write_text(service_path, service, mode=0o644)
    atomic_write_text(timer_path, timer, mode=0o644)
    return [service_path, timer_path]
