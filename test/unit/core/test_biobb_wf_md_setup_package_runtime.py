"""Runtime-contract tests for the builtin BioBB MD-setup package."""

from __future__ import annotations

import shlex
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from jarvis_cd.deployment import ProgramProbeResult, RuntimeStatus
from jarvis_cd.shell import LocalExecInfo
from jarvis_cd.util.hostfile import Hostfile


def _load_package() -> ModuleType:
    """Load the builtin package without changing repository imports."""

    path = (
        Path(__file__).resolve().parents[3]
        / "builtin"
        / "builtin"
        / "biobb_wf_md_setup"
        / "pkg.py"
    )
    spec = spec_from_file_location("test_biobb_md_setup_runtime_package", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load BioBB package from {path}")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


biobb_package = _load_package()


class _CapturedExec:
    """Capture launches and expose a configurable process result."""

    commands: list[str] = []
    infos: list[Any] = []
    return_code = 0

    def __init__(self, command: str, exec_info: Any) -> None:
        self.command = command
        self.exec_info = exec_info
        self.exit_code = {"localhost": self.return_code}
        self.commands.append(command)
        self.infos.append(exec_info)

    def run(self) -> _CapturedExec:
        """Return the captured result."""

        return self


class _CapturedMkdir:
    """Create the requested test directory."""

    def __init__(self, path: str, exec_info: Any) -> None:
        self.path = path
        self.exec_info = exec_info
        self.exit_code = {"localhost": 0}

    def run(self) -> _CapturedMkdir:
        """Materialize the directory and report success."""

        Path(self.path).mkdir(parents=True, exist_ok=True)
        return self


class _CapturedRm:
    """Capture exact cleanup authority without deleting files."""

    calls: list[tuple[str, bool]] = []

    def __init__(self, path: str, exec_info: Any, *, recursive: bool = False) -> None:
        self.path = path
        self.exec_info = exec_info
        self.recursive = recursive
        self.exit_code = {"localhost": 0}

    def run(self) -> _CapturedRm:
        """Record the requested cleanup."""

        self.calls.append((self.path, self.recursive))
        return self


@pytest.fixture(autouse=True)
def _capture_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep package tests free of process and remote-host side effects."""

    _CapturedExec.commands = []
    _CapturedExec.infos = []
    _CapturedExec.return_code = 0
    _CapturedRm.calls = []
    monkeypatch.setattr(biobb_package, "Exec", _CapturedExec)
    monkeypatch.setattr(biobb_package, "Mkdir", _CapturedMkdir)
    monkeypatch.setattr(biobb_package, "Rm", _CapturedRm)


def _base_config(**overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "deploy_mode": "default",
        "pdb_file": "",
        "out": "run",
        "force_field": "amber99sb-ildn",
        "water_type": "tip3p",
        "box_type": "cubic",
        "distance_to_molecule": 1.0,
        "ignore_input_hydrogens": True,
        "merge_chains": False,
        "nprocs": 1,
        "ppn": 1,
    }
    config.update(overrides)
    return config


def _package(tmp_path: Path, config: dict[str, Any]) -> Any:
    package = object.__new__(biobb_package.BiobbWfMdSetup)
    package.config = config
    package.shared_dir = tmp_path / "shared"
    package.private_dir = tmp_path / "private"
    package.env = {}
    package.mod_env = {"PATH": "/runtime/bin"}
    package.python_bin = "python3"
    package.pipeline = SimpleNamespace(get_hostfile=lambda: Hostfile(find_ips=False))
    package.runtime_line_callback = lambda: None
    return package


def test_agent_contract_exposes_real_scientific_inputs_and_hides_benchmark_knobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agents see the scientific cell instead of container benchmark controls."""

    monkeypatch.setattr(
        biobb_package,
        "probe_program",
        lambda *_args, **_kwargs: ProgramProbeResult(
            RuntimeStatus("ready", "runtime_probe_succeeded")
        ),
    )
    package = object.__new__(biobb_package.BiobbWfMdSetup)
    package.config = _base_config()
    package.env = {}
    package.mod_env = {}

    menu = {item["name"]: item for item in package._configure_menu()}
    contract = package._deployment_contract().to_dict()

    assert menu["pdb_file"]["default"] == ""
    assert menu["pdb_file"]["input_binding"] == {
        "schema_version": "jarvis.configuration-input-binding.v1",
        "kind": "local_file",
        "structure": "regular_file",
    }
    assert menu["out"]["default"] == "run"
    assert menu["force_field"]["default"] == "amber99sb-ildn"
    assert menu["box_type"]["default"] == "cubic"
    assert all(
        menu[name]["agent_visible"] is False
        for name in {
            "replicates",
            "parallel_scratch_root",
            "parallel_reps",
            "omp_threads",
            "md_steps",
            "md_nstxout",
            "md_extend_script",
            "base_image",
        }
    )
    assert [profile["name"] for profile in contract["execution_profiles"]] == [
        "native_md_setup"
    ]
    assert {item["id"] for item in contract["runtime_requirements"]} == {
        "biobb_python",
        "gromacs",
    }


def test_native_run_copies_input_and_launches_package_owned_driver(
    tmp_path: Path,
) -> None:
    """The caller PDB is immutable and all generated files share one owned root."""

    source = tmp_path / "caller" / "protein with space.pdb"
    source.parent.mkdir()
    source.write_text("ATOM      1  N   ALA A   1\nEND\n", encoding="utf-8")
    package = _package(
        tmp_path,
        _base_config(
            pdb_file=str(source),
            force_field="charmm27",
            water_type="spce",
            box_type="dodecahedron",
            distance_to_molecule=1.25,
        ),
    )

    package.start()

    staged = tmp_path / "shared" / "run" / "input.pdb"
    assert staged.read_bytes() == source.read_bytes()
    command = _CapturedExec.commands[-1]
    assert shlex.quote(str(staged.resolve())) in command
    assert "run_md_setup.py" in command
    assert "--force-field charmm27" in command
    assert "--water-type spce" in command
    assert "--box-type dodecahedron" in command
    assert "--distance-to-molecule 1.25" in command
    assert _CapturedExec.infos[-1].cwd == str(staged.parent.resolve())
    assert isinstance(_CapturedExec.infos[-1], LocalExecInfo)


def test_native_run_rejects_missing_unsafe_or_colliding_input(tmp_path: Path) -> None:
    """Malformed input and staged-name collisions fail before process launch."""

    package = _package(tmp_path, _base_config())
    with pytest.raises(ValueError, match="requires pdb_file"):
        package.start()

    empty = tmp_path / "empty.pdb"
    empty.touch()
    package.config = _base_config(pdb_file=str(empty))
    with pytest.raises(ValueError, match="bounded regular PDB"):
        package.start()

    source = tmp_path / "protein.pdb"
    source.write_text("ATOM\n", encoding="utf-8")
    collision = tmp_path / "shared" / "run" / "input.pdb"
    collision.parent.mkdir(parents=True, exist_ok=True)
    collision.write_text("unrelated\n", encoding="utf-8")
    package.config = _base_config(pdb_file=str(source))
    with pytest.raises(ValueError, match="staged input already exists"):
        package.start()

    assert collision.read_text(encoding="utf-8") == "unrelated\n"
    assert _CapturedExec.commands == []


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"force_field": "unknown"}, "force_field"),
        ({"water_type": "unknown"}, "water_type"),
        ({"box_type": "sphere"}, "box_type"),
        ({"distance_to_molecule": 0.0}, "distance_to_molecule"),
        ({"distance_to_molecule": 10.1}, "distance_to_molecule"),
    ],
)
def test_native_scientific_parameters_are_closed_and_bounded(
    tmp_path: Path, overrides: dict[str, Any], message: str
) -> None:
    """The package rejects unsupported scientific configurations locally."""

    source = tmp_path / "protein.pdb"
    source.write_text("ATOM\n", encoding="utf-8")
    package = _package(tmp_path, _base_config(pdb_file=str(source), **overrides))

    with pytest.raises(ValueError, match=message):
        package.start()

    assert _CapturedExec.commands == []


def test_native_process_failure_fails_the_package_lifecycle(tmp_path: Path) -> None:
    """A failed BioBB stage cannot be represented as a successful pipeline."""

    source = tmp_path / "protein.pdb"
    source.write_text("ATOM\n", encoding="utf-8")
    package = _package(tmp_path, _base_config(pdb_file=str(source)))
    _CapturedExec.return_code = 7

    with pytest.raises(RuntimeError, match="BioBB MD setup failed"):
        package.start()


def test_clean_uses_one_exact_recursive_output_without_wildcard(tmp_path: Path) -> None:
    """Cleanup cannot erase prefix-matching sibling directories."""

    package = _package(tmp_path, _base_config(out="results"))

    package.clean()

    assert _CapturedRm.calls == [
        (str((tmp_path / "shared" / "results").resolve()), True)
    ]
    assert "*" not in _CapturedRm.calls[0][0]
