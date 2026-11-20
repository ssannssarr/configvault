from __future__ import annotations

import os
from pathlib import Path

import pytest

from configvault import git
from configvault.config import Config
from configvault.errors import ExportError
from configvault.export import export_public, load_managed

from conftest import run_git, write


def manifest(cfg: Config, text: str) -> None:
    cfg.public_manifest.write_text(text, encoding="utf-8")


def public_paths(cfg: Config) -> set[str]:
    result = git.run_git(
        cfg.public_dir, ["ls-files", "-z"], check=True
    )
    return {path for path in result.stdout.split("\x00") if path}


def test_export_only_explicit_selection(cfg: Config) -> None:
    write(cfg.source / "niri" / "config.kdl", "niri")
    write(cfg.source / "niri" / "secrets" / "token", "secret")
    write(cfg.source / "nvim" / "init.lua", "nvim")
    write(cfg.source / "other" / "file", "other")
    manifest(cfg, "niri/**\nnvim/**\n!niri/secrets/**\n")

    result = export_public(cfg)

    assert result.selected == ["niri/config.kdl", "nvim/init.lua"]
    assert result.deleted == []
    assert (cfg.public_dir / "niri" / "config.kdl").read_text() == "niri"
    assert (cfg.public_dir / "nvim" / "init.lua").read_text() == "nvim"
    assert not (cfg.public_dir / "other").exists()
    assert not (cfg.public_dir / "niri" / "secrets").exists()
    assert load_managed(cfg) == {"niri/config.kdl", "nvim/init.lua"}
    assert public_paths(cfg) == {"niri/config.kdl", "nvim/init.lua"}
    assert result.commit == git.head_commit(cfg.public_dir)


def test_public_deletions_are_mirrored_and_manual_files_kept(cfg: Config) -> None:
    write(cfg.source / "a.txt", "a")
    write(cfg.source / "b.txt", "b")
    manifest(cfg, "*.txt\n")
    first = export_public(cfg)
    write(cfg.public_dir / "manual.txt", "manual")
    write(cfg.public_dir / "manual-dir" / "keep.txt", "keep")

    (cfg.source / "a.txt").unlink()
    second = export_public(cfg)

    assert first.selected == ["a.txt", "b.txt"]
    assert second.deleted == ["a.txt"]
    assert not (cfg.public_dir / "a.txt").exists()
    assert (cfg.public_dir / "b.txt").exists()
    assert (cfg.public_dir / "manual.txt").read_text() == "manual"
    assert (cfg.public_dir / "manual-dir" / "keep.txt").read_text() == "keep"
    assert load_managed(cfg) == {"b.txt"}
    deleted_names = run_git(cfg.public_dir, "show", "--pretty=", "--name-only", "HEAD")
    assert "a.txt" in deleted_names


def test_excluding_a_previously_managed_file_deletes_only_that_file(cfg: Config) -> None:
    write(cfg.source / "a.txt", "a")
    write(cfg.source / "b.txt", "b")
    manifest(cfg, "*.txt\n")
    export_public(cfg)
    write(cfg.public_dir / "manual.txt", "manual")

    manifest(cfg, "a.txt\n")
    result = export_public(cfg)

    assert result.deleted == ["b.txt"]
    assert not (cfg.public_dir / "b.txt").exists()
    assert (cfg.public_dir / "manual.txt").read_text() == "manual"


def test_unmanaged_collision_is_refused(cfg: Config) -> None:
    write(cfg.source / "manual.txt", "source")
    write(cfg.public_dir / "manual.txt", "manual")
    manifest(cfg, "manual.txt\n")
    head = git.head_commit(cfg.public_dir)

    with pytest.raises(ExportError, match="not managed"):
        export_public(cfg)

    assert (cfg.public_dir / "manual.txt").read_text() == "manual"
    assert git.head_commit(cfg.public_dir) == head


def test_missing_state_treats_existing_files_as_manual(cfg: Config) -> None:
    write(cfg.source / "a.txt", "a")
    manifest(cfg, "a.txt\n")
    export_public(cfg)
    cfg.public_state_path.unlink()

    with pytest.raises(ExportError, match="not managed"):
        export_public(cfg)

    assert (cfg.public_dir / "a.txt").read_text() == "a"


def test_staged_manual_changes_are_refused(cfg: Config) -> None:
    write(cfg.source / "a.txt", "one")
    manifest(cfg, "a.txt\n")
    export_public(cfg)
    write(cfg.public_dir / "manual.txt", "manual")
    run_git(cfg.public_dir, "add", "manual.txt")
    write(cfg.source / "a.txt", "two")

    with pytest.raises(ExportError, match="staged changes outside"):
        export_public(cfg)

    assert (cfg.public_dir / "manual.txt").read_text() == "manual"
    assert (cfg.public_dir / "a.txt").read_text() == "one"


def test_managed_path_replaced_by_directory_is_protected(cfg: Config) -> None:
    write(cfg.source / "a.txt", "a")
    manifest(cfg, "a.txt\n")
    export_public(cfg)
    (cfg.public_dir / "a.txt").unlink()
    (cfg.public_dir / "a.txt").mkdir()
    write(cfg.public_dir / "a.txt" / "manual.txt", "manual")

    with pytest.raises(ExportError, match="now a directory"):
        export_public(cfg)

    assert (cfg.public_dir / "a.txt" / "manual.txt").read_text() == "manual"


def test_export_preserves_symlinks_and_modes(cfg: Config) -> None:
    niri = cfg.source / "niri"
    niri.mkdir(mode=0o700)
    write(niri / "config.kdl", "config", 0o600)
    os.symlink("config.kdl", niri / "link")
    os.chmod(niri, 0o700)
    manifest(cfg, "niri/**\n")

    export_public(cfg)

    assert (cfg.public_dir / "niri").stat().st_mode & 0o777 == 0o700
    assert (cfg.public_dir / "niri" / "config.kdl").stat().st_mode & 0o777 == 0o600
    assert (cfg.public_dir / "niri" / "link").is_symlink()
    assert os.readlink(cfg.public_dir / "niri" / "link") == "config.kdl"


def test_empty_export_requires_explicit_approval(cfg: Config) -> None:
    manifest(cfg, "# nothing selected\n")
    with pytest.raises(ExportError, match="empty"):
        export_public(cfg)

    result = export_public(cfg, allow_empty=True)
    assert result.selected == []
    assert result.commit is None


def test_empty_export_with_approval_mirrors_removal(cfg: Config) -> None:
    write(cfg.source / "a.txt", "a")
    manifest(cfg, "a.txt\n")
    export_public(cfg)
    manifest(cfg, "# nothing selected\n")

    result = export_public(cfg, allow_empty=True)

    assert result.deleted == ["a.txt"]
    assert not (cfg.public_dir / "a.txt").exists()
    assert load_managed(cfg) == set()


def test_suspicious_paths_require_explicit_approval(cfg: Config) -> None:
    write(cfg.source / "nvim" / "secrets" / "token", "secret")
    manifest(cfg, "nvim/**\n")

    with pytest.raises(ExportError, match="suspicious"):
        export_public(cfg)

    result = export_public(cfg, allow_suspicious=True)
    assert result.selected == ["nvim/secrets/token"]
    assert (cfg.public_dir / "nvim" / "secrets" / "token").read_text() == "secret"
    assert any("explicit approval" in warning for warning in result.warnings)


def test_secret_directory_name_is_suspicious(cfg: Config) -> None:
    write(cfg.source / "nvim" / "secrets" / "notes.txt", "not obviously a token")
    manifest(cfg, "nvim/**\n")

    with pytest.raises(ExportError, match="suspicious"):
        export_public(cfg)


def test_tool_metadata_is_always_refused(cfg: Config) -> None:
    write(cfg.source / "configvault.toml", "private_remote = 'x'")
    manifest(cfg, "configvault.toml\n")

    with pytest.raises(ExportError, match="tool metadata"):
        export_public(cfg, allow_suspicious=True)


def test_second_export_without_changes_has_no_commit(cfg: Config) -> None:
    write(cfg.source / "a.txt", "a")
    manifest(cfg, "a.txt\n")
    first = export_public(cfg)
    second = export_public(cfg)

    assert first.commit is not None
    assert second.commit is None
    assert second.selected == ["a.txt"]


def test_public_push_failure_preserves_commit(
    cfg: Config, tmp_path: Path
) -> None:
    cfg.public_remote = str(tmp_path / "missing-public.git")
    write(cfg.source / "a.txt", "a")
    manifest(cfg, "a.txt\n")

    result = export_public(cfg)

    assert result.commit is not None
    assert result.push_status == "failed"
    assert result.push_error
    assert (cfg.public_dir / "a.txt").read_text() == "a"
