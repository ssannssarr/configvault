from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from configvault import git
from configvault.config import Config


def write(path: Path, content: str, mode: int | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    if mode is not None:
        os.chmod(path, mode)
    return path


def run_git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    source = tmp_path / "source"
    source.mkdir()
    data = tmp_path / "data"
    config = Config(
        config_path=tmp_path / "configvault.toml",
        source=source,
        data_dir=data,
        public_dir=data / "public",
        public_manifest=source / ".gitinclude",
    )
    config.ensure_dirs()
    config.public_dir.mkdir(parents=True, exist_ok=True)
    git.init_repo(config.repo_dir, "main")
    git.init_repo(config.public_dir, "main")
    config.snapshot_dir.mkdir(parents=True, exist_ok=True)
    config.save()
    return config


@pytest.fixture
def bare_remote(tmp_path: Path) -> Path:
    remote = tmp_path / "private.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(remote)],
        check=True,
        capture_output=True,
        text=True,
    )
    return remote
