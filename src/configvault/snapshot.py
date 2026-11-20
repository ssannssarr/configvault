"""Snapshot engine with staging, atomic replacement, and crash recovery."""

from __future__ import annotations

import contextlib
import json
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from configvault import git
from configvault.config import Config
from configvault.errors import ConfigError, ManifestError, SnapshotError
from configvault.fs import (
    TreeDiff,
    TreeState,
    compare_states,
    copy_tree,
    remove_tree,
    scan_tree,
    tree_size,
)
from configvault.manifest import PatternSet, compile_pattern
from configvault.util import (
    Lock,
    append_log,
    atomic_write_json,
    local_stamp,
    utc_now,
)


@dataclass
class SnapshotResult:
    changed: bool
    commit: str | None
    message: str
    diff: TreeDiff
    warnings: list[str] = field(default_factory=list)
    size: int = 0


@dataclass
class StatusResult:
    diff: TreeDiff
    previous_commit: str | None
    warnings: list[str] = field(default_factory=list)
    previous_state: TreeState | None = None


class SnapshotEngine:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.work_dir = cfg.repo_dir / ".git" / "configvault"
        self._patterns = _exclusion_set(cfg.exclude)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def create(self, message: str | None = None) -> SnapshotResult:
        self.cfg.require_source()
        self.cfg.require_private_repo()
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        try:
            with Lock(self.cfg.lock_path):
                return self._create_locked(message)
        except RuntimeError as exc:
            raise SnapshotError(str(exc)) from exc

    def create_locked(self, message: str | None = None) -> SnapshotResult:
        """Create a snapshot while the caller already holds the lock."""
        self.cfg.require_source()
        self.cfg.require_private_repo()
        return self._create_locked(message)

    def status(self) -> StatusResult:
        self.cfg.require_source()
        self.cfg.require_private_repo()
        previous_commit = git.head_commit(self.cfg.repo_dir)
        previous = self._load_previous_state()
        warnings: list[str] = []
        live = scan_tree(self.cfg.source, self._patterns, warnings=warnings, strict=False)
        return StatusResult(
            diff=compare_states(previous, live),
            previous_commit=previous_commit,
            warnings=warnings,
            previous_state=previous,
        )

    def recover(self) -> list[str]:
        """Repair an interrupted snapshot. Safe to call at startup."""
        self.cfg.require_private_repo()
        self.work_dir.mkdir(parents=True, exist_ok=True)
        try:
            with Lock(self.cfg.lock_path):
                return self._recover_locked()
        except RuntimeError as exc:
            raise SnapshotError(str(exc)) from exc

    # ------------------------------------------------------------------
    # Snapshot creation
    # ------------------------------------------------------------------
    def _create_locked(self, message: str | None) -> SnapshotResult:
        recovery_notes = self._recover_locked()
        previous = self._load_previous_state()
        staging = self.work_dir / f"staging-{uuid.uuid4().hex}"
        warnings: list[str] = list(recovery_notes)

        try:
            new_state = copy_tree(
                self.cfg.source, staging, self._patterns, warnings=warnings
            )
        except BaseException:
            remove_tree(staging)
            raise

        diff = compare_states(previous, new_state)
        if diff.empty and git.has_head(self.cfg.repo_dir):
            remove_tree(staging)
            return SnapshotResult(
                changed=False,
                commit=None,
                message="No changes; no snapshot commit created",
                diff=diff,
                warnings=warnings,
                size=tree_size(new_state),
            )

        commit_message = message or _default_message(diff, new_state)
        try:
            commit = self._install_snapshot(staging, new_state, commit_message, diff)
        except BaseException:
            remove_tree(staging)
            raise
        result = SnapshotResult(
            changed=True,
            commit=commit,
            message=commit_message,
            diff=diff,
            warnings=warnings,
            size=tree_size(new_state),
        )
        append_log(
            self.cfg.log_path,
            f"snapshot {commit or '(none)'} +{len(diff.added)} "
            f"~{len(diff.modified)} -{len(diff.removed)}",
        )
        self._write_last_state(result)
        return result

    def _install_snapshot(
        self, staging: Path, state: TreeState, message: str, diff: TreeDiff
    ) -> str | None:
        repo = self.cfg.repo_dir
        snapshot = self.cfg.snapshot_dir
        metadata = self.cfg.metadata_path
        old_snapshot = self.work_dir / f"old-{uuid.uuid4().hex}"
        old_metadata = metadata.read_bytes() if metadata.exists() else None
        had_snapshot = snapshot.exists()

        if had_snapshot:
            os.replace(snapshot, old_snapshot)
        try:
            os.replace(staging, snapshot)
        except BaseException:
            if had_snapshot:
                os.replace(old_snapshot, snapshot)
            raise

        try:
            metadata_doc = state.to_json()
            metadata_doc["created_at"] = utc_now()
            metadata_doc["source"] = str(self.cfg.source)
            atomic_write_json(metadata, metadata_doc, mode=0o644)
            commit = git.stage_and_commit(
                repo, ["snapshot", "metadata.json"], message
            )
            if commit is None:
                raise SnapshotError(
                    "Snapshot differed from the previous tree but Git reported no "
                    "staged changes"
                )
        except BaseException as exc:
            self._rollback_install(
                snapshot=snapshot,
                old_snapshot=old_snapshot if had_snapshot else None,
                metadata=metadata,
                old_metadata=old_metadata,
            )
            if isinstance(exc, SnapshotError):
                raise
            raise SnapshotError(f"Snapshot commit failed; previous snapshot kept: {exc}") from exc

        with contextlib.suppress(OSError):
            remove_tree(old_snapshot)
        return commit
    def _rollback_install(
        self,
        *,
        snapshot: Path,
        old_snapshot: Path | None,
        metadata: Path,
        old_metadata: bytes | None,
    ) -> None:
        repo = self.cfg.repo_dir
        remove_tree(snapshot)
        if old_snapshot is not None and old_snapshot.exists():
            os.replace(old_snapshot, snapshot)
        elif git.has_head(repo):
            # The old tree is still in HEAD.
            git.run_git(repo, ["checkout", "HEAD", "--", "snapshot"], check=False)
            if not snapshot.exists():
                snapshot.mkdir(parents=True, exist_ok=True)
        else:
            snapshot.mkdir(parents=True, exist_ok=True)

        if old_metadata is not None:
            from configvault.util import atomic_write_bytes

            atomic_write_bytes(metadata, old_metadata, mode=0o644)
        else:
            with contextlib.suppress(FileNotFoundError):
                metadata.unlink()

        if git.has_head(repo):
            git.run_git(repo, ["reset", "--hard", "HEAD"], check=False)
        else:
            git.run_git(repo, ["rm", "-r", "--cached", "--ignore-unmatch", "--", "snapshot", "metadata.json"], check=False)
            with contextlib.suppress(FileNotFoundError):
                (repo / ".git" / "index").unlink()

    # ------------------------------------------------------------------
    # Recovery
    # ------------------------------------------------------------------
    def _recover_locked(self) -> list[str]:
        notes: list[str] = []
        self.work_dir.mkdir(parents=True, exist_ok=True)
        repo = self.cfg.repo_dir
        snapshot = self.cfg.snapshot_dir
        metadata = self.cfg.metadata_path
        head = git.head_commit(repo)

        if not snapshot.exists():
            if head:
                checkout = git.run_git(
                    repo, ["checkout", "HEAD", "--", "snapshot"], check=False
                )
                if checkout.returncode != 0:
                    snapshot.mkdir(parents=True, exist_ok=True)
                notes.append("restored missing snapshot from HEAD")
            else:
                olds = sorted(self.work_dir.glob("old-*"), key=_mtime, reverse=True)
                if olds:
                    os.replace(olds[0], snapshot)
                    notes.append("restored snapshot from interrupted swap")
                else:
                    snapshot.mkdir(parents=True, exist_ok=True)

        if head:
            dirty = git.run_git(
                repo,
                ["status", "--porcelain", "--", "snapshot", "metadata.json"],
                check=False,
            )
            if dirty.stdout.strip():
                git.run_git(repo, ["reset", "--hard", "HEAD"], check=False)
                notes.append("discarded uncommitted interrupted snapshot state")
        else:
            with contextlib.suppress(FileNotFoundError):
                metadata.unlink()
            git.run_git(
                repo,
                ["rm", "-r", "--cached", "--ignore-unmatch", "--", "snapshot", "metadata.json"],
                check=False,
            )

        for path in self.work_dir.glob("staging-*"):
            remove_tree(path)
            notes.append(f"removed stale staging directory {path.name}")
        for path in self.work_dir.glob("old-*"):
            remove_tree(path)
        return notes

    # ------------------------------------------------------------------
    # State helpers
    # ------------------------------------------------------------------
    def _load_previous_state(self) -> TreeState:
        repo = self.cfg.repo_dir
        if git.has_head(repo):
            raw = git.show_file(repo, "HEAD", "metadata.json")
            if raw:
                try:
                    data = json.loads(raw)
                    return TreeState.from_json(data)
                except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                    raise SnapshotError(
                        f"Snapshot metadata at HEAD is invalid: {exc}"
                    ) from exc
        if self.cfg.snapshot_dir.exists():
            warnings: list[str] = []
            return scan_tree(
                self.cfg.snapshot_dir, patterns=None, warnings=warnings, strict=False
            )
        return TreeState()

    def _write_last_state(self, result: SnapshotResult) -> None:
        try:
            atomic_write_json(
                self.cfg.last_snapshot_state_path,
                {
                    "commit": result.commit,
                    "created_at": utc_now(),
                    "message": result.message,
                    "size_bytes": result.size,
                    "added": result.diff.added,
                    "modified": result.diff.modified,
                    "removed": result.diff.removed,
                    "warnings": result.warnings,
                },
            )
        except OSError as exc:
            result.warnings.append(f"could not write last-snapshot state: {exc}")


def _default_message(diff: TreeDiff, state: TreeState) -> str:
    if not state.entries:
        suffix = "(empty)"
    else:
        suffix = f"(+{len(diff.added)} ~{len(diff.modified)} -{len(diff.removed)})"
    return f"snapshot: {local_stamp()} {suffix}"


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _exclusion_set(patterns: list[str]) -> PatternSet:
    compiled = []
    for pattern in patterns:
        try:
            compiled.append(compile_pattern(pattern))
        except ManifestError as exc:
            raise ConfigError(f"Invalid snapshot exclude pattern {pattern!r}: {exc}") from exc
    return PatternSet(includes=[], excludes=compiled)
