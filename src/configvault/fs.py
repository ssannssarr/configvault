"""Filesystem primitives for scanning, copying, and metadata preservation.

Git only records the executable bit.  ConfigVault therefore stores a full
metadata manifest alongside each snapshot so restore can recover modes,
mtimes, symlink targets, sizes, and content hashes.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from configvault.errors import SnapshotError
from configvault.manifest import PatternSet

CHUNK_SIZE = 1024 * 1024
MAX_COPY_ATTEMPTS = 3


@dataclass(frozen=True)
class Entry:
    kind: str  # file, dir, symlink
    mode: int
    size: int = 0
    mtime_ns: int = 0
    sha256: str | None = None
    target: str | None = None

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "kind": self.kind,
            "mode": self.mode,
            "mtime_ns": self.mtime_ns,
        }
        if self.kind == "file":
            data["size"] = self.size
            data["sha256"] = self.sha256
        elif self.kind == "symlink":
            data["target"] = self.target
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Entry":
        kind = data["kind"]
        if kind not in ("file", "dir", "symlink"):
            raise ValueError(f"unknown entry kind: {kind!r}")
        return cls(
            kind=kind,
            mode=int(data["mode"]),
            size=int(data.get("size", 0)),
            mtime_ns=int(data.get("mtime_ns", 0)),
            sha256=data.get("sha256"),
            target=data.get("target"),
        )


@dataclass
class TreeState:
    root_mode: int = 0o755
    root_mtime_ns: int = 0
    entries: dict[str, Entry] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "version": 1,
            "root_mode": self.root_mode,
            "root_mtime_ns": self.root_mtime_ns,
            "entries": {path: entry.to_json() for path, entry in sorted(self.entries.items())},
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "TreeState":
        if int(data.get("version", 1)) != 1:
            raise ValueError("unsupported metadata version")
        entries_raw = data.get("entries", {})
        if not isinstance(entries_raw, dict):
            raise ValueError("metadata entries must be an object")
        entries = {
            str(path): Entry.from_json(entry)
            for path, entry in entries_raw.items()
        }
        return cls(
            root_mode=int(data.get("root_mode", 0o755)),
            root_mtime_ns=int(data.get("root_mtime_ns", 0)),
            entries=entries,
        )


@dataclass
class TreeDiff:
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.added or self.removed or self.modified)


def entries_equal(left: Entry, right: Entry) -> bool:
    if left.kind != right.kind:
        return False
    if left.mtime_ns != right.mtime_ns:
        return False
    if left.kind == "symlink":
        return left.target == right.target
    if left.kind == "dir":
        return left.mode == right.mode
    return (
        left.mode == right.mode
        and left.sha256 == right.sha256
        and left.size == right.size
    )


def compare_states(old: TreeState, new: TreeState) -> TreeDiff:
    old_paths = set(old.entries)
    new_paths = set(new.entries)
    added = sorted(new_paths - old_paths)
    removed = sorted(old_paths - new_paths)
    modified = sorted(
        path
        for path in old_paths & new_paths
        if not entries_equal(old.entries[path], new.entries[path])
    )
    return TreeDiff(added=added, removed=removed, modified=modified)


def hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def scan_tree(
    root: Path,
    patterns: PatternSet | None = None,
    warnings: list[str] | None = None,
    strict: bool = False,
) -> TreeState:
    """Scan a tree without following symlinks.

    ``strict=True`` is used for live source scans during a snapshot: a file
    disappearing mid-scan is an error.  Status scans are lenient.
    """
    warnings = warnings if warnings is not None else []
    try:
        root_stat = os.lstat(root)
    except OSError as exc:
        raise SnapshotError(f"Cannot inspect {root}: {exc}") from exc
    if not stat.S_ISDIR(root_stat.st_mode):
        raise SnapshotError(f"Not a directory: {root}")

    state = TreeState(
        root_mode=stat.S_IMODE(root_stat.st_mode),
        root_mtime_ns=root_stat.st_mtime_ns,
    )

    def walk(directory: Path, prefix: str) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as exc:
            if strict:
                raise SnapshotError(f"Cannot read {directory}: {exc}") from exc
            warnings.append(f"cannot read directory {directory}: {exc}")
            return
        for dir_entry in entries:
            rel = f"{prefix}/{dir_entry.name}" if prefix else dir_entry.name
            if patterns is not None and patterns.is_excluded(rel):
                continue
            path = Path(dir_entry.path)
            try:
                st = os.lstat(path)
            except FileNotFoundError:
                if strict:
                    raise SnapshotError(f"File disappeared while scanning: {path}")
                warnings.append(f"file disappeared while scanning: {rel}")
                continue
            except OSError as exc:
                if strict:
                    raise SnapshotError(f"Cannot inspect {path}: {exc}") from exc
                warnings.append(f"cannot inspect {rel}: {exc}")
                continue

            mode = stat.S_IMODE(st.st_mode)
            if stat.S_ISLNK(st.st_mode):
                try:
                    target = os.readlink(path)
                except OSError as exc:
                    raise SnapshotError(f"Cannot read symlink {path}: {exc}") from exc
                state.entries[rel] = Entry(
                    kind="symlink",
                    mode=mode,
                    size=len(target),
                    mtime_ns=st.st_mtime_ns,
                    target=target,
                )
            elif stat.S_ISDIR(st.st_mode):
                state.entries[rel] = Entry(
                    kind="dir", mode=mode, mtime_ns=st.st_mtime_ns
                )
                walk(path, rel)
            elif stat.S_ISREG(st.st_mode):
                try:
                    digest, size = hash_file(path)
                except FileNotFoundError:
                    if strict:
                        raise SnapshotError(f"File disappeared while scanning: {path}")
                    warnings.append(f"file disappeared while scanning: {rel}")
                    continue
                except OSError as exc:
                    if strict:
                        raise SnapshotError(f"Cannot read {path}: {exc}") from exc
                    warnings.append(f"cannot read {rel}: {exc}")
                    continue
                state.entries[rel] = Entry(
                    kind="file",
                    mode=mode,
                    size=size,
                    mtime_ns=st.st_mtime_ns,
                    sha256=digest,
                )
            else:
                warnings.append(f"skipping special file: {rel}")

    walk(root, "")
    return state


def copy_tree(
    source: Path,
    destination: Path,
    patterns: PatternSet | None = None,
    warnings: list[str] | None = None,
) -> TreeState:
    """Copy a tree preserving symlinks, modes, and mtimes.

    The destination must not already contain data.  Any source change detected
    while copying aborts the operation; the caller is responsible for staging
    and atomic replacement.
    """
    warnings = warnings if warnings is not None else []
    try:
        root_stat = os.lstat(source)
    except OSError as exc:
        raise SnapshotError(f"Cannot inspect {source}: {exc}") from exc
    if not stat.S_ISDIR(root_stat.st_mode):
        raise SnapshotError(f"Not a directory: {source}")
    if destination.exists():
        if not destination.is_dir() or any(destination.iterdir()):
            raise SnapshotError(f"Destination already exists and is not empty: {destination}")
    else:
        destination.mkdir(parents=True, exist_ok=False)    # Make the staging root traversable regardless of the source mode until
    # final metadata is applied.
    os.chmod(destination, 0o700)

    state = TreeState(
        root_mode=stat.S_IMODE(root_stat.st_mode),
        root_mtime_ns=root_stat.st_mtime_ns,
    )

    def walk(src_dir: Path, dst_dir: Path, prefix: str) -> None:
        try:
            entries = sorted(os.scandir(src_dir), key=lambda entry: entry.name)
        except OSError as exc:
            raise SnapshotError(f"Cannot read {src_dir}: {exc}") from exc
        for dir_entry in entries:
            rel = f"{prefix}/{dir_entry.name}" if prefix else dir_entry.name
            if patterns is not None and patterns.is_excluded(rel):
                continue
            src_path = Path(dir_entry.path)
            dst_path = dst_dir / dir_entry.name
            try:
                st = os.lstat(src_path)
            except FileNotFoundError:
                raise SnapshotError(f"File disappeared while snapshotting: {src_path}")
            except OSError as exc:
                raise SnapshotError(f"Cannot inspect {src_path}: {exc}") from exc

            mode = stat.S_IMODE(st.st_mode)
            if stat.S_ISLNK(st.st_mode):
                try:
                    target = os.readlink(src_path)
                    os.symlink(target, dst_path)
                except OSError as exc:
                    raise SnapshotError(f"Cannot copy symlink {src_path}: {exc}") from exc
                _set_times(dst_path, st.st_mtime_ns, follow_symlinks=False, warnings=warnings)
                state.entries[rel] = Entry(
                    kind="symlink",
                    mode=mode,
                    size=len(target),
                    mtime_ns=st.st_mtime_ns,
                    target=target,
                )
            elif stat.S_ISDIR(st.st_mode):
                try:
                    dst_path.mkdir(mode=0o700)
                except OSError as exc:
                    raise SnapshotError(f"Cannot create directory {dst_path}: {exc}") from exc
                walk(src_path, dst_path, rel)
                try:
                    os.chmod(dst_path, mode)
                except OSError as exc:
                    raise SnapshotError(f"Cannot set mode on {dst_path}: {exc}") from exc
                _set_times(dst_path, st.st_mtime_ns, follow_symlinks=False, warnings=warnings)
                state.entries[rel] = Entry(
                    kind="dir", mode=mode, mtime_ns=st.st_mtime_ns
                )
            elif stat.S_ISREG(st.st_mode):
                entry = _copy_regular(src_path, dst_path, st, warnings)
                state.entries[rel] = entry
            else:
                warnings.append(f"skipping special file: {rel}")

    walk(source, destination, "")
    try:
        os.chmod(destination, state.root_mode)
    except OSError as exc:
        raise SnapshotError(f"Cannot set mode on {destination}: {exc}") from exc
    _set_times(
        destination, state.root_mtime_ns, follow_symlinks=False, warnings=warnings
    )
    return state


def _copy_regular(
    source: Path, destination: Path, initial_stat: os.stat_result, warnings: list[str]
) -> Entry:
    last_error: str | None = None
    for _attempt in range(MAX_COPY_ATTEMPTS):
        digest = hashlib.sha256()
        size = 0
        try:
            with source.open("rb") as src_handle, destination.open("xb") as dst_handle:
                while True:
                    chunk = src_handle.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    dst_handle.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                dst_handle.flush()
                os.fsync(dst_handle.fileno())
            after = os.stat(source, follow_symlinks=False)
            before = initial_stat
            if (
                after.st_size != before.st_size
                or after.st_mtime_ns != before.st_mtime_ns
                or after.st_ino != before.st_ino
                or after.st_mode != before.st_mode
            ):
                last_error = "source changed while copying"
                with contextlib.suppress(OSError):
                    destination.unlink()
                initial_stat = after
                continue
            os.chmod(destination, stat.S_IMODE(before.st_mode))
            _set_times(
                destination, before.st_mtime_ns, follow_symlinks=False, warnings=warnings
            )
            return Entry(
                kind="file",
                mode=stat.S_IMODE(before.st_mode),
                size=size,
                mtime_ns=before.st_mtime_ns,
                sha256=digest.hexdigest(),
            )
        except FileNotFoundError as exc:
            with contextlib.suppress(OSError):
                destination.unlink()
            raise SnapshotError(f"File disappeared while snapshotting: {source}") from exc
        except OSError as exc:
            with contextlib.suppress(OSError):
                destination.unlink()
            raise SnapshotError(f"Cannot copy {source}: {exc}") from exc
    with contextlib.suppress(OSError):
        destination.unlink()
    raise SnapshotError(f"Could not get a stable copy of {source}: {last_error}")


def _set_times(
    path: Path, mtime_ns: int, follow_symlinks: bool, warnings: list[str]
) -> None:
    try:
        os.utime(
            path,
            ns=(mtime_ns, mtime_ns),
            follow_symlinks=follow_symlinks,
        )
    except (OSError, NotImplementedError) as exc:
        warnings.append(f"could not preserve mtime for {path}: {exc}")


def apply_metadata(root: Path, state: TreeState, warnings: list[str] | None = None) -> None:
    """Apply recorded modes and mtimes to an existing tree.

    Files and symlinks are handled first, then directories deepest-first so
    directory mtimes are not clobbered by child updates.
    """
    warnings = warnings if warnings is not None else []
    directories: list[tuple[str, Entry]] = []
    for rel, entry in sorted(state.entries.items()):
        path = root / rel
        try:
            if entry.kind == "symlink":
                if not path.is_symlink():
                    raise SnapshotError(f"Expected symlink at {path}")
                _set_times(path, entry.mtime_ns, follow_symlinks=False, warnings=warnings)
            elif entry.kind == "dir":
                if not path.is_dir() or path.is_symlink():
                    raise SnapshotError(f"Expected directory at {path}")
                directories.append((rel, entry))
            elif entry.kind == "file":
                if not path.is_file() or path.is_symlink():
                    raise SnapshotError(f"Expected regular file at {path}")
                os.chmod(path, entry.mode)
                _set_times(path, entry.mtime_ns, follow_symlinks=False, warnings=warnings)
        except OSError as exc:
            raise SnapshotError(f"Cannot apply metadata to {path}: {exc}") from exc

    for rel, entry in sorted(
        directories, key=lambda item: item[0].count("/"), reverse=True
    ):
        path = root / rel
        try:
            os.chmod(path, entry.mode)
            _set_times(path, entry.mtime_ns, follow_symlinks=False, warnings=warnings)
        except OSError as exc:
            raise SnapshotError(f"Cannot apply metadata to {path}: {exc}") from exc

    try:
        os.chmod(root, state.root_mode)
    except OSError as exc:
        raise SnapshotError(f"Cannot set mode on {root}: {exc}") from exc
    _set_times(root, state.root_mtime_ns, follow_symlinks=False, warnings=warnings)


def copy_file_atomic(source: Path, destination: Path) -> Entry:
    """Copy one regular file to destination via a same-directory temp file."""
    try:
        st = os.lstat(source)
    except OSError as exc:
        raise SnapshotError(f"Cannot inspect {source}: {exc}") from exc
    if not stat.S_ISREG(st.st_mode):
        raise SnapshotError(f"Not a regular file: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.configvault-", dir=destination.parent
    )
    tmp = Path(tmp_name)
    try:
        digest = hashlib.sha256()
        size = 0
        with source.open("rb") as src_handle, os.fdopen(fd, "wb") as dst_handle:
            while True:
                chunk = src_handle.read(CHUNK_SIZE)
                if not chunk:
                    break
                dst_handle.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            dst_handle.flush()
            os.fsync(dst_handle.fileno())
        after = os.stat(source, follow_symlinks=False)
        if after.st_size != st.st_size or after.st_mtime_ns != st.st_mtime_ns:
            raise SnapshotError(f"Source changed while exporting: {source}")
        os.chmod(tmp, stat.S_IMODE(st.st_mode))
        os.utime(tmp, ns=(st.st_mtime_ns, st.st_mtime_ns))
        os.replace(tmp, destination)
        return Entry(
            kind="file",
            mode=stat.S_IMODE(st.st_mode),
            size=size,
            mtime_ns=st.st_mtime_ns,
            sha256=digest.hexdigest(),
        )
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def copy_symlink_atomic(source: Path, destination: Path) -> Entry:
    try:
        st = os.lstat(source)
        if not stat.S_ISLNK(st.st_mode):
            raise SnapshotError(f"Not a symlink: {source}")
        target = os.readlink(source)
    except OSError as exc:
        raise SnapshotError(f"Cannot inspect symlink {source}: {exc}") from exc
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.parent / f".{destination.name}.configvault-{os.getpid()}-{id(source)}"
    with contextlib.suppress(FileNotFoundError):
        tmp.unlink()
    try:
        os.symlink(target, tmp)
        _set_times(tmp, st.st_mtime_ns, follow_symlinks=False, warnings=[])
        os.replace(tmp, destination)
        return Entry(
            kind="symlink",
            mode=stat.S_IMODE(st.st_mode),
            size=len(target),
            mtime_ns=st.st_mtime_ns,
            target=target,
        )
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def remove_file(path: Path) -> bool:
    """Remove a managed file or symlink. Never recurse into directories."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    if stat.S_ISDIR(st.st_mode):
        raise SnapshotError(f"Refusing to remove directory as a managed file: {path}")
    os.unlink(path)
    return True


def remove_empty_parents(path: Path, stop: Path) -> None:
    current = path.parent
    stop = stop.absolute()
    while current != stop and stop in current.parents:
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def copy_directory_metadata(source: Path, destination: Path) -> None:
    st = os.lstat(source)
    os.chmod(destination, stat.S_IMODE(st.st_mode))
    with contextlib.suppress(OSError):
        os.utime(destination, ns=(st.st_mtime_ns, st.st_mtime_ns))


def tree_size(state: TreeState) -> int:
    return sum(entry.size for entry in state.entries.values() if entry.kind == "file")


def remove_tree(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)
