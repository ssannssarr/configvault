from __future__ import annotations

from pathlib import Path

import pytest

from configvault.config import Config
from configvault.errors import ConfigError


def test_config_defaults_follow_custom_source_and_data_dir(tmp_path: Path) -> None:
    source = tmp_path / "custom-config"
    source.mkdir()
    config_path = tmp_path / "configvault.toml"
    config_path.write_text(
        f'source = "{source}"\ndata_dir = "{tmp_path / "custom-data"}"\n',
        encoding="utf-8",
    )

    cfg = Config.load(config_path)

    assert cfg.source == source
    assert cfg.data_dir == tmp_path / "custom-data"
    assert cfg.public_dir == tmp_path / "custom-data" / "public"
    assert cfg.public_manifest == source / ".gitinclude"


def test_config_roundtrip(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    cfg = Config(
        config_path=tmp_path / "config.toml",
        source=source,
        data_dir=tmp_path / "data",
        public_dir=tmp_path / "data" / "public",
        public_manifest=source / ".gitinclude",
        private_remote="git@example.invalid:private.git",
        public_remote="git@example.invalid:public.git",
        branch="main",
    )
    cfg.save()

    loaded = Config.load(cfg.config_path)

    assert loaded == cfg


@pytest.mark.parametrize("placement", ["data", "public"])
def test_config_rejects_repositories_inside_source(
    tmp_path: Path, placement: str
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    data = tmp_path / "data"
    public = tmp_path / "public"
    if placement == "data":
        data = source / "data"
    else:
        public = source / "public"

    cfg = Config(
        config_path=tmp_path / "config.toml",
        source=source,
        data_dir=data,
        public_dir=public,
        public_manifest=source / ".gitinclude",
    )
    with pytest.raises(ConfigError, match="must not be inside source"):
        cfg.validate_paths()


def test_config_rejects_symlinked_source(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "source"
    link.symlink_to(real)
    cfg = Config(
        config_path=tmp_path / "config.toml",
        source=link,
        data_dir=tmp_path / "data",
        public_dir=tmp_path / "data" / "public",
        public_manifest=link / ".gitinclude",
    )
    with pytest.raises(ConfigError, match="not a symlink"):
        cfg.require_source()
