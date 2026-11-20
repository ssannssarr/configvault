"""Configuration loading, defaults, and path safety checks."""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from configvault.errors import ConfigError
from configvault.util import atomic_write_text

DEFAULT_CONFIG_PATH = Path("~/.config/configvault.toml")
DEFAULT_SOURCE = Path("~/.config")
DEFAULT_DATA_DIR = Path("~/.local/share/configvault")

# These are deliberately conservative.  A private Git repository is still
# plaintext on GitHub; known credential stores and private-key material should
# not be uploaded by accident.  Users can edit this list explicitly.
DEFAULT_EXCLUDES: tuple[str, ...] = (
    "**/.git",
    "**/.git/**",
    ".git-credentials",
    "**/.git-credentials",
    "git/credentials",
    "gh/hosts.yml",
    "gcloud",
    "gcloud/**",
    "keyrings",
    "keyrings/**",
    "**/id_rsa",
    "**/id_rsa.*",
    "**/id_dsa",
    "**/id_dsa.*",
    "**/id_ecdsa",
    "**/id_ecdsa.*",
    "**/id_ed25519",
    "**/id_ed25519.*",
    "**/*.pem",
    "**/*.key",
    "**/*.p12",
    "**/*.pfx",
    "**/*.kdbx",
    "**/.env",
    "**/.env.*",
    "**/credentials.json",
    "**/token.json",
)

DEFAULT_BRANCH = "main"
DEFAULT_PUBLIC_SUBDIR = "public"


@dataclass
class Config:
    config_path: Path
    source: Path
    data_dir: Path
    public_dir: Path
    public_manifest: Path
    private_remote: str | None = None
    public_remote: str | None = None
    branch: str = DEFAULT_BRANCH
    exclude: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDES))

    # ------------------------------------------------------------------
    # Derived paths
    # ------------------------------------------------------------------
    @property
    def repo_dir(self) -> Path:
        return self.data_dir / "repo"

    @property
    def snapshot_dir(self) -> Path:
        return self.repo_dir / "snapshot"

    @property
    def metadata_path(self) -> Path:
        return self.repo_dir / "metadata.json"

    @property
    def state_dir(self) -> Path:
        return self.data_dir / "state"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def log_path(self) -> Path:
        return self.logs_dir / "configvault.log"

    @property
    def lock_path(self) -> Path:
        return self.state_dir / "lock"

    @property
    def public_state_path(self) -> Path:
        return self.state_dir / "public-manifest.json"

    @property
    def last_snapshot_state_path(self) -> Path:
        return self.state_dir / "last-snapshot.json"

    # ------------------------------------------------------------------
    # Loading / saving
    # ------------------------------------------------------------------
    @classmethod
    def load(cls, path: Path | str | None = None) -> "Config":
        config_path = Path(path).expanduser() if path else DEFAULT_CONFIG_PATH.expanduser()
        config_path = config_path.absolute()

        source = DEFAULT_SOURCE.expanduser().absolute()
        data_dir = DEFAULT_DATA_DIR.expanduser().absolute()
        private_remote: str | None = None
        public_remote: str | None = None
        branch = DEFAULT_BRANCH
        exclude = list(DEFAULT_EXCLUDES)
        public_dir: Path | None = None
        public_manifest: Path | None = None

        if config_path.exists():
            try:
                with config_path.open("rb") as handle:
                    raw = tomllib.load(handle)
            except (OSError, tomllib.TOMLDecodeError) as exc:
                raise ConfigError(f"Cannot read config {config_path}: {exc}") from exc
            if not isinstance(raw, dict):
                raise ConfigError(f"Config {config_path} must contain a TOML table")

            source = _path_value(raw, "source", source)
            data_dir = _path_value(raw, "data_dir", data_dir)
            public_dir = _path_value(raw, "public_dir", data_dir / DEFAULT_PUBLIC_SUBDIR)
            public_manifest = _path_value(
                raw, "public_manifest", source / ".gitinclude"
            )
            private_remote = _optional_str(raw, "private_remote")
            public_remote = _optional_str(raw, "public_remote")
            branch = _str_value(raw, "branch", branch)

            snapshot_table = raw.get("snapshot", {})
            if snapshot_table is not None and not isinstance(snapshot_table, dict):
                raise ConfigError("Config key [snapshot] must be a table")
            if isinstance(snapshot_table, dict) and "exclude" in snapshot_table:
                value = snapshot_table["exclude"]
                if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                    raise ConfigError("snapshot.exclude must be a list of strings")
                exclude = list(value)

        if public_dir is None:
            public_dir = data_dir / DEFAULT_PUBLIC_SUBDIR
        if public_manifest is None:
            public_manifest = source / ".gitinclude"

        cfg = cls(
            config_path=config_path,
            source=source,
            data_dir=data_dir,
            public_dir=public_dir,
            public_manifest=public_manifest,
            private_remote=private_remote,
            public_remote=public_remote,
            branch=branch,
            exclude=exclude,
        )
        cfg.validate_paths()
        return cfg

    def save(self) -> None:
        lines = [
            "# ConfigVault configuration",
            "# Private GitHub repositories are NOT encrypted storage.",
            f"source = {_toml_str(str(self.source))}",
            f"data_dir = {_toml_str(str(self.data_dir))}",
            f"public_dir = {_toml_str(str(self.public_dir))}",
            f"public_manifest = {_toml_str(str(self.public_manifest))}",
            f"private_remote = {_toml_str(self.private_remote or '')}",
            f"public_remote = {_toml_str(self.public_remote or '')}",
            f"branch = {_toml_str(self.branch)}",
            "",
            "[snapshot]",
            "exclude = [",
        ]
        lines.extend(f"  {_toml_str(pattern)}," for pattern in self.exclude)
        lines.append("]")
        atomic_write_text(self.config_path, "\n".join(lines) + "\n", mode=0o600)

    def copy_with(self, **changes: Any) -> "Config":
        return replace(self, **changes)

    # ------------------------------------------------------------------
    # Validation / layout
    # ------------------------------------------------------------------
    def validate_paths(self) -> None:
        source = self.source.resolve(strict=False)
        data = self.data_dir.resolve(strict=False)
        public = self.public_dir.resolve(strict=False)

        if source == Path("/"):
            raise ConfigError("source must not be the filesystem root")
        if source == data:
            raise ConfigError("source and data_dir must be different directories")
        if _is_within(data, source):
            raise ConfigError(
                f"data_dir ({data}) must not be inside source ({source}); "
                "the snapshot repository would back itself up"
            )
        if _is_within(public, source):
            raise ConfigError(
                f"public_dir ({public}) must not be inside source ({source}); "
                "the public repository would be exported into itself"
            )
        if _is_within(source, public):
            raise ConfigError("source must not be inside public_dir")
        if not self.branch or any(ch.isspace() for ch in self.branch):
            raise ConfigError(f"Invalid Git branch name: {self.branch!r}")
        for pattern in self.exclude:
            if not isinstance(pattern, str) or not pattern:
                raise ConfigError("snapshot.exclude entries must be non-empty strings")

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.state_dir, self.logs_dir, self.repo_dir):
            path.mkdir(parents=True, exist_ok=True)

    def require_source(self) -> None:
        if self.source.is_symlink():
            raise ConfigError(
                f"Source must be a real directory, not a symlink: {self.source}"
            )
        if not self.source.exists():
            raise ConfigError(f"Source directory does not exist: {self.source}")
        if not self.source.is_dir():
            raise ConfigError(f"Source is not a directory: {self.source}")

    def require_private_repo(self) -> None:
        if not (self.repo_dir / ".git").exists():
            raise ConfigError(
                f"Private repository is not initialized: {self.repo_dir}. "
                "Run 'configvault init' first."
            )

    def display(self) -> dict[str, str]:
        return {
            "config": str(self.config_path),
            "source": str(self.source),
            "data_dir": str(self.data_dir),
            "private_repo": str(self.repo_dir),
            "snapshot": str(self.snapshot_dir),
            "public_repo": str(self.public_dir),
            "public_manifest": str(self.public_manifest),
            "private_remote": self.private_remote or "(not configured)",
            "public_remote": self.public_remote or "(not configured)",
            "branch": self.branch,
            "logs": str(self.logs_dir),
            "state": str(self.state_dir),
        }


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _path_value(raw: dict[str, Any], key: str, default: Path) -> Path:
    value = raw.get(key)
    if value is None:
        return default
    if not isinstance(value, str) or not value:
        raise ConfigError(f"Config key {key!r} must be a non-empty string")
    return Path(os.path.expanduser(value)).absolute()


def _str_value(raw: dict[str, Any], key: str, default: str) -> str:
    value = raw.get(key, default)
    if not isinstance(value, str):
        raise ConfigError(f"Config key {key!r} must be a string")
    return value


def _optional_str(raw: dict[str, Any], key: str) -> str | None:
    value = raw.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ConfigError(f"Config key {key!r} must be a string")
    return value


def _toml_str(value: str) -> str:
    # JSON string escaping is valid TOML basic-string escaping for our use.
    return json.dumps(value, ensure_ascii=False)
