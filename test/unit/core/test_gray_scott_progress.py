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
    terminal = adapter.finalize_progress()

    observations = first + second + terminal
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
    observations += adapter.finalize_progress()

    assert [item.current for item in observations] == [40.0, 50.0, 100.0]
    assert observations[-1].state is ProgressState.COMPLETED
    assert observations[-1].metadata["completion_signal"] == (
        "writer_closed_and_timing_reported"
    )


def test_gray_scott_direct_iowarp_run_uses_successful_process_exit() -> None:
    """The direct clio-core binary needs no fabricated terminal stdout line."""
    module = _progress_module()
    adapter = module.adapter_from_package(
        {"pkg_type": "builtin.gray_scott", "steps": 20, "out_every": 10}
    )
    assert adapter is not None

    observations = adapter.observe_progress(
        "steps: 20\n"
        "Simulation at step 10 writing output step 1\n"
        "Simulation at step 20 writing output step 2\n"
    )
    observations += adapter.finalize_progress_for_exit(0)

    assert [item.current for item in observations] == [0.0, 10.0, 20.0, 20.0]
    assert observations[-1].state is ProgressState.COMPLETED
    assert observations[-1].metadata["completion_signal"] == (
        "process_exit_zero_after_final_output"
    )


def test_gray_scott_failed_process_is_not_reported_completed() -> None:
    """A nonzero direct process exit terminates the last truthful state."""
    module = _progress_module()
    adapter = module.adapter_from_package(
        {"pkg_type": "builtin.gray_scott", "steps": 20, "out_every": 10}
    )
    assert adapter is not None

    observations = adapter.observe_progress(
        "steps: 20\nSimulation at step 10 writing output step 1\n"
    )
    observations += adapter.finalize_progress_for_exit(3)

    assert observations[-1].state is ProgressState.FAILED
    assert observations[-1].current == 10.0
    assert observations[-1].metadata["return_code"] == 3


def test_gray_scott_done_marker_cannot_override_failed_process_exit() -> None:
    """Application success text remains provisional until the process exits."""
    module = _progress_module()
    adapter = module.adapter_from_package(
        {"pkg_type": "builtin.gray_scott", "steps": 20, "out_every": 10}
    )
    assert adapter is not None

    observations = adapter.observe_progress(
        "steps: 20\nSimulation at step 10 writing output step 1\nDone.\n"
    )
    observations += adapter.finalize_progress_for_exit(9)

    assert all(item.state is not ProgressState.COMPLETED for item in observations)
    assert observations[-1].state is ProgressState.FAILED
    assert observations[-1].metadata["completion_signal"] == "process_exit_nonzero"
    assert observations[-1].metadata["application_completion_signal"] == (
        "application_reported_done"
    )


class _CapturedExec:
    calls: list[tuple[str, Any]] = []
    exit_codes: dict[str, int] = {"localhost": 0}

    def __init__(self, command: str, exec_info: Any) -> None:
        self.calls.append((command, exec_info))
        self.exit_code = dict(self.exit_codes)

    def run(self) -> "_CapturedExec":
        return self


class _CapturedMkdir:
    exit_codes: dict[str, int] = {"localhost": 0}

    def __init__(self, path: object, *_args: object, **_kwargs: object) -> None:
        self.path = path
        self.exit_code = dict(self.exit_codes)

    def run(self) -> "_CapturedMkdir":
        return self


class _CapturedJsonFile:
    documents: list[dict[str, object]] = []

    def __init__(self, _path: object) -> None:
        pass

    def save(self, document: dict[str, object]) -> None:
        self.documents.append(document)


def test_gray_scott_configuration_matches_direct_iowarp_settings_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """JARVIS writes every field required by clio-core's Settings::from_json."""
    module = _package_module()
    package = object.__new__(module.GrayScott)
    package.config = {"deploy_mode": "default"}
    package.pipeline = SimpleNamespace(get_hostfile=lambda: None)
    package.env = {}
    package.shared_dir = "/scratch/shared"
    package.settings_json_path = "/scratch/shared/settings-files.json"
    package.adios2_xml_path = "/scratch/shared/adios2.xml"
    package.pkg_dir = "/packages/gray_scott"
    package.copy_template_file = lambda *_args: None
    _CapturedJsonFile.documents = []
    monkeypatch.setattr(
        module.Application,
        "_configure",
        lambda self, **kwargs: self.config.update(kwargs),
    )
    monkeypatch.setattr(module, "Mkdir", _CapturedMkdir)
    monkeypatch.setattr(module, "JsonFile", _CapturedJsonFile)

    package._configure(
        outdir="/scratch/run/gray-scott.bp",
        width=32,
        height=32,
        steps=20,
        out_every=10,
    )

    assert len(_CapturedJsonFile.documents) == 1
    settings = _CapturedJsonFile.documents[0]
    assert set(settings) == {
        "L",
        "Du",
        "Dv",
        "F",
        "k",
        "dt",
        "plotgap",
        "steps",
        "noise",
        "output",
        "checkpoint",
        "checkpoint_freq",
        "checkpoint_output",
        "adios_config",
        "adios_span",
        "adios_memory_selection",
        "mesh_type",
    }
    assert settings["output"] == "/scratch/run/gray-scott.bp"
    assert settings["checkpoint"] is False
    assert settings["checkpoint_output"] == ("/scratch/run/gray-scott.bp.checkpoint.bp")
    assert settings["adios_span"] is False
    assert settings["adios_memory_selection"] is False
    assert settings["mesh_type"] == "image"


def test_gray_scott_default_output_uses_package_shared_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An omitted output path resolves to durable package-shared storage."""
    module = _package_module()
    package = object.__new__(module.GrayScott)
    package.config = {"deploy_mode": "default", "outdir": ""}
    package.pipeline = SimpleNamespace(get_hostfile=lambda: None)
    package.env = {}
    package.shared_dir = "/scratch/pipeline/gray-scott-step"
    package.settings_json_path = "/scratch/pipeline/settings-files.json"
    package.adios2_xml_path = "/scratch/pipeline/adios2.xml"
    package.pkg_dir = "/packages/gray_scott"
    package.copy_template_file = lambda *_args: None
    _CapturedJsonFile.documents = []
    monkeypatch.setattr(
        module.Application,
        "_configure",
        lambda self, **kwargs: self.config.update(kwargs),
    )
    monkeypatch.setattr(module, "Mkdir", _CapturedMkdir)
    monkeypatch.setattr(module, "JsonFile", _CapturedJsonFile)

    package._configure(width=32, height=32, steps=20, out_every=10)

    expected = "/scratch/pipeline/gray-scott-step/gray-scott-output"
    assert package.config["outdir"] == expected
    assert package.config["checkpoint_output"] == f"{expected}.checkpoint.bp"
    assert _CapturedJsonFile.documents[0]["output"] == expected


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("outdir", "relative/output.bp"),
        ("outdir", "/scratch/../escape.bp"),
        ("checkpoint_output", "relative/restart.bp"),
    ],
)
def test_gray_scott_rejects_ambiguous_dataset_paths(
    monkeypatch: pytest.MonkeyPatch,
    field_name: str,
    value: str,
) -> None:
    """Run and cleanup must share one absolute normalized path identity."""
    module = _package_module()
    package = object.__new__(module.GrayScott)
    package.config = {"deploy_mode": "default"}
    package.pipeline = SimpleNamespace(get_hostfile=lambda: None)
    package.env = {}
    package.shared_dir = "/scratch/shared"
    monkeypatch.setattr(
        module.Application,
        "_configure",
        lambda self, **kwargs: self.config.update(kwargs),
    )

    with pytest.raises(ValueError, match=f"unsafe {field_name}"):
        package._configure(
            width=32,
            height=32,
            steps=20,
            out_every=10,
            **{field_name: value},
        )


def test_gray_scott_configuration_propagates_directory_setup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing durable output directory aborts configuration."""
    module = _package_module()
    package = object.__new__(module.GrayScott)
    package.config = {"deploy_mode": "default"}
    package.pipeline = SimpleNamespace(get_hostfile=lambda: None)
    package.env = {}
    package.shared_dir = "/scratch/shared"
    monkeypatch.setattr(
        module.Application,
        "_configure",
        lambda self, **kwargs: self.config.update(kwargs),
    )
    monkeypatch.setattr(_CapturedMkdir, "exit_codes", {"localhost": 13})
    monkeypatch.setattr(module, "Mkdir", _CapturedMkdir)

    with pytest.raises(RuntimeError, match="output setup.*localhost=13"):
        package._configure(
            outdir="/scratch/run/gray-scott.bp",
            width=32,
            height=32,
            steps=20,
            out_every=10,
        )


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        ({"width": 32, "height": 64}, "width and height to match"),
        ({"steps": 21, "out_every": 10}, "steps to be divisible"),
        (
            {"checkpoint": True, "checkpoint_freq": 0},
            "checkpoint_freq must be a positive integer",
        ),
    ],
)
def test_gray_scott_rejects_unsupported_direct_grid_contracts(
    monkeypatch: pytest.MonkeyPatch,
    settings: dict[str, int],
    message: str,
) -> None:
    """Invalid direct-app settings fail instead of being silently ignored."""
    module = _package_module()
    package = object.__new__(module.GrayScott)
    package.config = {
        "deploy_mode": "default",
        "width": 32,
        "height": 32,
        "steps": 20,
        "out_every": 10,
    }
    monkeypatch.setattr(
        module.Application,
        "_configure",
        lambda self, **kwargs: self.config.update(kwargs),
    )

    with pytest.raises(ValueError, match=message):
        package._configure(**settings)


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


def test_gray_scott_default_runtime_uses_explicit_quoted_executable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bare-metal execution can target an exact installed IOWarp binary."""
    module = _package_module()
    package = object.__new__(module.GrayScott)
    package.config = {
        "deploy_mode": "default",
        "nprocs": 1,
        "ppn": 1,
        "executable": "/opt/iowarp tools/gray-scott",
    }
    package.mod_env = {}
    package.pipeline = SimpleNamespace(get_hostfile=lambda: "/tmp/host file")
    package.settings_json_path = "/tmp/settings files.json"
    package.runtime_line_callback = lambda: None
    package.log = lambda *_args, **_kwargs: None
    _CapturedExec.calls = []
    monkeypatch.setattr(module, "Exec", _CapturedExec)

    package.start()

    assert len(_CapturedExec.calls) == 1
    command, _ = _CapturedExec.calls[0]
    assert command == "'/opt/iowarp tools/gray-scott' '/tmp/settings files.json'"


def test_gray_scott_default_runtime_rejects_empty_executable() -> None:
    """Invalid executable settings fail before constructing an MPI process."""
    module = _package_module()
    package = object.__new__(module.GrayScott)
    package.config = {
        "deploy_mode": "default",
        "nprocs": 1,
        "ppn": 1,
        "executable": "   ",
    }
    package.mod_env = {}
    package.settings_json_path = "/tmp/settings.json"
    package.runtime_line_callback = lambda: None

    with pytest.raises(ValueError, match="executable must be a non-empty string"):
        package.start()


def test_gray_scott_runtime_propagates_nonzero_process_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed direct IOWarp producer fails package start synchronously."""
    module = _package_module()
    package = object.__new__(module.GrayScott)
    package.config = {
        "deploy_mode": "default",
        "nprocs": 2,
        "ppn": 2,
        "executable": "gray-scott",
    }
    package.mod_env = {}
    package.pipeline = SimpleNamespace(get_hostfile=lambda: "/tmp/hostfile")
    package.settings_json_path = "/tmp/settings.json"
    package.runtime_line_callback = lambda: None
    package.log = lambda *_args, **_kwargs: None
    _CapturedExec.calls = []
    monkeypatch.setattr(_CapturedExec, "exit_codes", {"ares": 17})
    monkeypatch.setattr(module, "Exec", _CapturedExec)

    with pytest.raises(RuntimeError, match="IOWarp Gray-Scott.*ares=17"):
        package.start()


def test_gray_scott_runtime_surfaces_callback_failure_diagnostic() -> None:
    """A callback-triggered process termination retains its causal error."""
    module = _package_module()
    result = SimpleNamespace(
        exit_code={"localhost": -15},
        stderr={
            "localhost": (
                "Output line callback failed for stdout: "
                "progress store is unavailable\n"
            )
        },
    )

    with pytest.raises(RuntimeError) as raised:
        module.GrayScott._raise_for_exec_failure(
            result,
            operation="IOWarp Gray-Scott",
        )

    message = str(raised.value)
    assert "localhost=-15" in message
    assert "Output line callback failed for stdout" in message
    assert "progress store is unavailable" in message


def test_gray_scott_clean_removes_exact_output_and_checkpoint_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cleanup covers independent datasets without wildcard expansion."""
    module = _package_module()
    package = object.__new__(module.GrayScott)
    package.config = {
        "outdir": "/scratch/results/main output.bp",
        "checkpoint_output": "/scratch/checkpoints/restart.bp",
    }
    package.env = {"ADIOS2_ENGINE": "BP5"}
    package.pipeline = SimpleNamespace(get_hostfile=lambda: None)
    _CapturedExec.calls = []
    monkeypatch.setattr(module, "Exec", _CapturedExec)

    package.clean()

    assert len(_CapturedExec.calls) == 1
    command, exec_info = _CapturedExec.calls[0]
    assert command == (
        "rm -rf -- '/scratch/results/main output.bp' /scratch/checkpoints/restart.bp"
    )
    assert "*" not in command
    assert exec_info.env == {"ADIOS2_ENGINE": "BP5"}


def test_gray_scott_clean_deduplicates_paths_and_rejects_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cleanup never repeats a path or permits a filesystem-root target."""
    module = _package_module()
    package = object.__new__(module.GrayScott)
    package.config = {
        "outdir": "/scratch/results/gray-scott.bp",
        "checkpoint_output": "/scratch/results/gray-scott.bp",
    }
    package.env = {}
    package.pipeline = SimpleNamespace(get_hostfile=lambda: None)
    _CapturedExec.calls = []
    monkeypatch.setattr(module, "Exec", _CapturedExec)

    package.clean()

    assert _CapturedExec.calls[0][0] == ("rm -rf -- /scratch/results/gray-scott.bp")

    package.config["outdir"] = "/"
    with pytest.raises(ValueError, match="unsafe outdir cleanup path"):
        package.clean()
    assert len(_CapturedExec.calls) == 1
