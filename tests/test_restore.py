from __future__ import annotations

import os
from pathlib import Path

import pytest

from configvault import git
from configvault import restore as restore_module
from configvault.config import Config
from configvault.errors import GitError, RestoreError
from configvault.restore import restore
from configvault.snapshot import SnapshotEngine

from conftest import write


def test_restore_previous_snapshot_with_safety_snapshot(cfg: Config) -> None:
    write(cfg.source / "a.txt", "old", 0o600)
    engine = SnapshotEngine(cfg)
    first = engine.create()
    assert first.commit is not None

    write(cfg.source / "a.txt", "new")
    write(cfg.source / "b.txt", "temporary")
    engine.create()
    write(cfg.source / "unsaved.txt", "not snapshotted")

    result = restore(cfg, first.commit)

    assert result.restored_commit == first.commit
    assert result.safety_changed is True
    assert result.safety_commit is not None
    assert (cfg.source / "a.txt").read_text() == "old"
    assert not (cfg.source / "b.txt").exists()
    assert not (cfg.source / "unsaved.txt").exists()
    assert (cfg.source / "a.txt").stat().st_mode & 0o777 == 0o600
    assert git.history(cfg.repo_dir)[0]["message"].startswith(
        "pre-restore safety snapshot"
    )
    assert git.show_file(cfg.repo_dir, result.safety_commit, "snapshot/unsaved.txt") == "not snapshotted"


def test_restore_recreates_empty_directories_and_symlinks(cfg: Config) -> None:
    empty = cfg.source / "empty-dir"
    empty.mkdir()
    write(cfg.source / "real.txt", "real")
    os.symlink("real.txt", cfg.source / "link")
    engine = SnapshotEngine(cfg)
    first = engine.create()

    (cfg.source / "empty-dir").rmdir()
    (cfg.source / "link").unlink()
    engine.create()

    restore(cfg, first.commit)

    assert (cfg.source / "empty-dir").is_dir()
    assert (cfg.source / "link").is_symlink()
    assert os.readlink(cfg.source / "link") == "real.txt"


def test_restore_rejects_unknown_commit(cfg: Config) -> None:
    write(cfg.source / "a.txt", "one")
    SnapshotEngine(cfg).create()
    head = git.head_commit(cfg.repo_dir)

    with pytest.raises(GitError):
        restore(cfg, "does-not-exist")

    assert git.head_commit(cfg.repo_dir) == head
    assert (cfg.source / "a.txt").read_text() == "one"


def test_restore_aborts_when_safety_snapshot_fails(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(cfg.source / "a.txt", "one")
    engine = SnapshotEngine(cfg)
    first = engine.create()
    write(cfg.source / "a.txt", "two")

    def fail(*args, **kwargs):
        raise restore_module.SnapshotError("simulated safety failure")

    monkeypatch.setattr(SnapshotEngine, "create_locked", fail)
    with pytest.raises(RestoreError, match="Safety snapshot failed"):
        restore(cfg, first.commit)

    assert (cfg.source / "a.txt").read_text() == "two"


def test_restore_staging_failure_leaves_live_config_untouched(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(cfg.source / "a.txt", "one")
    engine = SnapshotEngine(cfg)
    first = engine.create()
    write(cfg.source / "a.txt", "two")

    def fail_validation(*args, **kwargs):
        raise RestoreError("simulated validation failure")

    monkeypatch.setattr(restore_module, "_validate_staged_tree", fail_validation)
    with pytest.raises(RestoreError, match="simulated validation failure"):
        restore(cfg, first.commit)

    assert (cfg.source / "a.txt").read_text() == "two"
    assert not list(cfg.source.parent.glob(".source.configvault-*"))


def test_restore_swap_failure_rolls_back(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(cfg.source / "a.txt", "one")
    engine = SnapshotEngine(cfg)
    first = engine.create()
    write(cfg.source / "a.txt", "two")
    engine.create()

    real_replace = os.replace

    def failing_replace(src, dst):
        source = Path(src)
        if source.name.startswith(".source.configvault-restore-"):
            raise OSError("simulated swap failure")
        return real_replace(src, dst)

    monkeypatch.setattr(restore_module.os, "replace", failing_replace)
    with pytest.raises(RestoreError, match="previous configuration was put back"):
        restore(cfg, first.commit)

    assert (cfg.source / "a.txt").read_text() == "two"
    assert not list(cfg.source.parent.glob(".source.configvault-pre-restore-*"))
    assert not list(cfg.source.parent.glob(".source.configvault-restore-*"))


def test_restore_refuses_non_snapshot_commit(cfg: Config) -> None:
    write(cfg.source / "a.txt", "one")
    SnapshotEngine(cfg).create()
    (cfg.repo_dir / "random.txt").write_text("not a snapshot")
    commit = git.stage_and_commit(cfg.repo_dir, ["random.txt"], "unrelated commit")
    assert commit is not None
    head = git.head_commit(cfg.repo_dir)

    with pytest.raises(RestoreError, match="not a ConfigVault snapshot"):
        restore(cfg, commit)

    assert git.head_commit(cfg.repo_dir) == head
    assert (cfg.source / "a.txt").read_text() == "one"
