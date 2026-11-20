"""Public export with explicit selection and managed-file protection."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from configvault import git
from configvault.config import Config
from configvault.errors import ExportError, GitError, ManifestError, SnapshotError
from configvault.fs import (
    copy_directory_metadata,
    copy_file_atomic,
    copy_symlink_atomic,
    remove_file,
)
from configvault.manifest import Selection, parse_manifest, select_entries
from configvault.safety import is_tool_metadata, suspicious_paths
from configvault.util import Lock, append_log, atomic_write_json, local_stamp, utc_now


@dataclass
class ExportResult:
    selected: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    commit: str | None = None
    push_status: str = "skipped"
    push_error: str | None = None
    warnings: list[str] = field(default_factory=list)
    dry_run: bool = False
    message: str = ""

    @property
    def exported(self) -> int:
        return len(self.selected)


def export_public(
    cfg: Config,
    *,
    message: str | None = None,
    dry_run: bool = False,
    allow_empty: bool = False,
    allow_suspicious: bool = False,
    push: bool = True,
) -> ExportResult:
    cfg.require_source()
    if not (cfg.public_dir / ".git").exists():
        raise ExportError(
            f"Public repository is not initialized: {cfg.public_dir}. "
            "Run 'configvault init' first."
        )
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    try:
        with Lock(cfg.lock_path):
            return _export_locked(
                cfg,
                message=message,
                dry_run=dry_run,
                allow_empty=allow_empty,
                allow_suspicious=allow_suspicious,
                push=push,
            )
    except RuntimeError as exc:
        raise ExportError(str(exc)) from exc


def _export_locked(
    cfg: Config,
    *,
    message: str | None,
    dry_run: bool,
    allow_empty: bool,
    allow_suspicious: bool,
    push: bool,
) -> ExportResult:
    try:
        patterns = parse_manifest(cfg.public_manifest, require_includes=not allow_empty)
        selected, warnings = select_entries(cfg.source, patterns)
    except ManifestError as exc:
        raise ExportError(str(exc)) from exc

    selected_by_rel = {item.rel: item for item in selected}
    selected_paths = sorted(selected_by_rel)
    managed = load_managed(cfg)

    if not selected_paths and not allow_empty:
        raise ExportError(
            "Public selection is empty; refusing to export. "
            "Add patterns to the manifest or pass --allow-empty explicitly."
        )

    tool_paths = [rel for rel in selected_paths if is_tool_metadata(rel)]
    if tool_paths:
        raise ExportError(
            "Refusing to publish ConfigVault/tool metadata: "
            + ", ".join(tool_paths[:10])
            + ". Exclude these paths with '!' patterns."
        )

    suspicious = suspicious_paths(selected_paths)
    if suspicious and not allow_suspicious:
        details = "; ".join(f"{rel} ({reason})" for rel, reason in suspicious[:10])
        raise ExportError(
            "Refusing to publish suspicious/secret-like paths: "
            + details
            + ". Exclude them or pass --allow-suspicious explicitly."
        )
    if suspicious:
        warnings.extend(
            f"publishing suspicious path with explicit approval: {rel} ({reason})"
            for rel, reason in suspicious
        )

    deleted = sorted(managed - set(selected_paths))
    allowed_paths = set(managed) | set(selected_paths)
    _preflight(cfg, selected_by_rel, deleted, managed, allowed_paths)

    if dry_run:
        return ExportResult(
            selected=selected_paths,
            deleted=deleted,
            push_status="skipped",
            warnings=warnings,
            dry_run=True,
            message="dry run",
        )

    created_dirs: list[tuple[Path, Path]] = []
    for rel in selected_paths:
        selection = selected_by_rel[rel]
        target = cfg.public_dir / rel
        try:
            _ensure_parent_dirs(cfg.source, cfg.public_dir, rel, created_dirs)
            if selection.kind == "file":
                copy_file_atomic(selection.source, target)
            elif selection.kind == "symlink":
                copy_symlink_atomic(selection.source, target)
            else:  # pragma: no cover - select_entries only returns these two
                raise ExportError(f"Unsupported export kind for {rel}: {selection.kind}")
        except (SnapshotError, OSError) as exc:
            raise ExportError(f"Failed to export {rel}: {exc}") from exc

    for src_dir, dst_dir in sorted(
        created_dirs, key=lambda pair: len(pair[1].parts), reverse=True
    ):
        try:
            copy_directory_metadata(src_dir, dst_dir)
        except OSError as exc:
            raise ExportError(
                f"Failed to preserve directory metadata for {dst_dir}: {exc}"
            ) from exc

    for rel in deleted:
        target = cfg.public_dir / rel
        try:
            remove_file(target)
        except (SnapshotError, OSError) as exc:
            raise ExportError(f"Failed to mirror deletion of {rel}: {exc}") from exc

    _save_managed(cfg, set(selected_paths))
    commit_message = message or _default_message(len(selected_paths), len(deleted))
    try:
        commit = git.stage_and_commit(
            cfg.public_dir, sorted(allowed_paths), commit_message
        )
    except GitError as exc:
        raise ExportError(f"Public export was written but Git commit failed: {exc}") from exc

    result = ExportResult(
        selected=selected_paths,
        deleted=deleted,
        commit=commit,
        warnings=warnings,
        message=commit_message,
    )

    if push and cfg.public_remote:
        try:
            sync_result = git.sync(cfg.public_dir, cfg.public_remote, cfg.branch)
            result.push_status = sync_result.status
        except GitError as exc:
            result.push_status = "failed"
            result.push_error = str(exc)
            result.warnings.append(f"public push failed: {exc}")
    elif not cfg.public_remote:
        result.push_status = "skipped"

    append_log(
        cfg.log_path,
        f"export +{len(selected_paths)} -{len(deleted)} "
        f"commit={commit or '(none)'} push={result.push_status}",
    )
    return result


def load_managed(cfg: Config) -> set[str]:
    path = cfg.public_state_path
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExportError(f"Cannot read public managed-file state {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ExportError(f"Invalid public managed-file state: {path}")
    recorded_dir = data.get("public_dir")
    if recorded_dir and Path(recorded_dir) != cfg.public_dir:
        raise ExportError(
            f"Public managed-file state belongs to {recorded_dir}, not "
            f"{cfg.public_dir}. Refusing to guess; move or remove {path}."
        )
    files = data.get("files", [])
    if not isinstance(files, list) or not all(isinstance(item, str) for item in files):
        raise ExportError(f"Invalid public managed-file list: {path}")
    for rel in files:
        _validate_relpath(rel)
    return set(files)


def _save_managed(cfg: Config, files: set[str]) -> None:
    atomic_write_json(
        cfg.public_state_path,
        {
            "version": 1,
            "public_dir": str(cfg.public_dir),
            "updated_at": utc_now(),
            "files": sorted(files),
        },
    )


def _preflight(
    cfg: Config,
    selected: dict[str, Selection],
    deleted: list[str],
    managed: set[str],
    allowed_paths: set[str],
) -> None:
    collisions: list[str] = []
    for rel in sorted(selected):
        _validate_relpath(rel)
        _check_ancestors(cfg.public_dir, rel)
        target = cfg.public_dir / rel
        exists = target.is_symlink() or target.exists()
        if exists and rel not in managed:
            collisions.append(rel)
            continue
        if exists and target.is_dir() and not target.is_symlink():
            raise ExportError(
                f"Managed path {rel!r} is now a directory; refusing to replace it"
            )
    if collisions:
        raise ExportError(
            "Refusing to overwrite files not managed by ConfigVault: "
            + ", ".join(collisions[:10])
            + ". Move them or remove them first."
        )

    for rel in deleted:
        _validate_relpath(rel)
        _check_ancestors(cfg.public_dir, rel)
        target = cfg.public_dir / rel
        if target.is_dir() and not target.is_symlink():
            raise ExportError(
                f"Managed path {rel!r} is now a directory; refusing to delete "
                "a manually maintained directory"
            )

    staged = git.run_git(
        cfg.public_dir, ["diff", "--cached", "--name-only", "-z"], check=False
    )
    if staged.returncode != 0:
        detail = (staged.stderr or staged.stdout).strip()
        raise ExportError(f"Cannot inspect public Git index: {detail}")
    staged_paths = {path for path in staged.stdout.split("\x00") if path}
    outside = sorted(staged_paths - allowed_paths)
    if outside:
        raise ExportError(
            "Public repository has staged changes outside ConfigVault-managed "
            "files: " + ", ".join(outside[:10])
            + ". Commit or unstage them first."
        )
    conflicts = git.run_git(
        cfg.public_dir,
        ["diff", "--cached", "--name-only", "--diff-filter=U", "-z"],
        check=False,
    )
    if conflicts.stdout.strip("\x00"):
        raise ExportError("Public repository has unresolved merge conflicts")


def _validate_relpath(rel: str) -> None:
    if not rel or "\x00" in rel:
        raise ExportError(f"Invalid managed path: {rel!r}")
    path = PurePosixPath(rel)
    if path.is_absolute() or any(part in ("..", "") for part in path.parts):
        raise ExportError(f"Unsafe managed path: {rel!r}")


def _check_ancestors(public_dir: Path, rel: str) -> None:
    parts = rel.split("/")[:-1]
    current = public_dir
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise ExportError(
                f"Refusing to follow symlinked directory in public repository: {current}"
            )
        if current.exists() and not current.is_dir():
            raise ExportError(
                f"Parent path is not a directory in public repository: {current}"
            )


def _ensure_parent_dirs(
    source_root: Path,
    public_root: Path,
    rel: str,
    created_dirs: list[tuple[Path, Path]],
) -> None:
    parts = rel.split("/")[:-1]
    for index in range(1, len(parts) + 1):
        src = source_root.joinpath(*parts[:index])
        dst = public_root.joinpath(*parts[:index])
        if dst.is_symlink():
            raise ExportError(f"Refusing to follow symlinked directory: {dst}")
        if dst.exists():
            if not dst.is_dir():
                raise ExportError(f"Public parent path is not a directory: {dst}")
            continue
        # Create writable now; exact source metadata is applied after all
        # files are copied so a read-only source directory cannot block us.
        dst.mkdir(mode=0o700)
        created_dirs.append((src, dst))


def _default_message(exported: int, deleted: int) -> str:
    return f"export: {local_stamp()} (+{exported} -{deleted})"
