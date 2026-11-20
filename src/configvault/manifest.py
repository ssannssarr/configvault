"""Public selection manifest and a deliberately small glob implementation.

Syntax (all patterns are relative to the source root, using ``/``):

* ``*`` matches any characters within one path segment.
* ``?`` matches exactly one character within one path segment.
* ``**`` as a complete path segment matches zero or more path segments.
* A trailing ``/`` is shorthand for ``/**``.
* A leading ``!`` marks an exclusion. Exclusions always win.
* Lines starting with ``#`` are comments. Blank lines are ignored.
* A pattern that matches a directory selects the whole subtree beneath it.

Not supported: brace expansion, character classes, negation via ``^``,
inline comments, and ``.gitignore`` semantics such as matching a bare name at
any depth.  Use ``**/name`` for that.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from configvault.errors import ManifestError

MAX_MANIFEST_BYTES = 1_000_000


@dataclass(frozen=True)
class GlobPattern:
    raw: str
    regex: re.Pattern[str]

    def matches(self, relpath: str) -> bool:
        return bool(self.regex.match(relpath))


@dataclass(frozen=True)
class Selection:
    rel: str
    source: Path
    kind: str  # "file" or "symlink"


@dataclass
class PatternSet:
    includes: list[GlobPattern]
    excludes: list[GlobPattern]

    def _matches(self, patterns: list[GlobPattern], relpath: str) -> bool:
        return any(pattern.matches(relpath) for pattern in patterns)

    def _matches_or_parent(self, patterns: list[GlobPattern], relpath: str) -> bool:
        if not relpath:
            return False
        parts = relpath.split("/")
        for index in range(1, len(parts) + 1):
            candidate = "/".join(parts[:index])
            if self._matches(patterns, candidate):
                return True
        return False

    def is_excluded(self, relpath: str) -> bool:
        return self._matches_or_parent(self.excludes, relpath)

    def is_included(self, relpath: str) -> bool:
        return self._matches_or_parent(self.includes, relpath)

    def is_selected(self, relpath: str) -> bool:
        return self.is_included(relpath) and not self.is_excluded(relpath)


def parse_manifest(path: Path, require_includes: bool = True) -> PatternSet:
    if not path.exists():
        raise ManifestError(
            f"Public selection manifest not found: {path}. "
            "Create it with one pattern per line (see README)."
        )
    if not path.is_file():
        raise ManifestError(f"Public selection manifest is not a file: {path}")
    size = path.stat().st_size
    if size > MAX_MANIFEST_BYTES:
        raise ManifestError(
            f"Public selection manifest is too large ({size} bytes; "
            f"limit {MAX_MANIFEST_BYTES})"
        )
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ManifestError(f"Cannot read {path}: {exc}") from exc

    includes: list[GlobPattern] = []
    excludes: list[GlobPattern] = []
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        excluded = line.startswith("!")
        if excluded:
            line = line[1:].strip()
            if not line:
                raise ManifestError(f"{path}:{lineno}: empty exclusion pattern")
        try:
            pattern = compile_pattern(line)
        except ManifestError as exc:
            raise ManifestError(f"{path}:{lineno}: {exc}") from exc
        (excludes if excluded else includes).append(pattern)

    if not includes and require_includes:
        raise ManifestError(
            f"{path} contains no inclusion patterns; refusing an empty selection"
        )
    return PatternSet(includes=includes, excludes=excludes)


def compile_pattern(pattern: str) -> GlobPattern:
    original = pattern
    if "\x00" in pattern:
        raise ManifestError("pattern contains a NUL byte")
    if pattern.startswith("/"):
        raise ManifestError(
            "patterns are already relative to the source root; "
            "do not use a leading '/'"
        )
    if pattern.endswith("/"):
        pattern = pattern + "**"

    segments = pattern.split("/")
    for segment in segments:
        if segment == "":
            raise ManifestError(f"empty path segment in pattern {original!r}")
        if segment in (".", ".."):
            raise ManifestError(f"relative path segment {segment!r} is not allowed")
        if "**" in segment and segment != "**":
            raise ManifestError(
                f"'**' must be a complete path segment in pattern {original!r}"
            )
        if "\\" in segment:
            raise ManifestError(
                f"backslashes are not supported; use '/' in pattern {original!r}"
            )

    expression_parts: list[str] = []
    skip_separator = True
    for index, segment in enumerate(segments):
        last = index == len(segments) - 1
        if segment == "**":
            if last:
                if expression_parts and not skip_separator:
                    expression_parts.append("/")
                expression_parts.append(".*")
                skip_separator = False
            else:
                if expression_parts and not skip_separator:
                    expression_parts.append("/")
                expression_parts.append("(?:[^/]+/)*")
                skip_separator = True
        else:
            if expression_parts and not skip_separator:
                expression_parts.append("/")
            expression_parts.append(_segment_regex(segment))
            skip_separator = False
    expression = "".join(expression_parts)
    expression = "^" + expression + "$"
    try:
        compiled = re.compile(expression)
    except re.error as exc:  # pragma: no cover - defensive
        raise ManifestError(f"cannot compile pattern {original!r}: {exc}") from exc
    return GlobPattern(raw=original, regex=compiled)


def _segment_regex(segment: str) -> str:
    out: list[str] = []
    for char in segment:
        if char == "*":
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(char))
    return "".join(out)


def select_entries(source: Path, patterns: PatternSet) -> tuple[list[Selection], list[str]]:
    """Return selected regular files/symlinks and non-fatal warnings.

    Directories are not returned; Git cannot track empty directories.  Symlinks
    are never followed, and nested ``.git`` entries are always skipped.
    """
    selected: list[Selection] = []
    warnings: list[str] = []

    def walk(directory: Path, prefix: str) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as exc:
            raise ManifestError(f"Cannot read directory {directory}: {exc}") from exc
        for entry in entries:
            rel = f"{prefix}/{entry.name}" if prefix else entry.name
            if entry.name == ".git":
                continue
            try:
                is_symlink = entry.is_symlink()
                if is_symlink:
                    if patterns.is_selected(rel):
                        selected.append(
                            Selection(rel=rel, source=Path(entry.path), kind="symlink")
                        )
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if patterns.is_excluded(rel):
                        continue
                    walk(Path(entry.path), rel)
                    continue
                if entry.is_file(follow_symlinks=False):
                    if patterns.is_selected(rel):
                        selected.append(
                            Selection(rel=rel, source=Path(entry.path), kind="file")
                        )
                    continue
                if patterns.is_selected(rel):
                    warnings.append(f"skipping special file: {rel}")
            except OSError as exc:
                raise ManifestError(f"Cannot inspect {entry.path}: {exc}") from exc

    if not source.exists() or not source.is_dir():
        raise ManifestError(f"Source directory does not exist: {source}")
    walk(source, "")
    selected.sort(key=lambda item: item.rel)
    return selected, warnings


def entry_kind(path: Path) -> str:
    mode = os.lstat(path).st_mode
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISREG(mode):
        return "file"
    return "special"
