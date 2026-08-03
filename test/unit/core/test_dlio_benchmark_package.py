"""Runtime-contract tests for the builtin DLIO Benchmark package."""

from __future__ import annotations

import shlex
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest


def _load_package() -> ModuleType:
    """Load builtin DLIO without relying on repository registration."""
    path = (
        Path(__file__).resolve().parents[3]
        / "builtin"
        / "builtin"
        / "dlio_benchmark"
        / "pkg.py"
    )
    spec = spec_from_file_location("test_builtin_dlio_benchmark", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load the DLIO package from {path}")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dlio_package = _load_package()


class _CapturedExec:
    """Capture exact package commands and configurable process results."""

    commands: list[str] = []
    exec_infos: list[Any] = []
    failures: dict[str, dict[str, object]] = {}

    def __init__(self, command: str, exec_info: Any) -> None:
        self.command = command
        self.exec_info = exec_info
        self.exit_code = self.failures.get(command, {"localhost": 0})
        self.commands.append(command)
        self.exec_infos.append(exec_info)

    def run(self) -> _CapturedExec:
        """Return the captured process result."""
        callback = getattr(self.exec_info, "line_callback", None)
        if callback is not None:
            callback("stdout", f"running {self.command}\n")
            return_code = next(
                (code for code in self.exit_code.values() if code != 0),
                0,
            )
            callback.finalize_process(return_code)
        return self


@pytest.fixture(autouse=True)
def _reset_exec_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep command-level package tests independent."""
    _CapturedExec.commands = []
    _CapturedExec.exec_infos = []
    _CapturedExec.failures = {}
    monkeypatch.setattr(dlio_package, "Exec", _CapturedExec)


def _package(tmp_path: Path, **updates: object) -> Any:
    """Build a minimally configured package without invoking JARVIS discovery."""
    package = object.__new__(dlio_package.DlioBenchmark)
    package.shared_dir = str(tmp_path / "shared")
    package.get_hostfile = lambda: None
    package.env = {"PATH": "/runtime/bin"}
    package.mod_env = dict(package.env)
    package.config = {
        "workload": "resnet50_v100",
        "generate_data": False,
        "evaluation": False,
        "checkpoint_supported": True,
        "checkpoint": True,
        "data_path": "dataset",
        "output_path": "output",
        "checkpoint_path": "checkpoints",
        "num_files_train": 32,
        "num_samples_per_file": 2,
        "record_length_bytes": 114660,
        "record_length_bytes_resize": 114660,
        "batch_size": 8,
        "read_threads": 4,
        "epochs": 3,
        "train_computation_time": 0.05,
        "checkpoint_size_bytes": 104857600,
        "checkpoint_fsync": True,
        "checkpoint_after_epoch": 2,
        "epochs_between_checkpoints": 4,
        "nprocs": 4,
        "ppn": 4,
        "timeout_seconds": 3600,
        "tracing": False,
        "cache_policy": "none",
        "deploy_mode": "default",
    }
    package.config.update(updates)
    package.runtime_line_callback = lambda: None
    return package


def test_agent_contract_has_safe_cache_and_runtime_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Describe a bounded runtime without silently claiming cold-cache IO."""
    package = object.__new__(dlio_package.DlioBenchmark)
    package.config = {"generate_data": False}
    package.mod_env = {"PATH": "/runtime/bin"}
    package.env = dict(package.mod_env)
    package._deployment_environment = lambda: package.mod_env
    monkeypatch.setattr(
        dlio_package,
        "probe_program",
        lambda *args, **kwargs: SimpleNamespace(
            status=dlio_package.RuntimeStatus("ready", "runtime_probe_succeeded")
        ),
    )

    menu = {item["name"]: item for item in package._configure_menu()}
    assert menu["cache_policy"]["default"] == "none"
    assert menu["cache_policy"]["choices"] == ["none", "sync", "drop_caches"]
    assert menu["output_path"]["default"] == "output"

    contract = package._deployment_contract().to_dict()
    assert contract["package"] == "builtin.dlio_benchmark"
    assert contract["execution_profiles"][0]["readiness"] == {
        "mechanism": "process_exit",
        "condition": "successful_exit",
    }
    runtime = contract["runtime_requirements"][0]
    assert runtime["id"] == "dlio"
    assert runtime["status"]["usable"] is True
    assert runtime["provider_resolutions"] == [
        {
            "provider": "path",
            "query": {"kind": "program", "value": "dlio_benchmark"},
        }
    ]


def test_command_quotes_paths_and_exposes_typed_workload_controls(
    tmp_path: Path,
) -> None:
    """Build one native DLIO invocation without a shell-injection surface."""
    package = _package(tmp_path)
    command = package._command(generate_only=False, output_path=tmp_path / "result dir")

    assert shlex.quote("workload=resnet50_v100") in command
    assert (
        shlex.quote(f"++workload.dataset.data_folder={tmp_path / 'shared' / 'dataset'}")
        in command
    )
    assert shlex.quote(f"++workload.output.folder={tmp_path / 'result dir'}") in command
    assert "++workload.reader.read_threads=4" in command
    assert "++workload.dataset.num_samples_per_file=2" in command
    assert "++workload.dataset.record_length_bytes=114660" in command
    assert "++workload.dataset.record_length_bytes_resize=114660" in command
    assert "++workload.workflow.evaluation=false" in command
    assert "++workload.dataset.num_files_eval=0" in command
    assert "++workload.workflow.train=true" in command
    assert "++workload.train.computation_time=0.05" in command
    assert "++workload.workflow.checkpoint=true" in command
    assert "++workload.model.model_size_bytes=104857600" in command
    assert "++workload.checkpoint.fsync=true" in command
    assert "++workload.checkpoint.checkpoint_after_epoch=2" in command
    assert "++workload.checkpoint.epochs_between_checkpoints=4" in command


@pytest.mark.parametrize("policy, expected", [("none", []), ("sync", ["sync"])])
def test_non_privileged_cache_policies_never_call_sudo(
    tmp_path: Path,
    policy: str,
    expected: list[str],
) -> None:
    """The safe policies are explicit and cannot perform privileged eviction."""
    package = _package(tmp_path, cache_policy=policy)

    package._apply_cache_policy()

    assert _CapturedExec.commands == expected
    assert all("sudo" not in command for command in _CapturedExec.commands)


def test_privileged_cache_eviction_is_explicit_and_noninteractive(
    tmp_path: Path,
) -> None:
    """Cold-cache requests fail rather than hanging on an interactive password."""
    package = _package(tmp_path, cache_policy="drop_caches")

    package._apply_cache_policy()

    assert _CapturedExec.commands == [
        "sync && sudo -n sh -c 'echo 3 > /proc/sys/vm/drop_caches'"
    ]


def test_start_propagates_native_dlio_failure(tmp_path: Path) -> None:
    """A failed rank makes the package fail instead of reporting completion."""
    package = _package(tmp_path)
    command = package._command(
        generate_only=False,
        output_path=package._output_path() / "training",
    )
    _CapturedExec.failures[command] = {"node-a": 9}

    with pytest.raises(RuntimeError, match="DLIO workload.*node-a=9"):
        package.start()

    assert _CapturedExec.exec_infos[-1].timeout == 3600


class _StrictRuntimeCallback:
    """Reject output after package-level semantic finalization."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.finalized: list[int] = []
        self.closed = False

    def __call__(self, stream_name: str, line: str) -> None:
        """Record output only while the package callback remains open."""
        if self.closed:
            raise RuntimeError("runtime callback already finalized")
        self.lines.append(f"{stream_name}:{line}")

    def finalize_process(self, return_code: int) -> None:
        """Close package semantics after the terminal process."""
        self.finalized.append(return_code)
        self.closed = True

    def reconcile_process_exit(self, return_code: int) -> None:
        """Expose the process callback protocol used by JARVIS."""


def test_generate_and_train_share_one_callback_until_terminal_phase(
    tmp_path: Path,
) -> None:
    """Data generation must not close semantics before training starts."""
    package = _package(tmp_path, generate_data=True)
    callback = _StrictRuntimeCallback()
    package.runtime_line_callback = lambda: callback

    package.start()

    assert len(callback.lines) == 2
    assert callback.finalized == [0]
    assert callback.closed is True
    assert _CapturedExec.exec_infos[0].line_callback is not callback
    assert _CapturedExec.exec_infos[1].line_callback is not callback


@pytest.mark.parametrize(
    "field, value",
    [
        ("workload", "bad workload"),
        ("nprocs", 0),
        ("ppn", True),
        ("read_threads", -1),
        ("cache_policy", "pretend-cold"),
        ("timeout_seconds", 0),
    ],
)
def test_configuration_rejects_ambiguous_or_unsafe_values(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    """Invalid scientific and execution controls fail before submission."""
    package = _package(tmp_path, **{field: value})

    with pytest.raises((TypeError, ValueError)):
        package._validate_configuration()
