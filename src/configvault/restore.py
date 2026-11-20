"""Safe restore: validate, snapshot, stage, validate, swap, roll back."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from configvault import git
from configvault.config import Config
from configvault.errors import RestoreError, SnapshotError
from configvault.fs import (
    TreeDiff,
    TreeState,
    apply_metadata,
    compare_states,
    copy_tree,
    remove_tree,
    scan_tree,
)
from configvault.snapshot import SnapshotEngine
from configvault.util import Lock, append_log


@dataclass
class RestoreResult:
    restored_commit: str
    safety_commit: str | None
    safety_changed: bool
    warnings: list[str] = field(default_factory=list)
    backup_removed: bool = True


def restore(cfg: Config, ref: str) -> RestoreResult:
    cfg.require_source()
    cfg.require_private_repo()
    if cfg.source.is_symlink():
        raise RestoreError(
            f"Refusing to restore over a symlinked source directory: {cfg.source}"
        )
    cfg.state_dir.mkdir(parents=True, exist_ok=True)

    try:
        with Lock(cfg.lock_path):
            return _restore_locked(cfg, ref)
    except RuntimeError as exc:
        raise RestoreError(str(exc)) from exc


def _restore_locked(cfg: Config, ref: str) -> RestoreResult:
    commit = git.validate_commit(cfg.repo_dir, ref)
    metadata = _load_metadata(cfg, commit)
    _validate_relpaths(metadata)

    engine = SnapshotEngine(cfg)
    try:
        safety = engine.create_locked(
            f"pre-restore safety snapshot before {commit[:12]}"
        )
    except SnapshotError as exc:
        raise RestoreError(
            f"Safety snapshot failed; restore aborted without touching live config: {exc}"
        ) from exc

    worktree = cfg.state_dir / f"restore-wt-{uuid.uuid4().hex}"
    staging = cfg.source.parent / f".{cfg.source.name}.configvault-restore-{uuid.uuid4().hex}"
    backup = cfg.source.parent / f".{cfg.source.name}.configvault-pre-restore-{uuid.uuid4().hex}"
    warnings: list[str] = list(safety.warnings)
    backup_removed = True

    try:
        git.worktree_add(cfg.repo_dir, worktree, commit)
        source_snapshot = worktree / "snapshot"
        if source_snapshot.exists():
            copy_tree(source_snapshot, staging, warnings=warnings)
        else:
            staging.mkdir(parents=False, exist_ok=False)

        _materialize_empty_directories(staging, metadata)
        _validate_staged_tree(staging, metadata, warnings)

        apply_metadata(staging, metadata, warnings=warnings)
        final_state = scan_tree(staging, warnings=warnings, strict=True)
        diff = compare_states(metadata, final_state)
        if not diff.empty:
            raise RestoreError(
                "Restored staging tree does not match snapshot metadata: "
                + _format_diff(diff)
            )

        if backup.exists():
            raise RestoreError(f"Restore backup path already exists: {backup}")
        os.replace(cfg.source, backup)
        try:
            os.replace(staging, cfg.source)
        except BaseException as exc:
            try:
                os.replace(backup, cfg.source)
            except OSError as rollback_exc:
                raise RestoreError(
                    "Restore failed and automatic rollback also failed. "
                    f"Previous configuration is at {backup}. Error: {rollback_exc}"
                ) from exc
            raise RestoreError(
                f"Restore failed; previous configuration was put back: {exc}"
            ) from exc

        try:
            remove_tree(backup)
        except OSError as exc:
            backup_removed = False
            warnings.append(f"could not remove restore backup {backup}: {exc}")

        append_log(
            cfg.log_path,
            f"restore {commit[:12]} safety={safety.commit or '(none)'}",
        )
        return RestoreResult(
            restored_commit=commit,
            safety_commit=safety.commit,
            safety_changed=safety.changed,
            warnings=warnings,
            backup_removed=backup_removed,
        )
    except SnapshotError as exc:
        raise RestoreError(f"Restore staging failed: {exc}") from exc
    finally:
        remove_tree(staging)
        git.worktree_remove(cfg.repo_dir, worktree)
        if worktree.exists():
            with contextlib.suppress(OSError):
                shutil.rmtree(worktree)
            git.run_git(cfg.repo_dir, ["worktree", "prune"], check=False)


def _load_metadata(cfg: Config, commit: str) -> TreeState:
    raw = git.show_file(cfg.repo_dir, commit, "metadata.json")
    if raw is None:
        raise RestoreError(
            f"Commit {commit[:12]} is not a ConfigVault snapshot "
            "(metadata.json is missing)"
        )
    if not _is_snapshot_commit(cfg, commit):
        raise RestoreError(
            f"Commit {commit[:12]} is not a ConfigVault snapshot commit "
            "(it does not change snapshot/ or metadata.json)"
        )
    try:
        data = json.loads(raw)
        return TreeState.from_json(data)
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RestoreError(f"Invalid snapshot metadata at {commit[:12]}: {exc}") from exc


def _is_snapshot_commit(cfg: Config, commit: str) -> bool:
    parent = git.run_git(
        cfg.repo_dir, ["rev-parse", "--verify", f"{commit}^"], check=False
    )
    if parent.returncode != 0:
        return True
    changed = git.run_git(
        cfg.repo_dir,
        ["diff-tree", "--no-commit-id", "--name-only", "-r", commit],
        check=False,
    )
    if changed.returncode != 0:
        return False
    paths = [line for line in changed.stdout.splitlines() if line]
    return any(path == "metadata.json" or path.startswith("snapshot/") for path in paths)


def _validate_relpaths(state: TreeState) -> None:
    for rel in state.entries:
        if not rel or "\x00" in rel:
            raise RestoreError(f"Invalid path in snapshot metadata: {rel!r}")
        path = PurePosixPath(rel)
        if path.is_absolute() or any(part in ("..", "") for part in path.parts):
            raise RestoreError(f"Unsafe path in snapshot metadata: {rel!r}")


def _materialize_empty_directories(staging: Path, metadata: TreeState) -> None:
    directories = [
        (rel, entry)
        for rel, entry in metadata.entries.items()
        if entry.kind == "dir"
    ]
    for rel, _entry in sorted(directories, key=lambda item: item[0].count("/")):
        path = staging / rel
        if path.exists() and not path.is_dir():
            raise RestoreError(f"Cannot create directory over non-directory: {path}")
        path.mkdir(parents=True, exist_ok=True)


def _validate_staged_tree(
    staging: Path, metadata: TreeState, warnings: list[str]
) -> None:
    actual = scan_tree(staging, warnings=warnings, strict=True)
    extra = sorted(set(actual.entries) - set(metadata.entries))
    missing = sorted(set(metadata.entries) - set(actual.entries))
    if extra:
        raise RestoreError(
            "Restored staging tree contains unexpected paths: " + ", ".join(extra[:10])
        )
    if missing:
        raise RestoreError(
            "Restored staging tree is missing paths: " + ", ".join(missing[:10])
        )
    for rel, expected in metadata.entries.items():
        found = actual.entries[rel]
        if expected.kind != found.kind:
            raise RestoreError(
                f"Restored path {rel!r} has kind {found.kind}, expected {expected.kind}"
            )
        if expected.kind == "file":
            if expected.sha256 != found.sha256 or expected.size != found.size:
                raise RestoreError(f"Restored file content mismatch: {rel}")
        elif expected.kind == "symlink":
            if expected.target != found.target:
                raise RestoreError(f"Restored symlink target mismatch: {rel}")


def _format_diff(diff: TreeDiff) -> str:
    parts: list[str] = []
    if diff.added:
        parts.append(f"added={len(diff.added)}")
    if diff.removed:
        parts.append(f"removed={len(diff.removed)}")
    if diff.modified:
        parts.append(f"modified={len(diff.modified)}")
    return ", ".join(parts) or "unknown difference"
