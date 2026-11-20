"""Secret and suspicious-path safeguards.

These are heuristics, not a guarantee.  They exist to stop the most common
accidental publication of credentials and private keys.  A private Git
repository is still plaintext storage on GitHub.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from urllib.parse import urlsplit

SECRET_BASENAMES = {
    ".git-credentials",
    ".netrc",
    ".env",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "credentials",
    "credentials.json",
    "token",
    "token.json",
    "secrets",
    "secret",
}
SECRET_SUFFIXES = (
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".kdbx",
    ".keystore",
    ".jks",
)
SECRET_DIR_NAMES = {
    "keyrings",
    "gcloud",
    "gnupg",
    ".gnupg",
    "secrets",
    ".secrets",
    "credentials",
}
SECRET_WORDS = ("credential", "secret", "private_key", "access_token", "auth_token")


def is_tool_metadata(relpath: str) -> bool:
    """Paths ConfigVault itself owns and never publishes."""
    parts = PurePosixPath(relpath).parts
    if any(part == ".git" for part in parts):
        return True
    return relpath in {"configvault.toml", ".gitinclude"}


def suspicious_reason(relpath: str) -> str | None:
    path = PurePosixPath(relpath)
    parts = [part.lower() for part in path.parts]
    name = parts[-1] if parts else ""
    if not name:
        return None
    if any(part in SECRET_DIR_NAMES for part in parts[:-1]):
        return "inside a credential/private-key directory"
    if name in SECRET_BASENAMES:
        return "known credential or key filename"
    if name.startswith(".env."):
        return "environment secret file"
    if any(name.endswith(suffix) for suffix in SECRET_SUFFIXES):
        return "private-key or credential container extension"
    if any(word in name for word in SECRET_WORDS):
        return "secret-like filename"
    if name in {"hosts.yml", "hosts.yaml"} and "gh" in parts[:-1]:
        return "GitHub CLI credential store"
    return None


def suspicious_paths(paths: list[str]) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for relpath in paths:
        reason = suspicious_reason(relpath)
        if reason:
            found.append((relpath, reason))
    return found


def remote_has_credentials(url: str | None) -> bool:
    if not url:
        return False
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        try:
            parsed = urlsplit(url)
        except ValueError:
            return True
        return bool(parsed.username or parsed.password)
    # scp-like syntax: user@host:path is fine; user:password@host is not.
    return bool(re.match(r"^[^/@:]+:[^/@]+@", url))
