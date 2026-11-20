"""ConfigVault command-line interface."""

from __future__ import annotations

from pathlib import Path
from typing import NoReturn, Optional

import typer

from configvault import __version__, git
from configvault.config import Config
from configvault.errors import (
    ConfigVaultError,
    DivergenceError,
    GitError,
)
from configvault.export import export_public, load_managed
from configvault.init import initialize
from configvault.restore import restore as restore_snapshot
from configvault.snapshot import SnapshotEngine
from configvault.systemd import systemd_user_dir

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Snapshot-based dotfiles backup with private and public Git layers.",
)


@app.callback()
def main(
    ctx: typer.Context,
    config: Optional[Path] = typer.Option(
        None,
        "--config",
        envvar="CONFIGVAULT_CONFIG",
        help="Path to configvault.toml (default: ~/.config/configvault.toml).",
    ),
) -> None:
    try:
        ctx.obj = Config.load(config)
    except ConfigVaultError as exc:
        _die(exc)


def _die(exc: Exception) -> NoReturn:
    typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)


def _warn(message: str) -> None:
    typer.secho(f"warning: {message}", fg=typer.colors.YELLOW, err=True)


def _cfg(ctx: typer.Context) -> Config:
    return ctx.obj


def _short(commit: str | None) -> str:
    return commit[:12] if commit else "(none)"


def _print_warnings(warnings: list[str]) -> None:
    for warning in warnings:
        _warn(warning)


@app.command()
def init(
    ctx: typer.Context,
    private_remote: Optional[str] = typer.Option(
        None, "--private-remote", help="Private GitHub repository URL."
    ),
    public_remote: Optional[str] = typer.Option(
        None, "--public-remote", help="Public GitHub repository URL."
    ),
    force_config: bool = typer.Option(
        False, "--force-config", help="Overwrite an existing config.toml."
    ),
    install_systemd: bool = typer.Option(
        False, "--install-units", help="Install systemd user units (does not enable them)."
    ),
) -> None:
    """Initialize configuration and both Git repositories."""
    cfg = _cfg(ctx)
    try:
        result = initialize(
            cfg,
            private_remote=private_remote,
            public_remote=public_remote,
            force_config=force_config,
            install_systemd=install_systemd,
        )
    except ConfigVaultError as exc:
        _die(exc)

    typer.echo(f"config:        {cfg.config_path}")
    typer.echo(f"private repo:  {result.private_repo}")
    typer.echo(f"public repo:   {result.public_repo}")
    typer.echo(f"manifest:      {cfg.public_manifest}")
    typer.secho(
        "security: private GitHub repositories are access-controlled, not "
        "encrypted; anyone with repository access can read the snapshots.",
        fg=typer.colors.YELLOW,
        err=True,
    )
    if result.manifest_created:
        typer.echo("created a commented .gitinclude template; edit it before exporting")
    _print_warnings(result.warnings)
    if result.unit_paths:
        typer.echo("installed systemd user units:")
        for path in result.unit_paths:
            typer.echo(f"  {path}")
        typer.echo("enable with: systemctl --user enable --now configvault.timer")
    typer.echo("next: configvault snapshot")


@app.command()
def snapshot(
    ctx: typer.Context,
    message: Optional[str] = typer.Option(
        None, "-m", "--message", help="Custom snapshot commit message."
    ),
    no_sync: bool = typer.Option(
        False, "--no-sync", help="Do not push the private repository."
    ),
    no_export: bool = typer.Option(
        False, "--no-export", help="Do not export public dotfiles."
    ),
) -> None:
    """Create a complete local snapshot, then sync and export."""
    cfg = _cfg(ctx)
    try:
        result = SnapshotEngine(cfg).create(message)
    except ConfigVaultError as exc:
        _die(exc)

    if not result.changed:
        typer.echo("no changes; no snapshot commit created")
    else:
        typer.echo(f"snapshot: {_short(result.commit)}")
        typer.echo(
            f"changes:  +{len(result.diff.added)} "
            f"~{len(result.diff.modified)} -{len(result.diff.removed)}"
        )
        typer.echo(f"size:     {result.size} bytes")
    _print_warnings(result.warnings)

    if not no_sync:
        try:
            sync_result = git.sync(cfg.repo_dir, cfg.private_remote, cfg.branch)
            typer.echo(f"private:  {sync_result.message}")
        except DivergenceError as exc:
            _warn(f"private sync refused: {exc}")
        except GitError as exc:
            _warn(f"private sync failed: {exc}")

    if not no_export:
        try:
            export_result = export_public(cfg)
            typer.echo(
                f"public:   exported {export_result.exported}, "
                f"deleted {len(export_result.deleted)}, "
                f"commit {_short(export_result.commit)}, push {export_result.push_status}"
            )
            _print_warnings(export_result.warnings)
        except ConfigVaultError as exc:
            _warn(f"public export skipped/failed: {exc}")


@app.command()
def status(ctx: typer.Context) -> None:
    """Show pending changes and synchronization status."""
    cfg = _cfg(ctx)
    try:
        cfg.require_private_repo()
        result = SnapshotEngine(cfg).status()
    except ConfigVaultError as exc:
        _die(exc)

    typer.echo(f"source:   {cfg.source}")
    typer.echo(f"private:  {cfg.repo_dir}")
    typer.echo(f"head:     {_short(result.previous_commit)}")
    repo_state = git.repo_status(cfg.repo_dir)
    typer.echo(f"worktree: {'dirty' if repo_state.dirty else 'clean'}")

    tracking = git.remote_tracking_status(cfg.repo_dir, cfg.branch)
    if tracking is None:
        typer.echo("remote:   no fetched remote branch")
    else:
        ahead, behind = tracking
        typer.echo(f"remote:   ahead {ahead}, behind {behind} (last fetch)")

    diff = result.diff
    typer.echo(
        f"pending:  +{len(diff.added)} ~{len(diff.modified)} -{len(diff.removed)}"
    )
    for label, paths in (
        ("added", diff.added),
        ("modified", diff.modified),
        ("removed", diff.removed),
    ):
        for path in paths[:20]:
            typer.echo(f"  {label:8} {path}")
        if len(paths) > 20:
            typer.echo(f"  ... {len(paths) - 20} more {label}")

    try:
        managed = load_managed(cfg)
        typer.echo(f"public:   {len(managed)} managed files")
        if (cfg.public_dir / ".git").exists():
            public_state = git.repo_status(cfg.public_dir)
            typer.echo(f"public wt: {'dirty' if public_state.dirty else 'clean'}")
        else:
            typer.echo("public wt: not initialized")
    except ConfigVaultError as exc:
        _warn(f"cannot read public state: {exc}")

    _print_warnings(result.warnings)


@app.command()
def history(
    ctx: typer.Context,
    limit: int = typer.Option(20, "-n", "--limit", min=1, max=500),
) -> None:
    """Display snapshot history."""
    cfg = _cfg(ctx)
    try:
        cfg.require_private_repo()
        rows = git.history(cfg.repo_dir, limit)
    except ConfigVaultError as exc:
        _die(exc)
    if not rows:
        typer.echo("no snapshots yet")
        return
    head = git.head_commit(cfg.repo_dir)
    for row in rows:
        marker = "*" if row["commit"] == head else " "
        typer.echo(f"{marker} {row['commit'][:12]}  {row['date']}  {row['message']}")


@app.command()
def diff(
    ctx: typer.Context,
    commit: Optional[str] = typer.Argument(
        None, help="Base commit (default: HEAD~1)."
    ),
    patch: bool = typer.Option(False, "--patch", help="Show a full patch."),
) -> None:
    """Show changes between snapshots."""
    cfg = _cfg(ctx)
    try:
        cfg.require_private_repo()
        if not git.has_head(cfg.repo_dir):
            typer.echo("no snapshots yet")
            return
        base = commit or "HEAD~1"
        if git.run_git(cfg.repo_dir, ["rev-parse", "--verify", base], check=False).returncode != 0:
            typer.echo(f"cannot diff: {base} does not exist")
            raise typer.Exit(code=1)
        if patch:
            typer.echo(git.diff_patch(cfg.repo_dir, base, "HEAD"), nl=False)
        else:
            rows = git.diff_name_status(cfg.repo_dir, base, "HEAD")
            if not rows:
                typer.echo("no differences")
            for status, path in rows:
                typer.echo(f"{status:2} {path}")
    except ConfigVaultError as exc:
        _die(exc)


@app.command()
def sync(ctx: typer.Context) -> None:
    """Synchronize existing local commits with the private remote."""
    cfg = _cfg(ctx)
    try:
        result = git.sync(cfg.repo_dir, cfg.private_remote, cfg.branch)
    except ConfigVaultError as exc:
        _die(exc)
    typer.echo(result.message)
    if result.status == "skipped" and not cfg.private_remote:
        _warn("configure private_remote in configvault.toml to enable synchronization")


@app.command()
def export(
    ctx: typer.Context,
    message: Optional[str] = typer.Option(
        None, "-m", "--message", help="Custom public export commit message."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show planned changes only."),
    allow_empty: bool = typer.Option(
        False, "--allow-empty", help="Explicitly allow an empty selection."
    ),
    allow_suspicious: bool = typer.Option(
        False, "--allow-suspicious", help="Explicitly allow secret-like paths."
    ),
    no_push: bool = typer.Option(False, "--no-push", help="Do not push public repo."),
) -> None:
    """Export explicitly selected configurations to the public repository."""
    cfg = _cfg(ctx)
    try:
        result = export_public(
            cfg,
            message=message,
            dry_run=dry_run,
            allow_empty=allow_empty,
            allow_suspicious=allow_suspicious,
            push=not no_push,
        )
    except ConfigVaultError as exc:
        _die(exc)

    prefix = "would export" if dry_run else "exported"
    typer.echo(f"{prefix}: {result.exported}")
    for rel in result.selected:
        typer.echo(f"  + {rel}")
    if result.deleted:
        typer.echo(f"{'would delete' if dry_run else 'deleted'}: {len(result.deleted)}")
        for rel in result.deleted:
            typer.echo(f"  - {rel}")
    if not dry_run:
        typer.echo(f"commit: {_short(result.commit)}")
        typer.echo(f"push:   {result.push_status}")
    _print_warnings(result.warnings)


@app.command()
def restore(
    ctx: typer.Context,
    commit: str = typer.Argument(..., help="Snapshot commit to restore."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Restore a previous snapshot safely."""
    cfg = _cfg(ctx)
    if not yes:
        typer.confirm(
            f"Replace {cfg.source} with snapshot {commit}? "
            "A safety snapshot will be created first.",
            abort=True,
        )
    try:
        result = restore_snapshot(cfg, commit)
    except ConfigVaultError as exc:
        _die(exc)
    typer.echo(f"restored: {_short(result.restored_commit)}")
    typer.echo(f"safety:   {_short(result.safety_commit)}")
    if not result.backup_removed:
        _warn("the previous live configuration backup could not be removed")
    _print_warnings(result.warnings)


@app.command()
def config(ctx: typer.Context) -> None:
    """Display configuration locations and settings."""
    cfg = _cfg(ctx)
    for key, value in cfg.display().items():
        typer.echo(f"{key:16} {value}")
    typer.echo(f"{'excludes':16} {len(cfg.exclude)} patterns")
    for pattern in cfg.exclude:
        typer.echo(f"  {pattern}")
    if cfg.public_manifest.exists():
        typer.echo(f"{'manifest':16} present")
    else:
        typer.echo(f"{'manifest':16} missing")
    try:
        typer.echo(f"{'managed public':16} {len(load_managed(cfg))} files")
    except ConfigVaultError as exc:
        _warn(str(exc))
    typer.echo(f"{'systemd units':16} {systemd_user_dir()}")


@app.command()
def version() -> None:
    """Print the ConfigVault version."""
    typer.echo(__version__)
