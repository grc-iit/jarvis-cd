"""Tests for builtin Gray-Scott progress semantics."""

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
        / "gray_scott"
        / "progress.py"
    )
    return load_progress_module(path)


def _package_module() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[3]
        / "builtin"
        / "builtin"
        / "gray_scott"
        / "pkg.py"
    )
    spec = spec_from_file_location("test_gray_scott_runtime_package", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load Gray-Scott package: {path}")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_gray_scott_tracks_completed_outputs_and_terminal_state() -> None:
    """Only completed writes and the application's done marker advance progress."""
    module = _progress_module()
    adapter = module.adapter_from_package(
        {
            "pkg_type": "builtin.gray_scott",
            "pkg_id": "reaction_diffusion",
            "steps": 1000,
            "out_every": 250,
            "outdir": "/scratch/run/gray-scott",
        }
    )
    assert adapter is not None

    first = adapter.observe_progress(
        "Gray-Scott 512x256  4 ranks  1000 steps\n"
        "  wrote /scratch/run/gray-scott/gs_000250.h5\n"
        "  wrote /scratch/run/gray-scott/gs_000500"
    )
    second = adapter.observe_progress(".h5\nDone.\n")

    observations = first + second
    assert [item.current for item in observations] == [0.0, 250.0, 500.0, 1000.0]
    assert [item.total for item in observations] == [1000.0] * 4
    assert observations[-1].state is ProgressState.COMPLETED
    assert observations[1].metadata["completion_signal"] == "hdf5_write_returned"
    assert observations[1].metadata["output_format"] == "hdf5"
    assert observations[1].metadata["output_path"] == (
        "/scratch/run/gray-scott/gs_000250.h5"
    )


def test_gray_scott_rejects_identity_mismatch_and_impossible_timestep() -> None:
    """Package configuration remains authoritative over observed output."""
    module = _progress_module()
    adapter = module.adapter_from_package(
        {"pkg_type": "builtin.gray_scott", "steps": 100}
    )
    assert adapter is not None

    with pytest.raises(ValueError, match="differs from.*configuration"):
        adapter.observe_progress("Gray-Scott 32x32  1 ranks  200 steps\n")

    with pytest.raises(ValueError, match="timestep exceeds"):
        adapter.observe_progress("wrote /tmp/gs_000101.h5\n")


def test_gray_scott_does_not_invent_a_total() -> None:
    """Malformed or absent totals leave progress explicitly indeterminate."""
    module = _progress_module()
    adapter = module.adapter_from_package(
        {
            "pkg_type": "builtin.gray_scott",
            "steps": float("inf"),
        }
    )
    assert adapter is not None

    observations = adapter.observe_progress("wrote /tmp/gs_000010.h5\nDone.")
    observations += adapter.finalize_progress()

    assert [item.current for item in observations] == [10.0, 10.0]
    assert all(item.total is None for item in observations)
    assert observations[-1].state is ProgressState.COMPLETED


def test_gray_scott_provider_is_package_local() -> None:
    """The factory cannot claim output from an unrelated package."""
    module = _progress_module()

    assert module.adapter_from_package({"pkg_type": "builtin.paraview"}) is None


def test_gray_scott_default_adios_mode_uses_native_terminal_signal() -> None:
    """The non-container launcher also reports progress through its ADIOS2 app."""
    module = _progress_module()
    adapter = module.adapter_from_package(
        {"pkg_type": "builtin.gray_scott", "steps": 100, "out_every": 10}
    )
    assert adapter is not None

    observations = adapter.observe_progress(
        "restart: from step 40\n"
        "steps: 100\n"
        "Simulation at step 50 writing output step 5\n"
        "Rank 0 - ET 900 - milliseconds\n"
    )

    assert [item.current for item in observations] == [40.0, 50.0, 100.0]
    assert observations[-1].state is ProgressState.COMPLETED
    assert observations[-1].metadata["completion_signal"] == (
        "writer_closed_and_timing_reported"
    )


class _CapturedExec:
    calls: list[tuple[str, Any]] = []

    def __init__(self, command: str, exec_info: Any) -> None:
        self.calls.append((command, exec_info))

    def run(self) -> "_CapturedExec":
        return self


class _CapturedMkdir:
    def __init__(self, _path: object, *_args: object, **_kwargs: object) -> None:
        pass

    def run(self) -> "_CapturedMkdir":
        return self


def test_gray_scott_runtime_streams_stdout_to_owned_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The package launcher wires its execution stream into JARVIS progress."""
    module = _package_module()
    package = object.__new__(module.GrayScott)

    def callback(_stream: str, _line: str) -> None:
        return None

    package.config = {
        "deploy_mode": "container",
        "nprocs": 2,
        "ppn": 2,
        "width": 64,
        "height": 64,
        "steps": 100,
        "out_every": 10,
        "outdir": "/tmp/gray_scott_test",
        "F": 0.035,
        "k": 0.06,
        "Du": 0.16,
        "Dv": 0.08,
    }
    package.mod_env = {}
    package.shared_dir = "/tmp/shared"
    package.private_dir = "/tmp/private"
    package.pipeline = SimpleNamespace(container_engine="apptainer")
    package.runtime_line_callback = lambda: callback
    package.deploy_image_name = lambda: "gray-scott:test"
    _CapturedExec.calls = []
    monkeypatch.setattr(module, "Exec", _CapturedExec)
    monkeypatch.setattr(module, "Mkdir", _CapturedMkdir)

    package.start()

    assert len(_CapturedExec.calls) == 1
    _, exec_info = _CapturedExec.calls[0]
    assert exec_info.line_callback is callback
