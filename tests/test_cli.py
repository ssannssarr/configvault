from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from configvault import git
from configvault.cli import app
from configvault.config import Config
from configvault.snapshot import SnapshotEngine

from conftest import write

runner = CliRunner()


def invoke(cfg: Config, *args: str):
    return runner.invoke(app, ["--config", str(cfg.config_path), *args])


def test_cli_config_and_version(cfg: Config) -> None:
    result = invoke(cfg, "config")
    assert result.exit_code == 0, result.output
    assert str(cfg.source) in result.output
    assert "excludes" in result.output

    version = invoke(cfg, "version")
    assert version.exit_code == 0
    assert version.output.strip() == "0.1.0"


def test_cli_snapshot_status_history_and_diff(cfg: Config) -> None:
    write(cfg.source / "a.txt", "one")
    snap = invoke(cfg, "snapshot", "--no-sync", "--no-export", "-m", "first")
    assert snap.exit_code == 0, snap.output
    assert "snapshot:" in snap.output
    first = git.head_commit(cfg.repo_dir)

    write(cfg.source / "a.txt", "two")
    write(cfg.source / "b.txt", "new")
    second = invoke(cfg, "snapshot", "--no-sync", "--no-export", "-m", "second")
    assert second.exit_code == 0, second.output

    status = invoke(cfg, "status")
    assert status.exit_code == 0, status.output
    assert "pending:" in status.output

    history = invoke(cfg, "history")
    assert history.exit_code == 0
    assert "second" in history.output
    assert "first" in history.output

    diff = invoke(cfg, "diff", first)
    assert diff.exit_code == 0, diff.output
    assert "snapshot/a.txt" in diff.output
    assert "snapshot/b.txt" in diff.output


def test_cli_export_and_restore(cfg: Config) -> None:
    write(cfg.source / "a.txt", "old")
    cfg.public_manifest.write_text("a.txt\n", encoding="utf-8")
    first = SnapshotEngine(cfg).create()
    assert first.commit is not None

    exported = invoke(cfg, "export", "--no-push")
    assert exported.exit_code == 0, exported.output
    assert "exported: 1" in exported.output
    assert (cfg.public_dir / "a.txt").read_text() == "old"

    write(cfg.source / "a.txt", "new")
    SnapshotEngine(cfg).create()

    restored = invoke(cfg, "restore", first.commit, "--yes")
    assert restored.exit_code == 0, restored.output
    assert "restored:" in restored.output
    assert (cfg.source / "a.txt").read_text() == "old"


def test_cli_snapshot_warns_when_export_unavailable(cfg: Config) -> None:
    write(cfg.source / "a.txt", "one")
    cfg.public_manifest.unlink(missing_ok=True)

    result = invoke(cfg, "snapshot")

    assert result.exit_code == 0, result.output
    assert "public export skipped/failed" in result.output


def test_cli_init(cfg: Config) -> None:
    result = invoke(cfg, "init")
    assert result.exit_code == 0, result.output
    assert "private repo:" in result.output
    assert cfg.public_manifest.exists()
