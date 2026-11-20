from __future__ import annotations

import os
from pathlib import Path

import pytest

from configvault.errors import ManifestError
from configvault.manifest import compile_pattern, parse_manifest, select_entries

from conftest import write


@pytest.mark.parametrize(
    ("pattern", "matching", "not_matching"),
    [
        ("niri/**", ["niri/config", "niri/sub/deep"], ["niri", "other/niri/x"]),
        ("**/*.conf", ["foo.conf", "a/b/foo.conf"], ["foo.txt", "a/foo.conf.bak"]),
        ("a/**/b", ["a/b", "a/x/b", "a/x/y/b"], ["a/x/c", "b"]),
        ("a/?", ["a/x"], ["a/xy", "a/"]),
        ("foo/", ["foo/x", "foo/deep/x"], ["foo", "foobar/x"]),
        ("**/id_rsa", ["id_rsa", "a/b/id_rsa"], ["a/id_rsa.pub"]),
    ],
)
def test_glob_patterns(pattern: str, matching: list[str], not_matching: list[str]) -> None:
    compiled = compile_pattern(pattern)
    for path in matching:
        assert compiled.matches(path), (pattern, path)
    for path in not_matching:
        assert not compiled.matches(path), (pattern, path)


def test_manifest_parsing_comments_and_exclusions(tmp_path: Path) -> None:
    manifest = tmp_path / ".gitinclude"
    manifest.write_text(
        "# comment\n\nniri/**\n!niri/**/secrets/**\nnvim/**\n",
        encoding="utf-8",
    )
    patterns = parse_manifest(manifest)

    assert patterns.is_selected("niri/config.kdl")
    assert not patterns.is_selected("niri/secrets/token")
    assert patterns.is_selected("nvim/init.lua")
    assert not patterns.is_selected("other/file")


def test_exclusions_override_inclusions_regardless_of_order(tmp_path: Path) -> None:
    manifest = tmp_path / ".gitinclude"
    manifest.write_text("!nvim/secrets/**\nnvim/**\n", encoding="utf-8")
    patterns = parse_manifest(manifest)

    assert patterns.is_selected("nvim/init.lua")
    assert not patterns.is_selected("nvim/secrets/token")


def test_directory_pattern_selects_subtree(tmp_path: Path) -> None:
    manifest = tmp_path / ".gitinclude"
    manifest.write_text("niri\n", encoding="utf-8")
    patterns = parse_manifest(manifest)

    assert patterns.is_selected("niri")
    assert patterns.is_selected("niri/sub/deep/file")


@pytest.mark.parametrize(
    "line",
    [
        "/absolute/**",
        "../escape/**",
        "a/../b/**",
        "a**/b",
        "a//b",
        "a\\b",
        "!",
    ],
)
def test_invalid_manifest_patterns(tmp_path: Path, line: str) -> None:
    manifest = tmp_path / ".gitinclude"
    manifest.write_text(line + "\n", encoding="utf-8")
    with pytest.raises(ManifestError):
        parse_manifest(manifest)


def test_empty_manifest_refused_unless_explicit(tmp_path: Path) -> None:
    manifest = tmp_path / ".gitinclude"
    manifest.write_text("# only comments\n", encoding="utf-8")
    with pytest.raises(ManifestError):
        parse_manifest(manifest)
    patterns = parse_manifest(manifest, require_includes=False)
    assert patterns.includes == []


def test_select_entries_skips_git_and_does_not_follow_symlinks(tmp_path: Path) -> None:
    source = tmp_path / "source"
    write(source / "niri" / "config", "config")
    write(source / "niri" / ".git" / "config", "git")
    write(source / "outside.txt", "outside")
    os.symlink("../outside.txt", source / "niri" / "link")
    os.symlink("..", source / "niri" / "dir-link")
    manifest = tmp_path / ".gitinclude"
    manifest.write_text("niri/**\n", encoding="utf-8")

    selected, warnings = select_entries(source, parse_manifest(manifest))
    rels = {item.rel for item in selected}

    assert "niri/config" in rels
    assert "niri/link" in rels
    assert "niri/dir-link" in rels
    assert "niri/.git/config" not in rels
    assert "outside.txt" not in rels
    assert not any(rel.startswith("niri/link/") for rel in rels)
    assert warnings == []
