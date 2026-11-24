"""Leaving the builtin package repository out (``JARVIS_NO_BUILTIN``/``no_builtin``)."""

from __future__ import annotations

import site
from pathlib import Path
from typing import Iterator

import pytest
import yaml

from jarvis_cd.core.cli import JarvisCLI
from jarvis_cd.core.config import (
    NO_BUILTIN_CONFIG_KEY,
    NO_BUILTIN_ENVIRONMENT,
    Jarvis,
)
from jarvis_cd.core.pkg import Pkg
from jarvis_cd.core.repository import RepositoryManager


@pytest.fixture(autouse=True)
def _fresh_singleton(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv(NO_BUILTIN_ENVIRONMENT, raising=False)
    Jarvis._instance = None
    yield
    Jarvis._instance = None


def _initialize(tmp_path: Path, **kwargs) -> Jarvis:
    jarvis = Jarvis(jarvis_root=str(tmp_path / ".ppi-jarvis"))
    jarvis.initialize(
        config_dir=str(tmp_path / "config"),
        private_dir=str(tmp_path / "private"),
        shared_dir=str(tmp_path / "shared"),
        **kwargs,
    )
    return jarvis


def _reload(tmp_path: Path) -> Jarvis:
    Jarvis._instance = None
    return Jarvis(jarvis_root=str(tmp_path / ".ppi-jarvis"))


def _site_packages_builtin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A ``builtin`` package installed in a (fake) site-packages directory."""
    site_packages = tmp_path / "site-packages"
    package = site_packages / "builtin" / "ior"
    package.mkdir(parents=True)
    (site_packages / "builtin" / "__init__.py").write_text("", encoding="utf-8")
    (package / "pkg.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(site, "getsitepackages", lambda: [str(site_packages)])
    monkeypatch.setattr(site, "getusersitepackages", lambda: str(site_packages))
    return site_packages / "builtin"


def test_default_init_still_registers_builtin(tmp_path: Path) -> None:
    jarvis = _initialize(tmp_path)

    assert jarvis.builtin_disabled is False
    assert NO_BUILTIN_CONFIG_KEY not in jarvis.config
    builtin_path = jarvis.get_builtin_repo_path()
    assert builtin_path is not None
    assert [Path(entry).name for entry in jarvis.repos["repos"]] == ["builtin"]
    assert jarvis.find_package("ior") == "builtin.ior"


def test_environment_leaves_builtin_out_of_init_and_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _site_packages_builtin(tmp_path, monkeypatch)
    monkeypatch.setenv(NO_BUILTIN_ENVIRONMENT, "1")

    jarvis = _initialize(tmp_path)

    assert jarvis.builtin_disabled is True
    assert jarvis.config[NO_BUILTIN_CONFIG_KEY] is True
    assert yaml.safe_load(jarvis.repos_file.read_text(encoding="utf-8")) == {
        "repos": []
    }
    assert jarvis.repos == {"repos": []}
    assert jarvis.get_builtin_repo_path() is None
    assert jarvis.find_package("ior") is None
    assert jarvis.list_builtin_resource_graphs() == {}
    assert RepositoryManager(jarvis).find_all_packages() == {}
    with pytest.raises(ValueError, match=NO_BUILTIN_ENVIRONMENT):
        Pkg.load_standalone("builtin.ior")
    with pytest.raises(ValueError, match="Package not found: ior"):
        Pkg.load_standalone("ior")


def test_config_key_persists_and_ignores_every_builtin_entry(tmp_path: Path) -> None:
    _initialize(tmp_path)
    operator_builtin = tmp_path / "operator" / "builtin"
    (operator_builtin / "builtin" / "site_package").mkdir(parents=True)
    operator_repo = tmp_path / "operator_repo"
    (operator_repo / "operator_repo").mkdir(parents=True)
    jarvis = _reload(tmp_path)
    jarvis.save_repos(
        {
            "repos": [
                str(operator_repo),
                str(operator_builtin),
                str(tmp_path / ".ppi-jarvis" / "builtin"),
            ]
        }
    )

    jarvis = _reload(tmp_path)
    jarvis.initialize(
        config_dir=str(tmp_path / "config"),
        private_dir=str(tmp_path / "private"),
        shared_dir=str(tmp_path / "shared"),
        no_builtin=True,
    )

    assert (
        yaml.safe_load(jarvis.config_file.read_text(encoding="utf-8"))[
            NO_BUILTIN_CONFIG_KEY
        ]
        is True
    )
    assert yaml.safe_load(jarvis.repos_file.read_text(encoding="utf-8")) == {
        "repos": [str(operator_repo)]
    }

    reloaded = _reload(tmp_path)
    assert reloaded.builtin_disabled is True
    assert reloaded.repos == {"repos": [str(operator_repo)]}
    assert reloaded.get_builtin_repo_path() is None
    with pytest.raises(ValueError, match="cannot add"):
        reloaded.add_repo(str(operator_builtin))

    # A later init without the switch keeps the root's setting.
    reloaded.initialize(
        config_dir=str(tmp_path / "config"),
        private_dir=str(tmp_path / "private"),
        shared_dir=str(tmp_path / "shared"),
    )
    assert _reload(tmp_path).builtin_disabled is True


def test_repos_yaml_builtin_entries_are_hidden_without_rewriting(
    tmp_path: Path,
) -> None:
    jarvis = _initialize(tmp_path)
    persisted = yaml.safe_load(jarvis.repos_file.read_text(encoding="utf-8"))
    config = dict(jarvis.config)
    config[NO_BUILTIN_CONFIG_KEY] = True
    jarvis.save_config(config)

    reloaded = _reload(tmp_path)
    assert reloaded.repos == {"repos": []}
    assert yaml.safe_load(reloaded.repos_file.read_text(encoding="utf-8")) == (
        persisted
    )


def test_environment_overrides_config_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _initialize(tmp_path, no_builtin=True)

    monkeypatch.setenv(NO_BUILTIN_ENVIRONMENT, "off")
    reloaded = _reload(tmp_path)
    assert reloaded.builtin_disabled is False
    assert reloaded.get_builtin_repo_path() is not None


def test_invalid_environment_value_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jarvis = _initialize(tmp_path)
    monkeypatch.setenv(NO_BUILTIN_ENVIRONMENT, "maybe")
    with pytest.raises(ValueError, match=NO_BUILTIN_ENVIRONMENT):
        _ = jarvis.builtin_disabled


def test_cli_init_records_no_builtin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVIS_ROOT", str(tmp_path / ".ppi-jarvis"))
    cli = JarvisCLI()
    cli.define_options()
    cli.parse(
        [
            "init",
            str(tmp_path / "config"),
            str(tmp_path / "private"),
            str(tmp_path / "shared"),
            "+no_builtin",
        ]
    )

    reloaded = _reload(tmp_path)
    assert reloaded.config[NO_BUILTIN_CONFIG_KEY] is True
    assert reloaded.repos == {"repos": []}
    assert reloaded.get_builtin_repo_path() is None
