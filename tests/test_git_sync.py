from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from configvault import git
from configvault.config import Config
from configvault.errors import DivergenceError, GitError
from configvault.snapshot import SnapshotEngine

from conftest import run_git, write


def _clone(remote: Path, target: Path) -> Path:
    subprocess.run(
        ["git", "clone", str(remote), str(target)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Remote Writer"],
        cwd=target,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "remote@example.invalid"],
        cwd=target,
        check=True,
        capture_output=True,
        text=True,
    )
    return target


def test_push_and_up_to_date(cfg: Config, bare_remote: Path) -> None:
    cfg.private_remote = str(bare_remote)
    write(cfg.source / "a.txt", "one")
    SnapshotEngine(cfg).create()
    local = git.head_commit(cfg.repo_dir)

    first = git.sync(cfg.repo_dir, cfg.private_remote, cfg.branch)
    second = git.sync(cfg.repo_dir, cfg.private_remote, cfg.branch)

    assert first.status == "pushed"
    assert second.status == "up-to-date"
    assert git.head_commit(cfg.repo_dir) == local
    remote_head = run_git(bare_remote, "rev-parse", "refs/heads/main").strip()
    assert remote_head == local


def test_network_failure_preserves_local_commit(
    cfg: Config, tmp_path: Path
) -> None:
    cfg.private_remote = str(tmp_path / "missing-remote.git")
    write(cfg.source / "a.txt", "one")
    result = SnapshotEngine(cfg).create()
    local = git.head_commit(cfg.repo_dir)
    assert result.commit == local

    with pytest.raises(GitError, match="local commit was preserved"):
        git.sync(cfg.repo_dir, cfg.private_remote, cfg.branch)

    assert git.head_commit(cfg.repo_dir) == local
    assert (cfg.snapshot_dir / "a.txt").read_text() == "one"


def test_remote_ahead_is_refused(cfg: Config, bare_remote: Path, tmp_path: Path) -> None:
    cfg.private_remote = str(bare_remote)
    write(cfg.source / "a.txt", "one")
    SnapshotEngine(cfg).create()
    git.sync(cfg.repo_dir, cfg.private_remote, cfg.branch)

    clone = _clone(bare_remote, tmp_path / "clone")
    (clone / "remote-only.txt").write_text("remote")
    run_git(clone, "add", "remote-only.txt")
    run_git(clone, "commit", "-m", "remote-only commit")
    run_git(clone, "push", "origin", "main")
    remote_before = run_git(bare_remote, "rev-parse", "refs/heads/main").strip()
    local = git.head_commit(cfg.repo_dir)

    with pytest.raises(DivergenceError, match="not present locally"):
        git.sync(cfg.repo_dir, cfg.private_remote, cfg.branch)

    assert git.head_commit(cfg.repo_dir) == local
    assert run_git(bare_remote, "rev-parse", "refs/heads/main").strip() == remote_before


def test_divergent_histories_are_refused_without_force(
    cfg: Config, bare_remote: Path, tmp_path: Path
) -> None:
    cfg.private_remote = str(bare_remote)
    write(cfg.source / "a.txt", "one")
    SnapshotEngine(cfg).create()
    git.sync(cfg.repo_dir, cfg.private_remote, cfg.branch)

    clone = _clone(bare_remote, tmp_path / "clone")
    (clone / "remote-only.txt").write_text("remote")
    run_git(clone, "add", "remote-only.txt")
    run_git(clone, "commit", "-m", "remote-only commit")
    run_git(clone, "push", "origin", "main")
    remote_before = run_git(bare_remote, "rev-parse", "refs/heads/main").strip()

    write(cfg.source / "b.txt", "local")
    SnapshotEngine(cfg).create()
    local = git.head_commit(cfg.repo_dir)

    with pytest.raises(DivergenceError, match="diverged"):
        git.sync(cfg.repo_dir, cfg.private_remote, cfg.branch)

    assert git.head_commit(cfg.repo_dir) == local
    assert run_git(bare_remote, "rev-parse", "refs/heads/main").strip() == remote_before


def test_history_and_diff(cfg: Config) -> None:
    write(cfg.source / "a.txt", "one")
    engine = SnapshotEngine(cfg)
    first = engine.create("first")
    write(cfg.source / "a.txt", "two")
    write(cfg.source / "b.txt", "new")
    second = engine.create("second")

    rows = git.history(cfg.repo_dir)
    assert [row["message"] for row in rows[:2]] == ["second", "first"]
    assert rows[0]["commit"] == second.commit

    changes = git.diff_name_status(cfg.repo_dir, first.commit or "HEAD~1", "HEAD")
    assert ("M", "snapshot/a.txt") in changes
    assert ("A", "snapshot/b.txt") in changes
    patch = git.diff_patch(cfg.repo_dir, first.commit or "HEAD~1", "HEAD")
    assert "snapshot/a.txt" in patch
