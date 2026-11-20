"""Initialization of configuration, repositories, and manifests."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from configvault import git
from configvault.config import Config
from configvault.errors import ConfigError
from configvault.safety import remote_has_credentials
from configvault.systemd import install_units
from configvault.util import append_log, atomic_write_text

MANIFEST_TEMPLATE = """\
# ConfigVault public export manifest
#
# Patterns are relative to ~/.config.  Only explicitly selected paths are
# published.  Exclusions start with '!' and always win.
#
# Examples:
#   niri/**
#   nvim/**
#   alacritty/**
#   fuzzel/**
#   zsh/**
#   !nvim/**/secrets/**
#
# Uncomment and edit the paths you want to publish.  An empty manifest is
# refused by 'configvault export'.
"""


@dataclass
class InitResult:
    config_created: bool
    private_repo: Path
    public_repo: Path
    manifest_created: bool
    warnings: list[str] = field(default_factory=list)
    unit_paths: list[Path] = field(default_factory=list)


def initialize(
    cfg: Config,
    *,
    private_remote: str | None = None,
    public_remote: str | None = None,
    force_config: bool = False,
    install_systemd: bool = False,
) -> InitResult:
    cfg.require_source()
    cfg.validate_paths()

    warnings: list[str] = []
    config_existed = cfg.config_path.exists()
    if private_remote is not None:
        cfg.private_remote = private_remote or None
    if public_remote is not None:
        cfg.public_remote = public_remote or None

    for label, url in (
        ("private", cfg.private_remote),
        ("public", cfg.public_remote),
    ):
        if remote_has_credentials(url):
            warnings.append(
                f"The {label} remote URL appears to contain embedded credentials. "
                "Use an SSH remote or a credential helper instead."
            )

    cfg.ensure_dirs()
    cfg.public_dir.mkdir(parents=True, exist_ok=True)

    if not config_existed or force_config:
        cfg.save()
    elif private_remote is not None or public_remote is not None:
        cfg.save()

    git.init_repo(cfg.repo_dir, cfg.branch, cfg.private_remote)
    git.init_repo(cfg.public_dir, cfg.branch, cfg.public_remote)
    cfg.snapshot_dir.mkdir(parents=True, exist_ok=True)

    manifest_created = False
    if not cfg.public_manifest.exists():
        cfg.public_manifest.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(cfg.public_manifest, MANIFEST_TEMPLATE, mode=0o644)
        manifest_created = True

    unit_paths: list[Path] = []
    if install_systemd:
        unit_paths = install_units(cfg)

    append_log(cfg.log_path, f"init private={cfg.repo_dir} public={cfg.public_dir}")
    return InitResult(
        config_created=not config_existed or force_config,
        private_repo=cfg.repo_dir,
        public_repo=cfg.public_dir,
        manifest_created=manifest_created,
        warnings=warnings,
        unit_paths=unit_paths,
    )


def ensure_not_real_source_for_tests(cfg: Config) -> None:
    """Guard used by tests; production code does not call this."""
    if cfg.source == Path.home() / ".config":
        raise ConfigError("refusing to use the real ~/.config in tests")
