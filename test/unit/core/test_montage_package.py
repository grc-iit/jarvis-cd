"""Runtime-contract tests for the builtin Montage package."""

from __future__ import annotations

import shlex
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest


def _load_package() -> ModuleType:
    """Load builtin Montage without repository registration."""
    path = (
        Path(__file__).resolve().parents[3]
        / "builtin"
        / "builtin"
        / "montage"
        / "pkg.py"
    )
    spec = spec_from_file_location("test_builtin_montage", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load the Montage package from {path}")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


montage_package = _load_package()


class _RuntimeCallback:
    """Capture the one terminal process finalization."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.finalizations: list[int] = []

    def __call__(self, _stream: str, line: str) -> None:
        self.lines.append(line)

    def finalize_process(self, return_code: int) -> None:
        self.finalizations.append(return_code)


class _CapturedExec:
    """Execute no external programs while materializing expected products."""

    commands: list[str] = []
    working_directories: list[str] = []
    failures: dict[str, int] = {}

    def __init__(self, command: str, exec_info: Any) -> None:
        self.command = command
        self.exec_info = exec_info
        self.exit_code = {"localhost": self.failures.get(command, 0)}
        self.commands.append(command)
        self.working_directories.append(str(exec_info.cwd))

    def run(self) -> _CapturedExec:
        """Materialize products corresponding to a successful Montage command."""
        return_code = self.exit_code["localhost"]
        if return_code == 0:
            tokens = shlex.split(self.command)

            def resolve(value: str) -> Path:
                path = Path(value)
                return path if path.is_absolute() else Path(self.exec_info.cwd) / path

            if tokens and tokens[0] == "mExec":
                output = resolve(tokens[tokens.index("-o") + 1])
                output.write_bytes(b"SIMPLE  " + bytes(4096))
            elif tokens and tokens[0] == "mExamine":
                stats = resolve(tokens[tokens.index(">") + 1])
                stats.write_text(
                    '[struct stat="OK", npixel=4096, nnull=32, '
                    "aveflux=1.25, rmsflux=0.50]\n",
                    encoding="utf-8",
                )
            elif tokens and tokens[0] == "mViewer":
                composite = resolve(tokens[tokens.index("-out") + 1])
                composite.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(2048))
        callback = getattr(self.exec_info, "line_callback", None)
        if callback is not None:
            callback("stdout", f"{self.command}\n")
            callback.finalize_process(return_code)
        return self


def _bundle(tmp_path: Path, band: str) -> SimpleNamespace:
    """Create one verified-bundle-shaped test object."""
    root = tmp_path / f"materialized-{band}"
    source = root / band / f"source-{band}.fits"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"SIMPLE  " + bytes(4096))
    header = root / "region.hdr"
    header.write_text("SIMPLE mosaic header\n", encoding="utf-8")
    return SimpleNamespace(
        band=band,
        bundle_sha256=band * 64,
        entrypoint=header,
        manifest=SimpleNamespace(
            entrypoint="region.hdr",
            files=(
                SimpleNamespace(path=f"{band}/source-{band}.fits", role="fits_source"),
                SimpleNamespace(path="region.hdr", role="mosaic_header"),
            ),
        ),
        root=root,
    )


def _stage_bundle(bundle: SimpleNamespace, destination: Path) -> Path:
    """Stage one bundle-shaped fixture into a mutable scratch directory."""
    destination.mkdir(parents=True, exist_ok=True)
    source = destination / bundle.band / f"source-{bundle.band}.fits"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"SIMPLE  " + bytes(4096))
    header = destination / "region.hdr"
    header.write_text("SIMPLE mosaic header\n", encoding="utf-8")
    return header


def _package(tmp_path: Path, **updates: object) -> Any:
    """Build a minimally configured Montage package."""
    package = object.__new__(montage_package.Montage)
    package.shared_dir = str(tmp_path / "shared")
    package.private_dir = str(tmp_path / "private")
    package.get_hostfile = lambda: None
    package.env = {"PATH": "/runtime/bin"}
    package.mod_env = dict(package.env)
    package.config = {
        "deploy_mode": "default",
        "region": "M31",
        "band": "j",
        "size": 0.2,
        "out": ".",
        "j_bundle": "/inputs/j.tar",
        "h_bundle": "/inputs/h.tar",
        "k_bundle": "/inputs/k.tar",
        "nprocs": 1,
        "ppn": 1,
    }
    package.config.update(updates)
    return package


def test_montage_contract_discloses_offline_three_band_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agents can discover a network-independent supplied-input profile."""
    package = object.__new__(montage_package.Montage)
    package.config = {"deploy_mode": "default"}
    package.mod_env = {"PATH": "/runtime/bin"}
    package.env = dict(package.mod_env)
    package._deployment_environment = lambda: package.mod_env
    monkeypatch.setattr(
        montage_package,
        "probe_program",
        lambda *_args, **_kwargs: SimpleNamespace(
            status=montage_package.RuntimeStatus("ready", "test")
        ),
    )

    menu = {item["name"]: item for item in package._configure_menu()}
    contract = package._deployment_contract().to_dict()

    for band in ("j", "h", "k"):
        assert menu[f"{band}_bundle"]["input_binding"]["kind"] == "local_file"
    profiles = {item["name"]: item for item in contract["execution_profiles"]}
    offline = profiles["offline_three_band"]
    assert offline["execution_kind"] == "batch"
    assert {item["parameter"] for item in offline["when"]} == {
        "j_bundle",
        "h_bundle",
        "k_bundle",
    }
    runtime = contract["runtime_requirements"][0]
    assert runtime["provider_resolutions"][0]["query"]["value"] == "montage@6.0"


def test_montage_rejects_partial_offline_and_container_configuration(
    tmp_path: Path,
) -> None:
    """Offline inputs are all-or-none and never trigger a container build."""
    partial = _package(tmp_path, h_bundle="", k_bundle="")
    with pytest.raises(ValueError, match="all three"):
        partial._validate_configuration()
    container = _package(tmp_path, deploy_mode="container")
    with pytest.raises(ValueError, match="native execution"):
        container._validate_configuration()


def test_legacy_script_uses_configured_archive_coordinates() -> None:
    """Legacy acquisition cannot silently fall back to fixed M17/J coordinates."""
    script = (
        Path(__file__).resolve().parents[3]
        / "builtin"
        / "builtin"
        / "montage"
        / "run_mosaic.sh"
    ).read_text(encoding="utf-8")

    assert 'REGION="${MONTAGE_REGION:-M17}"' in script
    assert 'BAND="${MONTAGE_BAND:-J}"' in script
    assert 'SIZE="${MONTAGE_SIZE:-0.2}"' in script
    assert 'mHdr "$REGION" "$SIZE"' in script
    assert 'mArchiveList 2mass "$BAND" "$REGION" "$SIZE" "$SIZE"' in script


def test_montage_offline_profile_runs_three_bands_and_finalizes_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The maintained profile stages inputs, validates outputs, and closes once."""
    package = _package(tmp_path)
    callback = _RuntimeCallback()
    package.runtime_line_callback = lambda: callback
    bundles = {
        f"/inputs/{band}.tar": _bundle(tmp_path, band) for band in ("j", "h", "k")
    }

    _CapturedExec.commands = []
    _CapturedExec.working_directories = []
    _CapturedExec.failures = {}
    monkeypatch.setattr(montage_package, "Exec", _CapturedExec)
    monkeypatch.setattr(
        montage_package,
        "extract_input_bundle",
        lambda path, _destination: bundles[path],
    )
    monkeypatch.setattr(montage_package, "stage_input_bundle", _stage_bundle)
    package.start()

    assert sum(command.startswith("mExec ") for command in _CapturedExec.commands) == 3
    assert (
        sum(command.startswith("mExamine ") for command in _CapturedExec.commands) == 3
    )
    assert (
        sum(command.startswith("mViewer ") for command in _CapturedExec.commands) == 1
    )
    assert callback.finalizations == [0]
    mexec_invocations = [
        (command, Path(cwd))
        for command, cwd in zip(
            _CapturedExec.commands,
            _CapturedExec.working_directories,
            strict=True,
        )
        if command.startswith("mExec ")
    ]
    assert {
        shlex.split(command)[shlex.split(command).index("-r") + 1]
        for command, _ in mexec_invocations
    } == {"staged/j", "staged/h", "staged/k"}
    assert all(
        "-f staged/region.hdr -o mosaic.fits" in command
        for command, _ in mexec_invocations
    )
    assert all(cwd != Path(package.shared_dir) for _, cwd in mexec_invocations)
    assert all(len(str(cwd)) < 128 for _, cwd in mexec_invocations)
    assert all(not cwd.exists() for _, cwd in mexec_invocations)
    output = Path(package.shared_dir)
    assert (output / "montage-j.fits").is_file()
    assert (output / "montage-h.fits").is_file()
    assert (output / "montage-k.fits").is_file()
    assert (output / "montage-jhk.png").is_file()
    assert (output / "montage-result.json").is_file()


def test_montage_propagates_the_first_command_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failed Montage command cannot become scheduler success."""
    package = _package(tmp_path)
    package.runtime_line_callback = _RuntimeCallback
    bundles = {
        f"/inputs/{band}.tar": _bundle(tmp_path, band) for band in ("j", "h", "k")
    }
    monkeypatch.setattr(montage_package, "Exec", _CapturedExec)
    monkeypatch.setattr(
        montage_package,
        "extract_input_bundle",
        lambda path, _destination: bundles[path],
    )
    monkeypatch.setattr(montage_package, "stage_input_bundle", _stage_bundle)
    _CapturedExec.commands = []
    _CapturedExec.working_directories = []
    _CapturedExec.failures = {}
    failed_command = (
        "mExec -r staged/j -f staged/region.hdr -o mosaic.fits 2MASS J workspace"
    )
    _CapturedExec.failures[failed_command] = 7

    with pytest.raises(RuntimeError, match="Montage J mosaic failed"):
        package.start()
    assert _CapturedExec.working_directories
    assert all(
        not Path(directory).exists() for directory in _CapturedExec.working_directories
    )


def test_montage_rejects_different_band_headers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A three-band run cannot silently combine different mosaic regions."""
    package = _package(tmp_path)
    Path(package.shared_dir).mkdir(parents=True)
    bundles = {
        f"/inputs/{band}.tar": _bundle(tmp_path, band) for band in ("j", "h", "k")
    }
    bundles["/inputs/h.tar"].entrypoint.write_text(
        "different mosaic header\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        montage_package,
        "extract_input_bundle",
        lambda path, _destination: bundles[path],
    )

    with pytest.raises(ValueError, match="share one mosaic header"):
        package._prepare_offline_inputs()
