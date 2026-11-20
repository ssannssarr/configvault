"""Typed errors used to keep CLI failures understandable."""


class ConfigVaultError(Exception):
    """Base error for expected ConfigVault failures."""


class ConfigError(ConfigVaultError):
    """Invalid or unsafe configuration."""


class SnapshotError(ConfigVaultError):
    """A snapshot could not be completed safely."""


class GitError(ConfigVaultError):
    """A Git operation failed."""


class DivergenceError(GitError):
    """Local and remote histories have diverged."""


class ManifestError(ConfigVaultError):
    """The public selection manifest is invalid."""


class ExportError(ConfigVaultError):
    """A public export was refused or failed."""


class RestoreError(ConfigVaultError):
    """A restore could not be completed safely."""
