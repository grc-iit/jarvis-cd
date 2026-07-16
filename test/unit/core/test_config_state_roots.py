"""Canonical JARVIS state-root tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from jarvis_cd.core.config import Jarvis
from jarvis_cd.core.execution import ExecutionStore
from jarvis_cd.core.pkg import Pkg
from jarvis_cd.util.private_path import reject_private_path_redirection


def _directory_link(link: Path, target: Path) -> None:
    """Create a directory redirection supported by the current platform."""
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
        return
    link.symlink_to(target, target_is_directory=True)


def test_symlinked_home_is_canonicalized_before_durable_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A site-wide home alias must not enter JARVIS private execution paths."""
    canonical_home = tmp_path / "canonical-home"
    canonical_home.mkdir()
    logical_home = tmp_path / "logical-home"
    _directory_link(logical_home, canonical_home)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: logical_home))

    logical_roots = {
        "config_dir": logical_home / "config",
        "private_dir": logical_home / "private",
        "shared_dir": logical_home / "shared",
    }
    jarvis_root = logical_home / ".ppi-jarvis"
    Jarvis._instance = None
    try:
        jarvis = Jarvis(jarvis_root=str(jarvis_root))
        jarvis.initialize(
            config_dir=str(logical_roots["config_dir"]),
            private_dir=str(logical_roots["private_dir"]),
            shared_dir=str(logical_roots["shared_dir"]),
        )

        expected_roots = {
            name: str((canonical_home / path.name).resolve())
            for name, path in logical_roots.items()
        }
        assert jarvis.jarvis_root == (canonical_home / ".ppi-jarvis").resolve()
        assert {
            name: getattr(jarvis, name) for name in expected_roots
        } == expected_roots
        persisted = yaml.safe_load(jarvis.config_file.read_text(encoding="utf-8"))
        assert {name: persisted[name] for name in expected_roots} == expected_roots

        legacy = persisted.copy()
        legacy.update({name: str(path) for name, path in logical_roots.items()})
        jarvis.config_file.write_text(
            yaml.safe_dump(legacy, default_flow_style=False),
            encoding="utf-8",
        )

        Jarvis._instance = None
        reloaded = Jarvis(jarvis_root=str(jarvis_root))
        assert {
            name: getattr(reloaded, name) for name in expected_roots
        } == expected_roots

        executions_dir = reloaded.get_pipeline_private_dir("example") / "executions"
        store = ExecutionStore(executions_dir, "example")
        created = store.create("execution_canonical", mode="direct")
        observed = store.get(created.execution_id)
        assert observed.execution_id == created.execution_id
        reject_private_path_redirection(executions_dir)
    finally:
        Jarvis._instance = None


def test_legacy_home_migration_does_not_hide_descendant_redirection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the trusted home alias is migrated; a private-state link is rejected."""
    canonical_home = tmp_path / "canonical-home"
    canonical_home.mkdir()
    logical_home = tmp_path / "logical-home"
    _directory_link(logical_home, canonical_home)
    redirected_target = canonical_home / "redirected-target"
    redirected_target.mkdir()
    redirected_private = canonical_home / "redirected-private"
    _directory_link(redirected_private, redirected_target)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: logical_home))

    jarvis_root = canonical_home / ".ppi-jarvis"
    jarvis_root.mkdir()
    config_file = jarvis_root / "jarvis_config.yaml"
    config_file.write_text(
        yaml.safe_dump(
            {
                "config_dir": str(logical_home / "config"),
                "private_dir": str(logical_home / "redirected-private"),
                "shared_dir": str(logical_home / "shared"),
                "current_pipeline": None,
                "hostfile": None,
            },
            default_flow_style=False,
        ),
        encoding="utf-8",
    )

    Jarvis._instance = None
    try:
        jarvis = Jarvis(jarvis_root=str(jarvis_root))
        expected_private = canonical_home / "redirected-private"
        assert jarvis.private_dir == str(expected_private)
        store = ExecutionStore(
            jarvis.get_pipeline_private_dir("example") / "executions",
            "example",
        )
        with pytest.raises(RuntimeError, match="symbolic link or reparse point"):
            store.create("blocked", mode="direct")
        assert not (redirected_target / "example" / "executions").exists()
    finally:
        Jarvis._instance = None


def test_default_jarvis_root_canonicalizes_home_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The implicit JARVIS root follows the same trusted-root boundary."""
    canonical_home = tmp_path / "canonical-home"
    canonical_home.mkdir()
    logical_home = tmp_path / "logical-home"
    _directory_link(logical_home, canonical_home)
    monkeypatch.delenv("JARVIS_ROOT", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: logical_home))

    Jarvis._instance = None
    try:
        jarvis = Jarvis()
        assert jarvis.jarvis_root == (canonical_home / ".ppi-jarvis").resolve()
    finally:
        Jarvis._instance = None


def test_stale_managed_builtin_repo_binds_to_running_distribution(
    tmp_path: Path,
) -> None:
    """A wheel upgrade must not keep describing an old copied builtin tree."""
    jarvis_root = tmp_path / ".ppi-jarvis"
    legacy_builtin = jarvis_root / "builtin"
    legacy_package = legacy_builtin / "builtin" / "paraview" / "pkg.py"
    legacy_package.parent.mkdir(parents=True)
    legacy_package.write_text(
        "# stale managed copy intentionally lacks pvpython_bin\n",
        encoding="utf-8",
    )
    operator_repo = tmp_path / "operator-packages"
    operator_repo.mkdir()

    Jarvis._instance = None
    try:
        jarvis = Jarvis(jarvis_root=str(jarvis_root))
        jarvis.initialize(
            config_dir=str(tmp_path / "config"),
            private_dir=str(tmp_path / "private"),
            shared_dir=str(tmp_path / "shared"),
        )
        persisted_repositories = {"repos": [str(operator_repo), str(legacy_builtin)]}
        jarvis.save_repos(persisted_repositories)
        active_distribution_builtin = jarvis._distribution_builtin_repository()
        assert active_distribution_builtin is not None
        assert jarvis.repos == {
            "repos": [str(operator_repo), str(active_distribution_builtin)]
        }

        Jarvis._instance = None
        reloaded = Jarvis(jarvis_root=str(jarvis_root))
        distribution_builtin = reloaded._distribution_builtin_repository()
        assert distribution_builtin is not None
        assert reloaded.repos == {
            "repos": [str(operator_repo), str(distribution_builtin)]
        }
        assert yaml.safe_load(reloaded.repos_file.read_text(encoding="utf-8")) == (
            persisted_repositories
        )
        assert legacy_package.read_text(encoding="utf-8").startswith(
            "# stale managed copy"
        )

        package = Pkg.load_standalone("builtin.paraview")
        setting = next(
            item
            for item in package.configure_menu()
            if item.get("name") == "pvpython_bin"
        )
        assert setting == {
            "name": "pvpython_bin",
            "msg": "Path or command used to launch the ParaView service",
            "type": str,
            "default": "pvpython",
        }
        assert (
            Path(package.pkg_dir)
            .resolve()
            .is_relative_to(distribution_builtin.resolve())
        )
    finally:
        Jarvis._instance = None


def test_explicit_operator_builtin_repository_is_not_rebound(
    tmp_path: Path,
) -> None:
    """Only JARVIS's exact legacy copy path is distribution-managed."""
    jarvis_root = tmp_path / ".ppi-jarvis"
    operator_builtin = tmp_path / "operator" / "builtin"
    marker = operator_builtin / "builtin" / "site_package" / "pkg.py"
    marker.parent.mkdir(parents=True)
    marker.write_text("# operator-owned package\n", encoding="utf-8")

    Jarvis._instance = None
    try:
        jarvis = Jarvis(jarvis_root=str(jarvis_root))
        jarvis.initialize(
            config_dir=str(tmp_path / "config"),
            private_dir=str(tmp_path / "private"),
            shared_dir=str(tmp_path / "shared"),
        )
        repositories = {"repos": [str(operator_builtin)]}
        jarvis.save_repos(repositories)

        Jarvis._instance = None
        reloaded = Jarvis(jarvis_root=str(jarvis_root))
        assert reloaded.repos == repositories
        assert reloaded.get_builtin_repo_path() == operator_builtin
        assert marker.read_text(encoding="utf-8") == "# operator-owned package\n"
    finally:
        Jarvis._instance = None
