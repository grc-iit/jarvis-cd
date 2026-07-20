"""Cross-process acceptance tests for resource-graph activation."""

from __future__ import annotations

import json
import hashlib
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from jarvis_cd.core.cli import JarvisCLI
from jarvis_cd.core.config import Jarvis
from jarvis_cd.core.resource_graph import ResourceGraphManager


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _run_jarvis(
    *arguments: str,
    jarvis_root: Path,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run the real JARVIS CLI in a fresh, bounded interpreter."""
    environment = os.environ.copy()
    environment["JARVIS_ROOT"] = str(jarvis_root)
    return subprocess.run(
        [sys.executable, "-m", "jarvis_cd.core.cli", *arguments],
        cwd=PROJECT_ROOT,
        env=environment,
        check=check,
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_rg_load_atomically_activates_graph_for_a_new_process(tmp_path: Path) -> None:
    """A graph loaded by one CLI process must be active in the next process."""
    jarvis_root = tmp_path / "custom-jarvis-root"
    source = tmp_path / "ares.yaml"
    source.write_text(
        """fs:
- avail: 500GB
  dev_type: ssd
  device: /dev/sdb1
  fs_type: xfs
  mount: /mnt/ssd/tester
  shared: false
""",
        encoding="utf-8",
    )
    source_before = source.read_bytes()

    _run_jarvis(
        "init",
        str(tmp_path / "config"),
        str(tmp_path / "private"),
        str(tmp_path / "shared"),
        jarvis_root=jarvis_root,
    )
    loaded = _run_jarvis("rg", "load", str(source), jarvis_root=jarvis_root)

    active_graph = jarvis_root / "resource_graph.yaml"
    assert active_graph.is_file()
    assert str(active_graph) in loaded.stdout
    assert source.read_bytes() == source_before
    assert not list(jarvis_root.glob(".resource_graph.*.tmp"))
    if os.name != "nt":
        assert stat.S_IMODE(active_graph.stat().st_mode) == 0o600

    observed = _run_jarvis("rg", "show", jarvis_root=jarvis_root)
    assert "/mnt/ssd/tester" in observed.stdout
    assert "dev_type: ssd" in observed.stdout


@pytest.mark.parametrize("activation", ["load", "load_builtin"])
def test_failed_replace_preserves_file_cache_and_manager_graph(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    activation: str,
) -> None:
    """A failed atomic replace must leave all active views unchanged."""
    jarvis_root = tmp_path / "custom-jarvis-root"
    monkeypatch.setenv("JARVIS_ROOT", str(jarvis_root))
    Jarvis._instance = None
    try:
        jarvis = Jarvis.get_instance()
        jarvis.initialize(
            str(tmp_path / "config"),
            str(tmp_path / "private"),
            str(tmp_path / "shared"),
        )
        jarvis.save_resource_graph(
            {"fs": [{"mount": "/old", "dev_type": "hdd", "shared": True}]}
        )
        active_before = jarvis.resource_graph_file.read_bytes()
        manager = ResourceGraphManager()
        source = tmp_path / "replacement.yaml"
        source.write_text(
            "fs:\n- mount: /new\n  dev_type: ssd\n  shared: true\n",
            encoding="utf-8",
        )

        def fail_replace(_source: object, _destination: object) -> None:
            raise OSError("simulated atomic replacement failure")

        monkeypatch.setattr("jarvis_cd.core.config.os.replace", fail_replace)
        with pytest.raises(OSError, match="simulated atomic replacement failure"):
            if activation == "load":
                manager.load(source)
            else:
                monkeypatch.setattr(
                    jarvis,
                    "get_builtin_resource_graph_path",
                    lambda _profile: source,
                )
                manager.load_builtin("fixture")

        assert jarvis.resource_graph_file.read_bytes() == active_before
        assert jarvis.resource_graph["fs"][0]["mount"] == "/old"
        mounts = {
            device["mount"]
            for node in manager.resource_graph.get_all_nodes()
            for device in manager.resource_graph.get_node_storage(node)
        }
        assert mounts == {"/old"}
        assert not list(jarvis_root.glob(".resource_graph.*.tmp"))
    finally:
        Jarvis._instance = None


@pytest.mark.parametrize("activation", ["load", "load_builtin"])
def test_post_replace_fsync_failure_keeps_every_live_graph_coherent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    activation: str,
) -> None:
    """A post-replace error must expose the new graph in all three live views."""
    jarvis_root = tmp_path / "custom-jarvis-root"
    monkeypatch.setenv("JARVIS_ROOT", str(jarvis_root))
    Jarvis._instance = None
    try:
        jarvis = Jarvis.get_instance()
        jarvis.initialize(
            str(tmp_path / "config"),
            str(tmp_path / "private"),
            str(tmp_path / "shared"),
        )
        jarvis.save_resource_graph(
            {"fs": [{"mount": "/old", "dev_type": "hdd", "shared": True}]}
        )
        manager = ResourceGraphManager()
        source = tmp_path / "replacement.yaml"
        source.write_text(
            "fs:\n- mount: /new\n  dev_type: ssd\n  shared: true\n",
            encoding="utf-8",
        )

        def fail_directory_fsync(_path: Path) -> None:
            raise OSError("simulated directory fsync failure")

        monkeypatch.setattr(
            "jarvis_cd.core.config._fsync_directory", fail_directory_fsync
        )
        with pytest.raises(OSError, match="simulated directory fsync failure"):
            if activation == "load":
                manager.load(source)
            else:
                monkeypatch.setattr(
                    jarvis,
                    "get_builtin_resource_graph_path",
                    lambda _profile: source,
                )
                manager.load_builtin("fixture")

        persisted = yaml.safe_load(
            jarvis.resource_graph_file.read_text(encoding="utf-8")
        )
        assert persisted["fs"][0]["mount"] == "/new"
        assert jarvis.resource_graph == persisted
        mounts = {
            device["mount"]
            for node in manager.resource_graph.get_all_nodes()
            for device in manager.resource_graph.get_node_storage(node)
        }
        assert mounts == {"/new"}
        assert not list(jarvis_root.glob(".resource_graph.*.tmp"))
    finally:
        Jarvis._instance = None


def test_load_builtin_activates_packaged_graph_for_a_new_process(
    tmp_path: Path,
) -> None:
    """JARVIS must own catalog resolution and durable builtin activation."""
    jarvis_root = tmp_path / "custom-jarvis-root"
    _run_jarvis(
        "init",
        str(tmp_path / "config"),
        str(tmp_path / "private"),
        str(tmp_path / "shared"),
        jarvis_root=jarvis_root,
    )

    catalog = _run_jarvis("rg", "builtins", jarvis_root=jarvis_root)
    assert "ares" in catalog.stdout.splitlines()
    loaded = _run_jarvis("rg", "load-builtin", "ares", "+json", jarvis_root=jarvis_root)
    result = json.loads(loaded.stdout)
    assert result == {
        "action": "loaded",
        "available": True,
        "catalog": ["ares", "deception", "delta", "g2-standard-4", "polaris"],
        "profile": "ares",
        "schema_version": "jarvis.resource-graph-builtin.v1",
        "source": result["source"],
        "source_sha256": result["source_sha256"],
    }
    assert result["source"].endswith("/builtin/resource_graph/ares.yaml") or result[
        "source"
    ].endswith("\\builtin\\resource_graph\\ares.yaml")
    assert (
        result["source_sha256"]
        == hashlib.sha256(Path(result["source"]).read_bytes()).hexdigest()
    )

    observed = _run_jarvis("rg", "show", jarvis_root=jarvis_root)
    assert "/mnt/ssd" in observed.stdout
    assert "dev_type: ssd" in observed.stdout


@pytest.mark.parametrize("profile", ["missing-cluster", "../ares", "ares/path"])
def test_load_builtin_reports_exact_availability_boundary(
    tmp_path: Path,
    profile: str,
) -> None:
    """Unknown and unsafe names must fail without relay-owned path guessing."""
    jarvis_root = tmp_path / "custom-jarvis-root"
    _run_jarvis(
        "init",
        str(tmp_path / "config"),
        str(tmp_path / "private"),
        str(tmp_path / "shared"),
        jarvis_root=jarvis_root,
    )

    result = _run_jarvis(
        "rg",
        "load-builtin",
        profile,
        "+json",
        jarvis_root=jarvis_root,
        check=False,
    )
    output = result.stdout + result.stderr
    if profile == "missing-cluster":
        assert result.returncode == 0
        document = json.loads(result.stdout)
        assert document["schema_version"] == "jarvis.resource-graph-builtin.v1"
        assert document["profile"] == profile
        assert document["action"] == "unavailable"
        assert document["available"] is False
        assert document["source"] is None
        assert document["source_sha256"] is None
        assert "ares" in document["catalog"]
    else:
        assert result.returncode != 0
        assert "jarvis.resource-graph-builtin.v1" not in result.stdout
        assert "must be one safe exact name" in output


def test_load_builtin_json_keeps_corrupt_profile_as_a_hard_error(
    tmp_path: Path,
) -> None:
    """Only a clean catalog miss is structured as unavailable."""
    jarvis_root = tmp_path / "custom-jarvis-root"
    _run_jarvis(
        "init",
        str(tmp_path / "config"),
        str(tmp_path / "private"),
        str(tmp_path / "shared"),
        jarvis_root=jarvis_root,
    )
    operator_builtin = tmp_path / "operator" / "builtin"
    graph_root = operator_builtin / "resource_graph"
    graph_root.mkdir(parents=True)
    (graph_root / "corrupt.yaml").write_text("fs: [", encoding="utf-8")
    (jarvis_root / "repos.yaml").write_text(
        yaml.safe_dump({"repos": [str(operator_builtin)]}),
        encoding="utf-8",
    )

    result = _run_jarvis(
        "rg",
        "load-builtin",
        "corrupt",
        "+json",
        jarvis_root=jarvis_root,
        check=False,
    )
    assert result.returncode != 0
    assert "jarvis.resource-graph-builtin.v1" not in result.stdout
    assert "Error:" in result.stdout + result.stderr


def test_builtin_catalog_rejects_unsafe_discovered_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unsafe on-disk names must fail before entering human or JSON output."""
    jarvis_root = tmp_path / "custom-jarvis-root"
    monkeypatch.setenv("JARVIS_ROOT", str(jarvis_root))
    Jarvis._instance = None
    try:
        jarvis = Jarvis.get_instance()
        graph_root = tmp_path / "operator" / "builtin" / "resource_graph"
        graph_root.mkdir(parents=True)
        unsafe = graph_root / "unsafe\nprofile.yaml"
        original_iterdir = Path.iterdir
        original_is_file = Path.is_file

        def fake_iterdir(path: Path):
            if path == graph_root:
                return iter([unsafe])
            return original_iterdir(path)

        def fake_is_file(path: Path) -> bool:
            if path == unsafe:
                return True
            return original_is_file(path)

        monkeypatch.setattr(jarvis, "get_builtin_repo_path", lambda: graph_root.parent)
        monkeypatch.setattr(Path, "iterdir", fake_iterdir)
        monkeypatch.setattr(Path, "is_file", fake_is_file)

        with pytest.raises(ValueError, match="must be one safe exact name"):
            jarvis.list_builtin_resource_graphs()
    finally:
        Jarvis._instance = None


def test_load_builtin_json_does_not_relabel_selected_source_disappearance(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A catalog hit that vanishes during I/O is a hard error, not unavailable."""

    class VanishingSourceManager:
        def list_builtins(self) -> list[str]:
            return ["ares"]

        def load_builtin(self, _profile: str) -> tuple[Path, str]:
            raise FileNotFoundError("selected builtin source vanished")

    cli = JarvisCLI()
    cli.kwargs = {"profile": "ares", "json": True}
    monkeypatch.setattr(cli, "rg_manager", VanishingSourceManager())
    monkeypatch.setattr(cli, "_ensure_initialized", lambda: None)

    with pytest.raises(FileNotFoundError, match="selected builtin source vanished"):
        cli.rg_load_builtin()
    assert "jarvis.resource-graph-builtin.v1" not in capsys.readouterr().out
