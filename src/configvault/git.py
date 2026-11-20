"""Safe Git CLI wrapper.

No operation force-pushes.  Sync fetches first, verifies ancestry, and refuses
divergent or behind-local histories.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from configvault.errors import DivergenceError, GitError

DEFAULT_TIMEOUT = 120
REMOTE = "origin"


@dataclass
class SyncResult:
    status: str  # skipped, up-to-date, pushed, ahead, diverged
    message: str
    local: str | None = None
    remote: str | None = None


@dataclass
class RepoStatus:
    branch: str
    head: str | None
    dirty: list[str]


def _env() -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("LC_ALL", "C")
    return env


def run_git(
    cwd: Path,
    args: list[str],
    *,
    check: bool = True,
    timeout: int = DEFAULT_TIMEOUT,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=timeout,
            env=_env(),
        )
    except FileNotFoundError as exc:  # pragma: no cover - git is required
        raise GitError("git executable not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(
            f"Git command timed out after {timeout}s: git {' '.join(args)}"
        ) from exc
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise GitError(
            f"git {' '.join(args)} failed in {cwd}"
            + (f": {detail}" if detail else "")
        )
    return result


def init_repo(repo: Path, branch: str, remote: str | None = None) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    if not (repo / ".git").exists():
        run_git(repo, ["init", "-b", branch])
    else:
        # Keep an existing repository's branch; do not rewrite history.
        pass

    if not run_git(repo, ["config", "user.name"], check=False).stdout.strip():
        run_git(repo, ["config", "user.name", "ConfigVault"])
    if not run_git(repo, ["config", "user.email"], check=False).stdout.strip():
        run_git(repo, ["config", "user.email", "configvault@localhost"])
    run_git(repo, ["config", "core.autocrlf", "false"], check=False)
    if remote:
        set_remote(repo, remote)


def set_remote(repo: Path, url: str) -> None:
    existing = run_git(repo, ["remote", "get-url", REMOTE], check=False)
    if existing.returncode == 0:
        if existing.stdout.strip() != url:
            run_git(repo, ["remote", "set-url", REMOTE, url])
    else:
        run_git(repo, ["remote", "add", REMOTE, url])


def get_remote(repo: Path) -> str | None:
    result = run_git(repo, ["remote", "get-url", REMOTE], check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def has_head(repo: Path) -> bool:
    return run_git(repo, ["rev-parse", "--verify", "HEAD"], check=False).returncode == 0


def head_commit(repo: Path) -> str | None:
    result = run_git(repo, ["rev-parse", "--verify", "HEAD"], check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def validate_commit(repo: Path, ref: str) -> str:
    if not ref or ref.startswith("-"):
        raise GitError(f"Invalid commit reference: {ref!r}")
    result = run_git(
        repo,
        ["rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"],
        check=False,
    )
    if result.returncode != 0:
        raise GitError(f"Commit not found in private repository: {ref}")
    return result.stdout.strip()


def is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    result = run_git(
        repo, ["merge-base", "--is-ancestor", ancestor, descendant], check=False
    )
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    detail = (result.stderr or result.stdout).strip()
    raise GitError(f"Cannot compare commits: {detail}")


def stage_and_commit(repo: Path, paths: list[str], message: str) -> str | None:
    if not paths:
        return None
    _stage_paths(repo, paths)
    staged = run_git(repo, ["diff", "--cached", "--quiet"], check=False)
    if staged.returncode == 0:
        if has_head(repo):
            return None
        # An empty initial snapshot still needs a commit so history exists.
        run_git(repo, ["commit", "--allow-empty", "-m", message])
        return head_commit(repo)
    if staged.returncode != 1:
        detail = (staged.stderr or staged.stdout).strip()
        raise GitError(f"Cannot inspect Git index: {detail}")
    run_git(repo, ["commit", "-m", message])
    return head_commit(repo)


def _stage_paths(repo: Path, paths: list[str], chunk_size: int = 200) -> None:
    ordered = list(dict.fromkeys(paths))
    for index in range(0, len(ordered), chunk_size):
        chunk = ordered[index : index + chunk_size]
        run_git(repo, ["add", "-A", "-f", "--", *chunk])


def repo_status(repo: Path) -> RepoStatus:
    branch_result = run_git(repo, ["symbolic-ref", "--short", "-q", "HEAD"], check=False)
    branch = branch_result.stdout.strip() or "(detached)"
    head = head_commit(repo)
    porcelain = run_git(repo, ["status", "--porcelain"], check=False)
    dirty = [line for line in porcelain.stdout.splitlines() if line.strip()]
    return RepoStatus(branch=branch, head=head, dirty=dirty)


def history(repo: Path, limit: int = 20) -> list[dict[str, str]]:
    if not has_head(repo):
        return []
    fmt = "%H%x1f%aI%x1f%s"
    result = run_git(repo, ["log", f"-n{limit}", f"--format={fmt}"])
    rows: list[dict[str, str]] = []
    for line in result.stdout.splitlines():
        parts = line.split("\x1f", 2)
        if len(parts) == 3:
            rows.append({"commit": parts[0], "date": parts[1], "message": parts[2]})
    return rows


def diff_name_status(repo: Path, base: str, target: str = "HEAD") -> list[tuple[str, str]]:
    result = run_git(
        repo,
        ["diff", "--no-ext-diff", "--no-color", "--name-status", base, target, "--"],
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise GitError(f"Cannot diff {base}..{target}: {detail}")
    rows: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            rows.append((parts[0], parts[1]))
    return rows


def diff_patch(repo: Path, base: str, target: str = "HEAD") -> str:
    result = run_git(
        repo,
        ["diff", "--no-ext-diff", "--no-color", base, target, "--"],
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise GitError(f"Cannot diff {base}..{target}: {detail}")
    return result.stdout


def show_file(repo: Path, commit: str, path: str) -> str | None:
    result = run_git(repo, ["show", f"{commit}:{path}"], check=False)
    if result.returncode != 0:
        return None
    return result.stdout


def sync(repo: Path, remote_url: str | None, branch: str) -> SyncResult:
    if not remote_url:
        return SyncResult(status="skipped", message="No private remote configured")
    set_remote(repo, remote_url)
    if not has_head(repo):
        return SyncResult(status="skipped", message="No local commits to synchronize")

    local = head_commit(repo)
    assert local is not None

    ls_remote = run_git(
        repo, ["ls-remote", "--heads", REMOTE, branch], check=False, timeout=DEFAULT_TIMEOUT
    )
    if ls_remote.returncode != 0:
        detail = (ls_remote.stderr or ls_remote.stdout).strip()
        raise GitError(
            "Cannot reach the private remote; the local commit was preserved"
            + (f": {detail}" if detail else "")
        )
    remote_exists = bool(ls_remote.stdout.strip())

    if remote_exists:
        fetched = run_git(
            repo, ["fetch", REMOTE, branch], check=False, timeout=DEFAULT_TIMEOUT
        )
        if fetched.returncode != 0:
            detail = (fetched.stderr or fetched.stdout).strip()
            raise GitError(
                "Cannot fetch the private remote; the local commit was preserved"
                + (f": {detail}" if detail else "")
            )
        remote_sha = run_git(
            repo, ["rev-parse", "--verify", f"refs/remotes/{REMOTE}/{branch}"], check=False
        )
        if remote_sha.returncode != 0:
            raise GitError(f"Remote branch {REMOTE}/{branch} exists but cannot be read")
        remote = remote_sha.stdout.strip()
        if remote == local:
            return SyncResult(
                status="up-to-date",
                message="Private repository is up to date",
                local=local,
                remote=remote,
            )
        if is_ancestor(repo, remote, local):
            _push(repo, branch)
            return SyncResult(
                status="pushed",
                message="Pushed local commits to the private remote",
                local=local,
                remote=remote,
            )
        if is_ancestor(repo, local, remote):
            raise DivergenceError(
                "The private remote has commits that are not present locally. "
                "Refusing to overwrite them. Integrate the remote history manually."
            )
        raise DivergenceError(
            "Local and private remote histories have diverged. "
            "Refusing to force-push. Integrate the histories manually."
        )

    _push(repo, branch)
    return SyncResult(
        status="pushed",
        message="Pushed a new private branch",
        local=local,
        remote=None,
    )


def _push(repo: Path, branch: str) -> None:
    result = run_git(
        repo, ["push", REMOTE, f"HEAD:refs/heads/{branch}"], check=False
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise GitError(
            "Push failed; the local commit was preserved"
            + (f": {detail}" if detail else "")
        )


def worktree_add(repo: Path, path: Path, commit: str) -> None:
    if path.exists():
        raise GitError(f"Worktree path already exists: {path}")
    run_git(repo, ["worktree", "add", "--detach", str(path), commit])


def worktree_remove(repo: Path, path: Path) -> None:
    run_git(repo, ["worktree", "remove", "--force", str(path)], check=False)


def remote_tracking_status(repo: Path, branch: str) -> tuple[int, int] | None:
    """Return (ahead, behind) using existing remote-tracking refs, no fetch."""
    local = head_commit(repo)
    if not local:
        return None
    remote_ref = f"refs/remotes/{REMOTE}/{branch}"
    if run_git(repo, ["rev-parse", "--verify", remote_ref], check=False).returncode != 0:
        return None
    counts = run_git(
        repo, ["rev-list", "--left-right", "--count", f"{remote_ref}...HEAD"], check=False
    )
    if counts.returncode != 0:
        return None
    parts = counts.stdout.split()
    if len(parts) != 2:
        return None
    behind, ahead = int(parts[0]), int(parts[1])
    return ahead, behind
