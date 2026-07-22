"""Focused tests for generic package deployment/readiness metadata."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from jarvis_cd.core.pkg import Pkg
from jarvis_cd.deployment import (
    ConfigurationCondition,
    ConfigurationInputBinding,
    ExecutionProfile,
    PackageDeploymentContract,
    ProgramProbeResult,
    ProviderResolution,
    ReadinessContract,
    RuntimeRequirement,
    RuntimeStatus,
)

_BUILTIN_REPOSITORY_ROOT = Path(__file__).resolve().parents[3] / "builtin"
sys.path.insert(0, str(_BUILTIN_REPOSITORY_ROOT))

from builtin.lammps import pkg as lammps_module  # noqa: E402
from builtin.paraview import pkg as paraview_module  # noqa: E402
from jarvis_cd.core.config import Jarvis  # noqa: E402


def _ready_probe() -> ProgramProbeResult:
    """Return a deterministic usable program probe."""
    return ProgramProbeResult(RuntimeStatus("ready", "runtime_probe_succeeded"))


def test_legacy_package_has_no_inferred_deployment_contract() -> None:
    """Class names and source locations cannot fabricate deployment semantics."""
    package = object.__new__(Pkg)

    assert package.describe_deployment() is None


def test_common_implementation_controls_are_not_agent_visible() -> None:
    """Generic agents see semantic package inputs, not JARVIS admin controls."""
    package = object.__new__(Pkg)

    parameters = package.configure_menu()

    assert parameters
    assert all(parameter["agent_visible"] is False for parameter in parameters)
    assert {parameter["name"] for parameter in parameters} >= {
        "install_method",
        "install_query",
        "install",
        "hostfile",
        "timeout",
    }


def test_standalone_descriptor_preserves_canonical_package_identity(
    tmp_path: Path,
) -> None:
    """Generic consumers can validate deployment identity against their lookup."""
    previous = Jarvis._instance
    Jarvis._instance = None
    try:
        jarvis = Jarvis(jarvis_root=str(tmp_path / ".ppi-jarvis"))
        jarvis.initialize(
            config_dir=str(tmp_path / "config"),
            private_dir=str(tmp_path / "private"),
            shared_dir=str(tmp_path / "shared"),
        )

        package = Pkg.load_standalone("builtin.lammps")
        document = package.describe_deployment()

        assert document is not None
        assert document["package"] == "builtin.lammps"
    finally:
        Jarvis._instance = previous


def test_schema_rejects_path_provider_queries_and_dangling_runtimes() -> None:
    """Provider selectors stay path-free and profile references stay exact."""
    with pytest.raises(ValueError, match="absolute path"):
        ProviderResolution("spack", "spec", "/opt/software/paraview")

    with pytest.raises(ValueError, match="unknown runtime requirements"):
        PackageDeploymentContract(
            package="site.demo",
            execution_profiles=(
                ExecutionProfile(
                    name="run",
                    execution_kind="batch",
                    when=(ConfigurationCondition("mode", "equals", "run"),),
                    runtime_requirements=("missing",),
                    readiness=ReadinessContract(
                        "process_exit",
                        "successful_exit",
                    ),
                ),
            ),
            runtime_requirements=(
                RuntimeRequirement(
                    requirement_id="present",
                    description="One usable test runtime",
                    required_capabilities=("compute",),
                    available_capabilities=("compute",),
                    status=RuntimeStatus("ready", "runtime_probe_succeeded"),
                ),
            ),
        )


def test_configuration_input_binding_is_closed_and_machine_readable() -> None:
    """Clients receive explicit file semantics instead of guessing from prose."""
    binding = ConfigurationInputBinding(
        kind="local_file",
        structure="regular_file",
    )

    assert binding.to_dict() == {
        "schema_version": "jarvis.configuration-input-binding.v1",
        "kind": "local_file",
        "structure": "regular_file",
    }
    with pytest.raises(
        ValueError,
        match="unsupported configuration input binding schema",
    ):
        ConfigurationInputBinding(
            kind="local_file",
            structure="regular_file",
            schema_version="jarvis.configuration-input-binding.v2",
        )


def test_lammps_contract_defaults_to_generated_batch_and_spack_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A null script is a real bounded workload rather than an empty launch."""
    monkeypatch.setattr(
        lammps_module,
        "probe_program",
        lambda *_args, **_kwargs: _ready_probe(),
    )
    package = object.__new__(lammps_module.Lammps)
    package.config = {}
    package.env = {}
    package.mod_env = {}

    menu = {item["name"]: item for item in package._configure_menu()}
    document = package.describe_deployment()

    assert menu["script"]["default"] == ""
    assert menu["script"]["input_binding"] == {
        "schema_version": "jarvis.configuration-input-binding.v1",
        "kind": "local_file",
        "structure": "regular_file",
    }
    assert "`lattice fcc <density>`" in menu["script"]["msg"]
    assert "`region ... units lattice`" in menu["script"]["msg"]
    assert (
        "Every created atom type must receive a positive mass" in menu["script"]["msg"]
    )
    assert "`mass 1 1.0`" in menu["script"]["msg"]
    assert menu["io_dump_interval"]["default"] == 100
    assert "lmp_bin" not in menu
    assert document is not None
    assert document["schema_version"] == "jarvis.package-deployment.v1"
    assert document["package"] == "builtin.lammps"
    assert {
        (profile["name"], profile["execution_kind"])
        for profile in document["execution_profiles"]
    } == {("generated_workload", "batch"), ("input_script", "batch")}
    profiles = {profile["name"]: profile for profile in document["execution_profiles"]}
    assert profiles["generated_workload"]["description"] == (
        "Built-in bounded Lennard-Jones smoke workload with a package-generated "
        "input script and trajectory output."
    )
    assert "`lattice fcc <density>`" in profiles["input_script"]["description"]
    assert (
        "Assign every created atom type a positive mass"
        in profiles["input_script"]["description"]
    )
    assert "`mass 1 1.0`" in profiles["input_script"]["description"]
    runtime = document["runtime_requirements"][0]
    assert runtime["status"] == {
        "state": "ready",
        "usable": True,
        "reason_code": "runtime_probe_succeeded",
    }
    assert runtime["provider_resolutions"] == [
        {
            "provider": "spack",
            "query": {"kind": "spec", "value": "lammps"},
        }
    ]


def test_lammps_concrete_launcher_override_is_not_agent_configuration() -> None:
    """Spack activation supplies PATH; callers cannot inject an executable path."""
    package = object.__new__(lammps_module.Lammps)
    package.config = {"lmp_bin": "/site/software/lmp"}

    with pytest.raises(ValueError, match="execution environment PATH"):
        package._validate_legacy_runtime_configuration()


def test_paraview_contract_is_mode_complete_and_path_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All ParaView modes expose generic readiness without selected paths."""

    def resolve(
        _package: Any,
        mode: str,
        _environment: dict[str, str],
    ) -> Any:
        capabilities = frozenset({"--mesa"}) if mode == "service" else frozenset()
        return paraview_module._ParaViewRuntime(
            executable=f"/private/runtime/{mode}",
            capabilities=capabilities,
        )

    monkeypatch.setattr(paraview_module.Paraview, "_resolve_runtime", resolve)
    package = object.__new__(paraview_module.Paraview)
    package.config = {}
    package.env = {}
    package.mod_env = {}

    document = package.describe_deployment()

    assert document is not None
    profiles = {profile["name"]: profile for profile in document["execution_profiles"]}
    assert profiles["batch_script"]["execution_kind"] == "batch"
    assert profiles["client_server"]["readiness"]["mechanism"] == "progress_event"
    assert profiles["live_dataset_service"]["readiness"] == {
        "mechanism": "service_runtime",
        "condition": "health_check_succeeded",
        "capability": "interactive_visualization",
    }
    encoded = json.dumps(document, sort_keys=True)
    assert "/private/runtime" not in encoded
    assert "executable" not in encoded
    assert "source_path" not in encoded


class _MissingWhich:
    """Represent a ParaView launcher absent from PATH."""

    def __init__(self, _executable: str, _exec_info: Any) -> None:
        self.exit_code = {"localhost": 1}
        self.stdout = {"localhost": ""}
        self.stderr = {"localhost": ""}

    def run(self) -> "_MissingWhich":
        """Return the completed deterministic lookup."""
        return self


class _RootProbeExec:
    """Probe discovered user installations with version-specific capabilities."""

    def __init__(self, command: str, _exec_info: Any) -> None:
        self.exit_code = {"localhost": 0}
        self.stdout = {"localhost": ""}
        self.stderr = {"localhost": ""}
        if "5.12" in command or "explicit" in command:
            self.stdout["localhost"] = "--mesa\n"

    def run(self) -> "_RootProbeExec":
        """Return the completed deterministic capability probe."""
        return self


def _paraview_probe_package() -> Any:
    """Build the minimum real package context required by runtime discovery."""
    package = object.__new__(paraview_module.Paraview)
    package.config = {"cwd": "", "force_offscreen_rendering": False}
    package.pipeline = SimpleNamespace(get_hostfile=lambda: object())
    return package


def test_paraview_discovers_explicit_home_without_path_configuration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """PARAVIEW_HOME is a package-owned fallback, not an agent-supplied binary."""
    executable = tmp_path / "explicit" / "bin" / "pvpython"
    executable.parent.mkdir(parents=True)
    executable.write_text("test launcher", encoding="utf-8")
    monkeypatch.setattr(paraview_module, "Which", _MissingWhich)
    monkeypatch.setattr(paraview_module, "Exec", _RootProbeExec)
    package = _paraview_probe_package()

    runtime = package._resolve_runtime(
        "service",
        {
            "HOME": str(tmp_path / "home"),
            "PARAVIEW_HOME": str(executable.parents[1]),
            "PATH": "",
        },
    )

    assert runtime.executable == str(executable.resolve())
    assert runtime.arguments(force_offscreen=True) == ("--mesa",)


def test_paraview_selects_capable_versioned_user_install_deterministically(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A newer but incapable runtime cannot displace a usable service runtime."""
    home = tmp_path / "home"
    older = home / "opt" / "ParaView-5.12.1" / "bin" / "pvpython"
    newer = home / "opt" / "ParaView-5.13.0" / "bin" / "pvpython"
    for executable in (older, newer):
        executable.parent.mkdir(parents=True)
        executable.write_text("test launcher", encoding="utf-8")
    monkeypatch.setattr(paraview_module, "Which", _MissingWhich)
    monkeypatch.setattr(paraview_module, "Exec", _RootProbeExec)
    package = _paraview_probe_package()

    runtime = package._resolve_runtime(
        "service",
        {"HOME": str(home), "PATH": ""},
    )

    assert runtime.executable == str(older.resolve())
    assert runtime.capabilities == frozenset({"--mesa"})
