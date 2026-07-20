"""Cross-process acceptance tests for resource-graph activation."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from jarvis_cd.core.config import Jarvis
from jarvis_cd.core.resource_graph import ResourceGraphManager


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _run_jarvis(*arguments: str, jarvis_root: Path) -> subprocess.CompletedProcess[str]:
    """Run the real JARVIS CLI in a fresh, bounded interpreter."""
    environment = os.environ.copy()
    environment["JARVIS_ROOT"] = str(jarvis_root)
    return subprocess.run(
        [sys.executable, "-m", "jarvis_cd.core.cli", *arguments],
        cwd=PROJECT_ROOT,
        env=environment,
        check=True,
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


def test_rg_load_failed_activation_preserves_file_and_in_memory_graph(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed atomic replace must leave both active views unchanged."""
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
            manager.load(source)

        assert jarvis.resource_graph_file.read_bytes() == active_before
        mounts = {
            device["mount"]
            for node in manager.resource_graph.get_all_nodes()
            for device in manager.resource_graph.get_node_storage(node)
        }
        assert mounts == {"/old"}
        assert not list(jarvis_root.glob(".resource_graph.*.tmp"))
    finally:
        Jarvis._instance = None
