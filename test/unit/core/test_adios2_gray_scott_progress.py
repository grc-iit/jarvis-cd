"""Tests for builtin ADIOS2 Gray-Scott progress semantics."""

from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from jarvis_cd.progress import ProgressState, load_progress_module


def _progress_module() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[3]
        / "builtin"
        / "builtin"
        / "adios2_gray_scott"
        / "progress.py"
    )
    return load_progress_module(path)


def _package_module() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[3]
        / "builtin"
        / "builtin"
        / "adios2_gray_scott"
        / "pkg.py"
    )
    spec = spec_from_file_location("test_adios2_gray_scott_runtime_package", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load ADIOS2 Gray-Scott package: {path}")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_adios2_gray_scott_tracks_restart_outputs_and_completion() -> None:
    """Native simulation messages expose restart-aware, durable progress."""
    module = _progress_module()
    adapter = module.adapter_from_package(
        {
            "pkg_type": "builtin.adios2_gray_scott",
            "pkg_id": "gray_scott_bp5",
            "steps": 100,
            "plotgap": 10,
            "out_file": "/scratch/run/gs.bp",
            "checkpoint_output": "/scratch/run/ckpt.bp",
        }
    )
    assert adapter is not None

    observations = adapter.observe_progress(
        "restart:          from step 40\n"
        "steps:            100\n"
        "Simulation at step 50 writing output step     5\n"
        "Simulation at step 60 writing output step     6\n"
        "Rank 0 - ET 1234 - milliseconds\n"
        "Rank 1 - ET 1235 - milliseconds\n"
    )

    assert [item.current for item in observations] == [40.0, 50.0, 60.0, 100.0]
    assert [item.total for item in observations] == [100.0] * 4
    assert observations[-1].state is ProgressState.COMPLETED
    assert observations[1].metadata["completion_signal"] == ("compute_step_completed")
    assert observations[1].metadata["output_write_state"] == "started"
    assert observations[-1].metadata["completion_signal"] == (
        "writer_closed_and_timing_reported"
    )


def test_adios2_gray_scott_rejects_untruthful_native_output() -> None:
    """Conflicting totals and output ordinals fail instead of being normalized."""
    module = _progress_module()
    adapter = module.adapter_from_package(
        {
            "pkg_type": "builtin.adios2_gray_scott",
            "steps": 100,
            "plotgap": 10,
        }
    )
    assert adapter is not None

    with pytest.raises(ValueError, match="differs from.*configuration"):
        adapter.observe_progress("steps: 200\n")

    with pytest.raises(ValueError, match="contradicts plotgap"):
        adapter.observe_progress("Simulation at step 20 writing output step 3\n")


def test_adios2_gray_scott_factory_is_package_local() -> None:
    """The ADIOS2 provider cannot claim another package's lifecycle."""
    module = _progress_module()

    assert module.adapter_from_package({"pkg_type": "builtin.gray_scott"}) is None


class _CapturedExec:
    calls: list[tuple[str, Any]] = []

    def __init__(self, command: str, exec_info: Any) -> None:
        self.calls.append((command, exec_info))

    def run(self) -> "_CapturedExec":
        return self


def test_adios2_gray_scott_runtime_streams_stdout_to_owned_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ADIOS2 launcher wires its process stream into JARVIS progress."""
    module = _package_module()
    package = object.__new__(module.Adios2GrayScott)

    def callback(_stream: str, _line: str) -> None:
        return None

    package.config = {
        "engine": "bp5",
        "nprocs": 2,
        "ppn": 2,
        "run_async": False,
    }
    package.settings_json_path = "/tmp/settings.json"
    package.pipeline = SimpleNamespace(get_hostfile=lambda: object())
    package.mod_env = {}
    package.runtime_line_callback = lambda: callback
    _CapturedExec.calls = []
    monkeypatch.setattr(module, "Exec", _CapturedExec)

    package.start()

    assert len(_CapturedExec.calls) == 1
    _, exec_info = _CapturedExec.calls[0]
    assert exec_info.line_callback is callback
