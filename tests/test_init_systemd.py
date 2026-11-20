from __future__ import annotations

from pathlib import Path

from configvault import git
from configvault.config import Config
from configvault.init import initialize
from configvault.systemd import install_units, render_units


def test_init_creates_layout_and_manifest(cfg: Config) -> None:
    result = initialize(cfg)

    assert (cfg.repo_dir / ".git").is_dir()
    assert (cfg.public_dir / ".git").is_dir()
    assert cfg.public_manifest.exists()
    assert "ConfigVault public export manifest" in cfg.public_manifest.read_text()
    assert result.manifest_created is True
    assert result.warnings == []


def test_init_is_idempotent(cfg: Config) -> None:
    first = initialize(cfg)
    second = initialize(cfg)

    assert first.manifest_created is True
    assert second.manifest_created is False
    assert cfg.public_manifest.exists()


def test_init_creates_config_for_new_path(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    config = Config(
        config_path=tmp_path / "new.toml",
        source=source,
        data_dir=tmp_path / "data",
        public_dir=tmp_path / "data" / "public",
        public_manifest=source / ".gitinclude",
    )

    result = initialize(config, private_remote="git@example.invalid:me/private.git")

    assert result.config_created is True
    assert config.config_path.exists()
    loaded = Config.load(config.config_path)
    assert loaded.private_remote == "git@example.invalid:me/private.git"
    assert git.get_remote(loaded.repo_dir) == "git@example.invalid:me/private.git"


def test_init_warns_about_credentials_in_remote(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    config = Config(
        config_path=tmp_path / "config.toml",
        source=source,
        data_dir=tmp_path / "data",
        public_dir=tmp_path / "data" / "public",
        public_manifest=source / ".gitinclude",
    )

    result = initialize(config, private_remote="https://token@example.invalid/x.git")

    assert any("embedded credentials" in warning for warning in result.warnings)


def test_systemd_units_match_requested_schedule() -> None:
    service, timer = render_units()

    assert "Type=oneshot" in service
    assert "ExecStart=" in service
    assert "snapshot" in service
    assert "OnCalendar=*-*-* 03:00:00" in timer
    assert "Persistent=true" in timer
    assert "RandomizedDelaySec=15m" in timer
    assert "WantedBy=timers.target" in timer
    assert "Restart=always" not in service


def test_install_units_uses_xdg_config_home(cfg: Config, monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    paths = install_units(cfg)

    expected = tmp_path / "xdg" / "systemd" / "user"
    assert paths == [expected / "configvault.service", expected / "configvault.timer"]
    assert all(path.exists() for path in paths)
