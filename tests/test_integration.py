from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from configvault import git
from configvault.cli import app
from configvault.config import Config
from configvault.init import initialize
from configvault.snapshot import SnapshotEngine

from conftest import run_git, write

runner = CliRunner()


def test_full_private_and_public_workflow(
    cfg: Config, bare_remote: Path, tmp_path: Path
) -> None:
    public_remote = tmp_path / "public.git"
    run_git(tmp_path, "init", "--bare", "-b", "main", str(public_remote))
    initialize(
        cfg,
        private_remote=str(bare_remote),
        public_remote=str(public_remote),
    )

    write(cfg.source / "niri" / "config.kdl", "niri-v1")
    write(cfg.source / "nvim" / "init.lua", "nvim-v1")
    write(cfg.source / "nvim" / "secrets" / "token", "do-not-publish")
    write(cfg.source / "other" / "private.txt", "private-only")
    cfg.public_manifest.write_text(
        "niri/**\nnvim/**\n!nvim/**/secrets/**\n", encoding="utf-8"
    )

    first = runner.invoke(
        app, ["--config", str(cfg.config_path), "snapshot"]
    )
    assert first.exit_code == 0, first.output
    first_commit = git.head_commit(cfg.repo_dir)
    assert first_commit is not None

    private_remote_head = run_git(bare_remote, "rev-parse", "refs/heads/main").strip()
    assert private_remote_head == first_commit
    public_remote_head = run_git(public_remote, "rev-parse", "refs/heads/main").strip()
    assert public_remote_head == git.head_commit(cfg.public_dir)
    assert (cfg.public_dir / "niri" / "config.kdl").read_text() == "niri-v1"
    assert (cfg.public_dir / "nvim" / "init.lua").read_text() == "nvim-v1"
    assert not (cfg.public_dir / "nvim" / "secrets").exists()
    assert not (cfg.public_dir / "other").exists()
    assert (cfg.snapshot_dir / "other" / "private.txt").read_text() == "private-only"

    # A manually maintained public file survives later exports.
    write(cfg.public_dir / "manual.txt", "manual")

    write(cfg.source / "niri" / "config.kdl", "niri-v2")
    write(cfg.source / "nvim" / "init.lua", "nvim-v2")
    (cfg.source / "other" / "private.txt").unlink()
    second = runner.invoke(
        app,
        ["--config", str(cfg.config_path), "snapshot", "-m", "second snapshot"],
    )
    assert second.exit_code == 0, second.output
    assert (cfg.public_dir / "niri" / "config.kdl").read_text() == "niri-v2"
    assert (cfg.public_dir / "manual.txt").read_text() == "manual"
    assert not (cfg.snapshot_dir / "other" / "private.txt").exists()

    write(cfg.source / "unsaved.txt", "not snapshotted")
    restored = runner.invoke(
        app,
        ["--config", str(cfg.config_path), "restore", first_commit, "--yes"],
    )
    assert restored.exit_code == 0, restored.output
    assert (cfg.source / "niri" / "config.kdl").read_text() == "niri-v1"
    assert (cfg.source / "other" / "private.txt").read_text() == "private-only"
    assert git.history(cfg.repo_dir)[0]["message"].startswith(
        "pre-restore safety snapshot"
    )


def test_public_export_does_not_require_private_history(cfg: Config) -> None:
    write(cfg.source / "niri" / "config.kdl", "niri")
    cfg.public_manifest.write_text("niri/**\n", encoding="utf-8")

    from configvault.export import export_public

    result = export_public(cfg)

    assert result.commit is not None
    assert (cfg.public_dir / "niri" / "config.kdl").read_text() == "niri"
    assert git.head_commit(cfg.repo_dir) is None
