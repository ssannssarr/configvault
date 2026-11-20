from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from configvault import git
from configvault import snapshot as snapshot_module
from configvault.config import Config
from configvault.errors import SnapshotError
from configvault.snapshot import SnapshotEngine

from conftest import write


def test_initial_snapshot(cfg: Config) -> None:
    write(cfg.source / "a.txt", "one", 0o600)
    write(cfg.source / "sub" / "b.txt", "two")

    result = SnapshotEngine(cfg).create()

    assert result.changed is True
    assert result.commit == git.head_commit(cfg.repo_dir)
    assert result.diff.added
    assert (cfg.snapshot_dir / "a.txt").read_text() == "one"
    assert (cfg.snapshot_dir / "sub" / "b.txt").read_text() == "two"
    metadata = json.loads(cfg.metadata_path.read_text())
    assert metadata["entries"]["a.txt"]["kind"] == "file"
    assert metadata["entries"]["a.txt"]["mode"] == 0o600
    assert metadata["entries"]["sub"]["kind"] == "dir"


def test_no_changes_creates_no_commit(cfg: Config) -> None:
    write(cfg.source / "a.txt", "one")
    engine = SnapshotEngine(cfg)
    first = engine.create()
    head = git.head_commit(cfg.repo_dir)

    second = engine.create()

    assert first.changed is True
    assert second.changed is False
    assert second.commit is None
    assert git.head_commit(cfg.repo_dir) == head


def test_mtime_only_change_is_snapshotted(cfg: Config) -> None:
    path = write(cfg.source / "a.txt", "one")
    engine = SnapshotEngine(cfg)
    engine.create()
    new_mtime = path.stat().st_mtime_ns + 1_000_000_000
    os.utime(path, ns=(new_mtime, new_mtime))

    result = engine.create()

    assert result.changed is True
    assert result.diff.modified == ["a.txt"]
    metadata = json.loads(cfg.metadata_path.read_text())
    assert metadata["entries"]["a.txt"]["mtime_ns"] == new_mtime


def test_modified_new_and_deleted_files_are_mirrored(cfg: Config) -> None:
    write(cfg.source / "a.txt", "one")
    write(cfg.source / "b.txt", "two")
    engine = SnapshotEngine(cfg)
    engine.create()

    write(cfg.source / "a.txt", "changed")
    write(cfg.source / "c.txt", "three")
    (cfg.source / "b.txt").unlink()
    result = engine.create()

    assert result.diff.modified == ["a.txt"]
    assert result.diff.added == ["c.txt"]
    assert result.diff.removed == ["b.txt"]
    assert (cfg.snapshot_dir / "a.txt").read_text() == "changed"
    assert (cfg.snapshot_dir / "c.txt").read_text() == "three"
    assert not (cfg.snapshot_dir / "b.txt").exists()
    assert git.run_git(
        cfg.repo_dir, ["cat-file", "-e", "HEAD:snapshot/b.txt"], check=False
    ).returncode != 0
    # The deleted file is still recoverable from history.
    old = git.history(cfg.repo_dir)[-1]["commit"]
    assert git.show_file(cfg.repo_dir, old, "snapshot/b.txt") == "two"


def test_custom_commit_message(cfg: Config) -> None:
    write(cfg.source / "a.txt", "one")
    SnapshotEngine(cfg).create("Before Niri changes")
    assert git.history(cfg.repo_dir)[0]["message"] == "Before Niri changes"


def test_symlinks_are_preserved_and_not_followed(cfg: Config) -> None:
    write(cfg.source / "real" / "inside.txt", "inside")
    outside = cfg.source.parent / "outside.txt"
    write(outside, "outside")
    os.symlink("../outside.txt", cfg.source / "outside-link")
    os.symlink("real", cfg.source / "dir-link")

    SnapshotEngine(cfg).create()

    outside_link = cfg.snapshot_dir / "outside-link"
    dir_link = cfg.snapshot_dir / "dir-link"
    assert outside_link.is_symlink()
    assert os.readlink(outside_link) == "../outside.txt"
    assert dir_link.is_symlink()
    assert os.readlink(dir_link) == "real"
    assert not (cfg.snapshot_dir / "outside.txt").exists()
    metadata = json.loads(cfg.metadata_path.read_text())
    assert metadata["entries"]["outside-link"]["target"] == "../outside.txt"
    assert metadata["entries"]["dir-link"]["target"] == "real"


def test_permissions_and_mtimes_are_preserved(cfg: Config) -> None:
    secret = write(cfg.source / "secret.conf", "secret", 0o600)
    executable = write(cfg.source / "run.sh", "#!/bin/sh\n", 0o755)
    private_dir = cfg.source / "private"
    private_dir.mkdir(mode=0o700)
    write(private_dir / "x", "x")
    os.chmod(private_dir, 0o700)
    expected_mtime = secret.stat().st_mtime_ns

    SnapshotEngine(cfg).create()

    assert (cfg.snapshot_dir / "secret.conf").stat().st_mode & 0o777 == 0o600
    assert (cfg.snapshot_dir / "run.sh").stat().st_mode & 0o777 == 0o755
    assert (cfg.snapshot_dir / "private").stat().st_mode & 0o777 == 0o700
    assert (cfg.snapshot_dir / "secret.conf").stat().st_mtime_ns == expected_mtime
    metadata = json.loads(cfg.metadata_path.read_text())
    assert metadata["entries"]["secret.conf"]["mode"] == 0o600
    assert metadata["entries"]["run.sh"]["mode"] == 0o755


def test_empty_snapshot(cfg: Config) -> None:
    engine = SnapshotEngine(cfg)
    first = engine.create()
    assert first.changed is True
    assert first.commit is not None
    metadata = json.loads(cfg.metadata_path.read_text())
    assert metadata["entries"] == {}

    head = git.head_commit(cfg.repo_dir)
    second = engine.create()
    assert second.changed is False
    assert git.head_commit(cfg.repo_dir) == head


def test_interrupted_copy_keeps_previous_snapshot(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(cfg.source / "a.txt", "old")
    engine = SnapshotEngine(cfg)
    engine.create()
    head = git.head_commit(cfg.repo_dir)
    write(cfg.source / "a.txt", "new")

    real_copy = snapshot_module.copy_tree

    def failing_copy(*args, **kwargs):
        real_copy(*args, **kwargs)
        raise SnapshotError("simulated interruption")

    monkeypatch.setattr(snapshot_module, "copy_tree", failing_copy)
    with pytest.raises(SnapshotError, match="simulated interruption"):
        engine.create()

    assert git.head_commit(cfg.repo_dir) == head
    assert (cfg.snapshot_dir / "a.txt").read_text() == "old"
    assert not list((cfg.repo_dir / ".git" / "configvault").glob("staging-*"))

    monkeypatch.setattr(snapshot_module, "copy_tree", real_copy)
    result = engine.create()
    assert result.changed is True
    assert (cfg.snapshot_dir / "a.txt").read_text() == "new"


def test_failed_commit_rolls_back_snapshot(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(cfg.source / "a.txt", "old")
    engine = SnapshotEngine(cfg)
    engine.create()
    old_head = git.head_commit(cfg.repo_dir)
    old_metadata = cfg.metadata_path.read_bytes()
    write(cfg.source / "a.txt", "new")

    def fail_commit(*args, **kwargs):
        raise SnapshotError("simulated commit failure")

    monkeypatch.setattr(git, "stage_and_commit", fail_commit)
    with pytest.raises(SnapshotError, match="simulated commit failure"):
        engine.create()

    assert git.head_commit(cfg.repo_dir) == old_head
    assert (cfg.snapshot_dir / "a.txt").read_text() == "old"
    assert cfg.metadata_path.read_bytes() == old_metadata
    assert git.repo_status(cfg.repo_dir).dirty == []
    assert not list((cfg.repo_dir / ".git" / "configvault").glob("old-*"))


def test_recovery_after_swap_before_commit(cfg: Config) -> None:
    write(cfg.source / "a.txt", "old")
    engine = SnapshotEngine(cfg)
    engine.create()
    old_head = git.head_commit(cfg.repo_dir)
    work = cfg.repo_dir / ".git" / "configvault"
    stale = work / "old-crash"
    os.replace(cfg.snapshot_dir, stale)
    (cfg.snapshot_dir / "sub").mkdir(parents=True)
    write(cfg.snapshot_dir / "a.txt", "uncommitted-new")

    notes = engine.recover()

    assert notes
    assert git.head_commit(cfg.repo_dir) == old_head
    assert (cfg.snapshot_dir / "a.txt").read_text() == "old"
    assert not stale.exists()
    assert git.repo_status(cfg.repo_dir).dirty == []


def test_status_reports_pending_changes(cfg: Config) -> None:
    write(cfg.source / "a.txt", "one")
    write(cfg.source / "b.txt", "two")
    engine = SnapshotEngine(cfg)
    engine.create()

    write(cfg.source / "a.txt", "changed")
    write(cfg.source / "c.txt", "three")
    (cfg.source / "b.txt").unlink()
    status = engine.status()

    assert status.diff.modified == ["a.txt"]
    assert status.diff.added == ["c.txt"]
    assert status.diff.removed == ["b.txt"]
    assert status.previous_commit == git.head_commit(cfg.repo_dir)


def test_default_secret_exclusions(cfg: Config) -> None:
    write(cfg.source / "gh" / "hosts.yml", "token: secret")
    write(cfg.source / ".env", "TOKEN=secret")
    write(cfg.source / "id_ed25519", "private key")
    write(cfg.source / "keyrings" / "default", "keyring")
    write(cfg.source / "normal.conf", "safe")

    SnapshotEngine(cfg).create()

    assert (cfg.snapshot_dir / "normal.conf").exists()
    assert not (cfg.snapshot_dir / "gh" / "hosts.yml").exists()
    assert not (cfg.snapshot_dir / ".env").exists()
    assert not (cfg.snapshot_dir / "id_ed25519").exists()
    assert not (cfg.snapshot_dir / "keyrings").exists()


def test_nested_git_directories_are_not_snapshotted(cfg: Config) -> None:
    write(cfg.source / "project" / "config.txt", "safe")
    write(cfg.source / "project" / ".git" / "config", "sensitive-ish")
    write(cfg.source / ".git" / "HEAD", "ref: refs/heads/main")

    SnapshotEngine(cfg).create()

    assert (cfg.snapshot_dir / "project" / "config.txt").exists()
    assert not (cfg.snapshot_dir / "project" / ".git").exists()
    assert not (cfg.snapshot_dir / ".git").exists()
