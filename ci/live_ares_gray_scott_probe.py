#!/usr/bin/env python3
"""Run a released-wheel Gray-Scott acceptance probe on Ares.

The outer process installs the supplied wheel into an isolated virtual
environment below a caller-supplied shared root.  The installed child then uses
the public JARVIS Python API to submit ``builtin.gray_scott`` against the exact
``clio-core/external/iowarp-gray-scott`` executable through the explicit Slurm
scheduler provider. Every valid invocation writes a machine-readable report,
including failure paths.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from uuid import uuid4


REPORT_SCHEMA = "jarvis.live-validation.gray-scott.v1"
PACKAGE_ID = "gray_scott_bp5"
OUTPUT_LOGICAL_NAME = "gray-scott-timesteps"
CHECKPOINT_LOGICAL_NAME = "gray-scott-restart-checkpoint"
EXPECTED_STEPS = 20
EXPECTED_PLOTGAP = 10
_SAFE_SPEC = re.compile(r"^[A-Za-z0-9@%+~_./:=^,-]{1,512}$")
_SAFE_SCHEDULER_TOKEN = re.compile(r"^[A-Za-z0-9_.:@/+,-]{1,255}$")
_SAFE_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.!+_-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def utc_now() -> str:
    """Return the current time as a stable UTC ISO-8601 timestamp."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    """Return the SHA-256 digest of one regular file."""
    status = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(status.st_mode):
        raise ValueError(f"expected a regular file: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_json(path: Path, document: Mapping[str, Any]) -> None:
    """Durably replace ``path`` with one private JSON report."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    temporary_path = Path(temporary_name)
    descriptor_owned = True
    try:
        try:
            stream = os.fdopen(descriptor, "w", encoding="utf-8", newline="\n")
        except BaseException:
            os.close(descriptor)
            descriptor_owned = False
            raise
        descriptor_owned = False
        with stream:
            json.dump(
                document,
                stream,
                allow_nan=False,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary_path.chmod(0o600)
        os.replace(temporary_path, path)
        if os.name != "nt":
            directory = os.open(
                path.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if descriptor_owned:
            os.close(descriptor)
        temporary_path.unlink(missing_ok=True)


def add_assertion(
    assertions: list[dict[str, Any]],
    name: str,
    *,
    passed: bool,
    expected: Any,
    actual: Any,
    detail: str | None = None,
) -> None:
    """Append one explicit, JSON-safe acceptance assertion."""
    result: dict[str, Any] = {
        "name": name,
        "passed": bool(passed),
        "expected": expected,
        "actual": actual,
    }
    if detail is not None:
        result["detail"] = detail
    assertions.append(result)


def _assert_expected_release_artifact(
    args: argparse.Namespace,
    report: dict[str, Any],
    *,
    installed_version: str | None = None,
) -> None:
    """Bind the acceptance report to the requested wheel and distribution."""
    assertions = report.get("assertions")
    if not isinstance(assertions, list):
        raise RuntimeError("live report assertions field is not a list")

    actual_digest = sha256_file(args.wheel)
    add_assertion(
        assertions,
        "release_artifact.wheel_sha256",
        passed=actual_digest == args.expected_wheel_sha256,
        expected=args.expected_wheel_sha256,
        actual=actual_digest,
        detail="exact bytes installed by the acceptance probe",
    )
    if installed_version is not None:
        add_assertion(
            assertions,
            "release_artifact.installed_version",
            passed=installed_version == args.expected_version,
            expected=args.expected_version,
            actual=installed_version,
            detail="importlib metadata from the isolated installed environment",
        )

    failed = [item["name"] for item in assertions if item.get("passed") is False]
    if failed:
        raise AssertionError(
            "release artifact provenance assertions failed: " + ", ".join(failed)
        )


def evaluate_semantics(
    *,
    record: Mapping[str, Any],
    progress: Mapping[str, Any],
    artifacts: Mapping[str, Any],
    output_path: Path,
    checkpoint_path: Path,
    bpls_output: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Evaluate the exact durable Gray-Scott acceptance contract."""
    assertions: list[dict[str, Any]] = []
    _assert_equal(assertions, "execution.state", record.get("state"), "completed")
    _assert_equal(assertions, "execution.terminal", record.get("terminal"), True)
    _assert_equal(assertions, "execution.return_code", record.get("return_code"), 0)
    _assert_equal(
        assertions,
        "execution.scheduler_provider",
        record.get("scheduler_provider"),
        "slurm",
    )
    native_id = record.get("scheduler_native_id")
    add_assertion(
        assertions,
        "execution.scheduler_native_id",
        passed=isinstance(native_id, str) and native_id.isdigit(),
        expected="non-empty decimal Slurm job ID",
        actual=native_id,
    )

    package = _single_by_key(progress.get("packages"), "package_id", PACKAGE_ID)
    add_assertion(
        assertions,
        "progress.package_present_once",
        passed=package is not None,
        expected=PACKAGE_ID,
        actual=None if package is None else package.get("package_id"),
    )
    if package is not None:
        _assert_equal(assertions, "progress.event_count", package.get("event_count"), 4)
        latest = package.get("latest")
        latest = latest if isinstance(latest, Mapping) else {}
        for key, expected in (
            ("state", "completed"),
            ("current", float(EXPECTED_STEPS)),
            ("total", float(EXPECTED_STEPS)),
            ("unit", "timestep"),
        ):
            _assert_equal(
                assertions, f"progress.latest.{key}", latest.get(key), expected
            )
        metadata = latest.get("metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        _assert_equal(
            assertions,
            "progress.completion_signal",
            metadata.get("completion_signal"),
            "process_exit_zero_after_final_output",
        )

    current_artifacts = artifacts.get("artifacts")
    package_artifacts = (
        [
            item
            for item in current_artifacts
            if isinstance(item, Mapping) and item.get("package_id") == PACKAGE_ID
        ]
        if isinstance(current_artifacts, list)
        else []
    )
    _assert_equal(
        assertions,
        "artifacts.package_logical_names",
        sorted(str(item.get("logical_name")) for item in package_artifacts),
        sorted([OUTPUT_LOGICAL_NAME, CHECKPOINT_LOGICAL_NAME]),
    )
    output = _single_by_key(current_artifacts, "logical_name", OUTPUT_LOGICAL_NAME)
    _assert_artifact(
        assertions,
        artifact=output,
        assertion_prefix="artifacts.output",
        logical_name=OUTPUT_LOGICAL_NAME,
        expected_path=output_path,
        expected_kind="scientific_dataset",
        expected_role="output",
        expected_revision=4,
        expected_metadata={
            "application": "gray_scott",
            "io_backend": "adios2",
            "member_pattern": "adios2-steps",
            "members_observed": 2,
            "latest_timestep": EXPECTED_STEPS,
            "completion_signal": "process_exit_zero_after_final_output",
        },
    )
    checkpoint = _single_by_key(
        current_artifacts,
        "logical_name",
        CHECKPOINT_LOGICAL_NAME,
    )
    _assert_artifact(
        assertions,
        artifact=checkpoint,
        assertion_prefix="artifacts.checkpoint",
        logical_name=CHECKPOINT_LOGICAL_NAME,
        expected_path=checkpoint_path,
        expected_kind="restart_checkpoint",
        expected_role="checkpoint",
        expected_revision=1,
        expected_metadata={
            "application": "gray_scott",
            "io_backend": "adios2",
            "detection_signal": "configured_path_created_or_changed_during_process",
            "physical_path_observed": True,
            "latest_output_timestep_observed": EXPECTED_STEPS,
            "completion_signal": "process_exit_zero_after_final_output",
            "return_code": 0,
        },
    )

    _assert_bpls(assertions, bpls_output)
    return assertions


def _assert_equal(
    assertions: list[dict[str, Any]], name: str, actual: Any, expected: Any
) -> None:
    """Append one exact-equality assertion."""
    add_assertion(
        assertions,
        name,
        passed=actual == expected,
        expected=expected,
        actual=actual,
    )


def _single_by_key(
    values: object,
    key: str,
    expected: str,
) -> Mapping[str, Any] | None:
    """Return one matching mapping, rejecting missing or duplicate values."""
    if not isinstance(values, list):
        return None
    matches = [
        item
        for item in values
        if isinstance(item, Mapping) and item.get(key) == expected
    ]
    return matches[0] if len(matches) == 1 else None


def _assert_artifact(
    assertions: list[dict[str, Any]],
    *,
    artifact: Mapping[str, Any] | None,
    assertion_prefix: str,
    logical_name: str,
    expected_path: Path,
    expected_kind: str,
    expected_role: str,
    expected_revision: int,
    expected_metadata: Mapping[str, Any],
) -> None:
    """Assert one exact package artifact without depending on opaque IDs."""
    add_assertion(
        assertions,
        f"{assertion_prefix}.present_once",
        passed=artifact is not None,
        expected=logical_name,
        actual=None if artifact is None else artifact.get("logical_name"),
    )
    if artifact is None:
        return
    for key, expected in (
        ("package_id", PACKAGE_ID),
        ("state", "finalized"),
        ("kind", expected_kind),
        ("role", expected_role),
        ("structure", "collection"),
        ("ownership", "shared"),
        ("media_type", "application/x-adios2-bp"),
        ("format", "adios2-bp5"),
        ("revision", expected_revision),
    ):
        _assert_equal(
            assertions, f"{assertion_prefix}.{key}", artifact.get(key), expected
        )
    artifact_id = artifact.get("artifact_id")
    add_assertion(
        assertions,
        f"{assertion_prefix}.artifact_id",
        passed=isinstance(artifact_id, str)
        and re.fullmatch(r"art_[A-Za-z0-9_-]{22,86}", artifact_id) is not None,
        expected="opaque art_* identifier",
        actual=artifact_id,
    )
    location = artifact.get("location")
    location = location if isinstance(location, Mapping) else {}
    _assert_equal(
        assertions,
        f"{assertion_prefix}.location.kind",
        location.get("kind"),
        "cluster_path",
    )
    _assert_equal(
        assertions,
        f"{assertion_prefix}.location.value",
        location.get("value"),
        expected_path.as_posix(),
    )
    metadata = artifact.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    for key, expected in expected_metadata.items():
        _assert_equal(
            assertions,
            f"{assertion_prefix}.metadata.{key}",
            metadata.get(key),
            expected,
        )


def _assert_bpls(
    assertions: list[dict[str, Any]],
    results: Mapping[str, Mapping[str, Any]],
) -> None:
    """Assert physical ADIOS2 datasets independently of JARVIS metadata."""
    for name in (
        "output_list",
        "output_steps",
        "checkpoint_list",
        "checkpoint_steps",
    ):
        result = results.get(name, {})
        _assert_equal(
            assertions, f"bpls.{name}.return_code", result.get("return_code"), 0
        )
    output_listing = str(results.get("output_list", {}).get("stdout", ""))
    output_steps = str(results.get("output_steps", {}).get("stdout", ""))
    checkpoint_listing = str(results.get("checkpoint_list", {}).get("stdout", ""))
    checkpoint_steps = str(results.get("checkpoint_steps", {}).get("stdout", ""))
    for variable in ("U", "V", "step"):
        add_assertion(
            assertions,
            f"bpls.output.variable.{variable}",
            passed=re.search(rf"\b{re.escape(variable)}\b", output_listing) is not None,
            expected=f"variable {variable}",
            actual=output_listing,
        )
    for expected_step in (10, 20):
        add_assertion(
            assertions,
            f"bpls.output.step.{expected_step}",
            passed=re.search(rf"\b{expected_step}\b", output_steps) is not None,
            expected=f"physical output contains timestep {expected_step}",
            actual=output_steps,
        )
    for variable in ("U", "V", "step"):
        add_assertion(
            assertions,
            f"bpls.checkpoint.variable.{variable}",
            passed=re.search(rf"\b{re.escape(variable)}\b", checkpoint_listing)
            is not None,
            expected=f"checkpoint variable {variable}",
            actual=checkpoint_listing,
        )
    add_assertion(
        assertions,
        "bpls.checkpoint.step.20",
        passed=re.search(r"\b20\b", checkpoint_steps) is not None,
        expected="latest restart checkpoint contains timestep 20",
        actual=checkpoint_steps,
    )


def _new_report(args: argparse.Namespace) -> dict[str, Any]:
    """Create a report document before any external side effect."""
    return {
        "schema_version": REPORT_SCHEMA,
        "started_at": utc_now(),
        "finished_at": None,
        "success": False,
        "phase": "initializing",
        "inputs": {
            "wheel": str(args.wheel),
            "expected_version": args.expected_version,
            "expected_wheel_sha256": args.expected_wheel_sha256,
            "root": str(args.root),
            "report": str(args.report),
            "partition": args.partition,
            "account": args.account,
            "clio_core_root": str(args.clio_core_root),
            "executable": str(args.executable),
            "spack": str(args.spack),
            "spack_spec": args.spack_spec,
            "bpls": str(args.bpls) if args.bpls is not None else None,
            "timeout_seconds": args.timeout_seconds,
            "poll_seconds": args.poll_seconds,
            "cancel_on_timeout": args.cancel_on_timeout,
        },
        "provenance": {},
        "acceptance_contract": {
            "package": "builtin.gray_scott",
            "application": "clio-core/external/iowarp-gray-scott",
            "grid": [32, 32, 32],
            "steps": EXPECTED_STEPS,
            "output_every": EXPECTED_PLOTGAP,
            "expected_output_steps": [10, 20],
            "progress_terminal_signal": "process_exit_zero_after_final_output",
            "artifact_logical_name": OUTPUT_LOGICAL_NAME,
            "checkpoint_artifact_logical_name": CHECKPOINT_LOGICAL_NAME,
            "artifact_state": "finalized",
            "artifact_format": "adios2-bp5",
            "artifact_completion_signal": "process_exit_zero_after_final_output",
            "scheduler_log_pattern": "%j",
            "scheduler_log_validation": ("resolved-path-exists-and-contains-native-id"),
        },
        "execution": {},
        "queries": {},
        "physical_validation": {},
        "assertions": [],
        "commands": [],
        "error": None,
    }


def _record_command(
    report: dict[str, Any],
    argv: Sequence[str],
    result: subprocess.CompletedProcess[str],
) -> None:
    """Append bounded subprocess evidence to the report."""
    report["commands"].append(
        {
            "argv": list(argv),
            "return_code": result.returncode,
            "stdout": result.stdout[-32_768:],
            "stderr": result.stderr[-32_768:],
        }
    )


def _run_command(
    report: dict[str, Any],
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    check: bool = True,
    timeout: float = 120.0,
) -> subprocess.CompletedProcess[str]:
    """Run one argv command and retain bounded diagnostics."""
    result = subprocess.run(
        list(argv),
        cwd=cwd,
        env=dict(env) if env is not None else None,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
    )
    _record_command(report, argv, result)
    if check and result.returncode != 0:
        raise RuntimeError(
            f"command failed with exit {result.returncode}: "
            + " ".join(str(item) for item in argv)
        )
    return result


def _validate_arguments(args: argparse.Namespace) -> None:
    """Reject unsafe or ambiguous live inputs before installation/submission."""
    for name in (
        "wheel",
        "root",
        "report",
        "clio_core_root",
        "executable",
        "spack",
    ):
        value = getattr(args, name)
        if not isinstance(value, Path) or not value.is_absolute():
            raise ValueError(f"--{name.replace('_', '-')} must be an absolute path")
    if args.bpls is not None and not args.bpls.is_absolute():
        raise ValueError("--bpls must be an absolute path")
    if not args.wheel.name.endswith(".whl") or not args.wheel.is_file():
        raise ValueError(f"--wheel must name an existing .whl file: {args.wheel}")
    if (
        not isinstance(args.expected_version, str)
        or _SAFE_VERSION.fullmatch(args.expected_version) is None
    ):
        raise ValueError("--expected-version must be one valid version token")
    if (
        not isinstance(args.expected_wheel_sha256, str)
        or _SHA256.fullmatch(args.expected_wheel_sha256) is None
    ):
        raise ValueError("--expected-wheel-sha256 must be 64 lowercase hex digits")
    if args.executable.name != "gray-scott":
        raise ValueError("--executable must name the clio-core gray-scott binary")
    if not args.executable.is_relative_to(args.clio_core_root):
        raise ValueError("--executable must belong to the selected clio-core checkout")
    if not args.executable.is_file() or not os.access(args.executable, os.X_OK):
        raise ValueError(
            f"--executable must be an executable regular file: {args.executable}"
        )
    if not (args.clio_core_root / ".git").is_dir():
        raise ValueError(
            f"--clio-core-root is not a git checkout: {args.clio_core_root}"
        )
    if not args.spack.is_file() or not os.access(args.spack, os.X_OK):
        raise ValueError(f"--spack must be executable: {args.spack}")
    if _SAFE_SPEC.fullmatch(args.spack_spec) is None or args.spack_spec.startswith("-"):
        raise ValueError("--spack-spec contains unsupported characters")
    for name in ("partition", "account"):
        value = getattr(args, name)
        if value is not None and _SAFE_SCHEDULER_TOKEN.fullmatch(value) is None:
            raise ValueError(f"--{name} must be one printable scheduler token")
    if args.timeout_seconds <= 0 or args.poll_seconds <= 0:
        raise ValueError("poll and timeout values must be positive")
    if args.poll_seconds > args.timeout_seconds:
        raise ValueError("--poll-seconds cannot exceed --timeout-seconds")


def _venv_python(venv: Path) -> Path:
    """Return the Python entry point for a platform-specific virtualenv."""
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _assert_fresh_install_root(root: Path) -> None:
    """Refuse to replace state that may belong to another acceptance run."""
    reserved = (
        ".jarvis-live-probe-venv",
        "jarvis-root",
        "outputs",
        "private",
        "shared",
    )
    existing = [name for name in reserved if (root / name).exists()]
    if existing:
        raise RuntimeError(
            "live acceptance root already contains owned state: " + ", ".join(existing)
        )


def _bootstrap_installed_child(
    args: argparse.Namespace,
    report: dict[str, Any],
) -> int:
    """Install the exact wheel and re-execute this probe from that environment."""
    _assert_fresh_install_root(args.root)
    report["phase"] = "installing_wheel"
    report["provenance"]["wheel"] = {
        "path": str(args.wheel),
        "sha256": sha256_file(args.wheel),
        "size_bytes": args.wheel.stat().st_size,
    }
    _assert_expected_release_artifact(args, report)
    atomic_write_json(args.report, report)
    venv = args.root / ".jarvis-live-probe-venv"
    uv = args.uv or shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv is required to install the acceptance wheel")
    _run_command(report, [uv, "venv", "--clear", str(venv)], cwd=args.root)
    python = _venv_python(venv)
    _run_command(
        report,
        [
            uv,
            "pip",
            "install",
            "--python",
            str(python),
            "--reinstall",
            "--no-cache",
            str(args.wheel),
        ],
        cwd=args.root,
        timeout=300.0,
    )
    child_argv = [
        str(python),
        str(Path(__file__).resolve()),
        *sys.argv[1:],
        "--installed-child",
    ]
    child_env = dict(os.environ)
    child_env.pop("PYTHONPATH", None)
    child_env["PATH"] = os.pathsep.join([str(python.parent), child_env.get("PATH", "")])
    result = subprocess.run(
        child_argv,
        cwd=args.root,
        env=child_env,
        text=True,
        check=False,
    )
    if not args.report.is_file():
        raise RuntimeError(
            f"installed probe exited {result.returncode} without writing its report"
        )
    return result.returncode


def _installed_distribution_provenance() -> dict[str, Any]:
    """Return installed distribution and import-path evidence."""
    import jarvis_cd

    distribution = importlib.metadata.distribution("jarvis_cd")
    direct_url: Any = None
    direct_url_text = distribution.read_text("direct_url.json")
    if direct_url_text:
        direct_url = json.loads(direct_url_text)
    return {
        "distribution_name": distribution.metadata["Name"],
        "version": distribution.version,
        "module_path": str(Path(jarvis_cd.__file__).resolve()),
        "python_executable": sys.executable,
        "python_executable_resolved": str(Path(sys.executable).resolve()),
        "python_prefix": str(Path(sys.prefix).resolve()),
        "python_base_prefix": str(Path(sys.base_prefix).resolve()),
        "direct_url": direct_url,
    }


def _git_provenance(report: dict[str, Any], repository: Path) -> dict[str, Any]:
    """Return exact clio-core source identity and worktree state."""
    source_relative = "external/iowarp-gray-scott"
    commit = _run_command(
        report,
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
    ).stdout.strip()
    status = _run_command(
        report,
        ["git", "-C", str(repository), "status", "--porcelain=v1"],
    ).stdout
    source_status = _run_command(
        report,
        [
            "git",
            "-C",
            str(repository),
            "status",
            "--porcelain=v1",
            "--",
            source_relative,
        ],
    ).stdout
    source_tree = _run_command(
        report,
        [
            "git",
            "-C",
            str(repository),
            "rev-parse",
            f"HEAD:{source_relative}",
        ],
    ).stdout.strip()
    remote = _run_command(
        report,
        ["git", "-C", str(repository), "remote", "get-url", "origin"],
        check=False,
    ).stdout.strip()
    return {
        "path": str(repository),
        "commit": commit,
        "dirty": bool(status.strip()),
        "status_porcelain": status.splitlines(),
        "origin": remote or None,
        "gray_scott_source": {
            "relative_path": source_relative,
            "git_tree": source_tree,
            "dirty": bool(source_status.strip()),
            "status_porcelain": source_status.splitlines(),
        },
    }


def _spack_provenance(report: dict[str, Any], spack: Path, spec: str) -> dict[str, Any]:
    """Return the selected installed Spack spec and its machine digest."""
    format_string = "{hash:32}|{name}|{version}|{prefix}"
    result = _run_command(
        report,
        [str(spack), "find", "--format", format_string, spec],
    )
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise RuntimeError(
            f"Spack spec must resolve to exactly one installation: {spec}"
        )
    fields = lines[0].split("|", 3)
    if len(fields) != 4:
        raise RuntimeError("Spack returned malformed concrete-spec provenance")
    dag_hash, name, version, prefix_text = fields
    prefix = Path(prefix_text)
    if not re.fullmatch(r"[a-z0-9]{32}", dag_hash) or not prefix.is_absolute():
        raise RuntimeError("Spack returned invalid concrete hash or prefix")
    if not prefix.is_dir():
        raise RuntimeError(f"Spack installation prefix does not exist: {prefix}")

    json_result = _run_command(
        report,
        [str(spack), "find", "--json", f"/{dag_hash}"],
        check=False,
    )
    parsed: Any = None
    json_sha256: str | None = None
    if json_result.stdout.strip():
        try:
            parsed = json.loads(json_result.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError("Spack emitted malformed JSON provenance") from error
        canonical = json.dumps(
            parsed, allow_nan=False, separators=(",", ":"), sort_keys=True
        )
        json_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return {
        "requested": spec,
        "name": name,
        "version": version,
        "dag_hash": dag_hash,
        "prefix": str(prefix),
        "resolved": parsed,
        "spec_hashes": sorted(_spack_hashes(parsed)),
        "resolved_json_sha256": json_sha256,
        "json_command_return_code": json_result.returncode,
    }


def _spack_hashes(value: object) -> set[str]:
    """Collect concrete DAG hashes from arbitrarily nested Spack JSON."""
    hashes: set[str] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in {"hash", "full_hash", "dag_hash"} and isinstance(item, str):
                if item:
                    hashes.add(item)
            else:
                hashes.update(_spack_hashes(item))
    elif isinstance(value, list):
        for item in value:
            hashes.update(_spack_hashes(item))
    return hashes


def _runtime_linkage(
    report: dict[str, Any], executable: Path, adios_prefix: Path
) -> dict[str, Any]:
    """Verify the binary links to ADIOS2 from the selected Spack prefix."""
    result = _run_command(report, ["ldd", str(executable)])
    libraries, missing = _parse_ldd_output(result.stdout)
    if missing:
        raise RuntimeError(f"Gray-Scott has unresolved shared libraries: {missing}")
    adios_paths = [
        Path(path).resolve()
        for name, path in libraries.items()
        if name.startswith("libadios2")
    ]
    if not adios_paths or any(
        not path.is_relative_to(adios_prefix.resolve()) for path in adios_paths
    ):
        raise RuntimeError(
            "Gray-Scott is not linked to ADIOS2 from the selected Spack prefix"
        )
    mpi_paths = [
        Path(path).resolve()
        for name, path in libraries.items()
        if name.startswith("libmpi.so")
    ]
    if len(mpi_paths) != 1:
        raise RuntimeError("Gray-Scott must resolve exactly one MPI runtime")
    mpi_prefix = mpi_paths[0].parent.parent
    mpi_executable = mpi_prefix / "bin" / "mpiexec"
    if not mpi_executable.is_file():
        raise RuntimeError(f"matching MPI launcher does not exist: {mpi_executable}")
    return {
        "libraries": libraries,
        "adios2_libraries": [str(path) for path in adios_paths],
        "mpi_library": str(mpi_paths[0]),
        "mpi_prefix": str(mpi_prefix),
        "mpi_executable": str(mpi_executable),
    }


def _parse_ldd_output(text: str) -> tuple[dict[str, str], list[str]]:
    """Parse the stable ``name => path`` subset of Linux ``ldd`` output."""
    libraries: dict[str, str] = {}
    missing: list[str] = []
    for line in text.splitlines():
        match = re.match(r"^\s*(?P<name>\S+)\s+=>\s+(?P<path>\S+)", line)
        if match is None:
            continue
        name = match.group("name")
        path = match.group("path")
        if path == "not":
            missing.append(name)
            continue
        libraries[name] = path
    return libraries, missing


def _spack_environment(
    *,
    base: Mapping[str, str],
    executable: Path,
    adios_prefix: Path,
    mpi_prefix: Path,
) -> dict[str, str]:
    """Build a bounded runtime environment from verified concrete prefixes."""
    environment = dict(base)
    path_parts = [
        str(Path(sys.executable).parent),
        str(executable.parent),
        str(mpi_prefix / "bin"),
        str(adios_prefix / "bin"),
        base.get("PATH", ""),
    ]
    library_parts = [
        str(adios_prefix / "lib"),
        str(mpi_prefix / "lib"),
        base.get("LD_LIBRARY_PATH", ""),
    ]
    environment["PATH"] = os.pathsep.join(part for part in path_parts if part)
    environment["LD_LIBRARY_PATH"] = os.pathsep.join(
        part for part in library_parts if part
    )
    return environment


def _pipeline_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """Select non-secret compiler/runtime variables needed inside Slurm."""
    allowed = {
        "ACLOCAL_PATH",
        "CMAKE_PREFIX_PATH",
        "CPATH",
        "CPLUS_INCLUDE_PATH",
        "C_INCLUDE_PATH",
        "INFOPATH",
        "LD_LIBRARY_PATH",
        "LIBRARY_PATH",
        "MANPATH",
        "MODULEPATH",
        "PATH",
        "PKG_CONFIG_PATH",
        "PYTHONPATH",
        "SPACK_ROOT",
    }
    prefixes = ("OMPI_", "OPAL_", "PMIX_", "PRTE_", "UCX_", "FI_")
    return {
        key: value
        for key, value in environment.items()
        if key in allowed or key.startswith(prefixes)
    }


def _assert_installed_import(provenance: Mapping[str, Any], venv: Path) -> None:
    """Prove JARVIS was imported through the probe virtual environment."""
    resolved_venv = venv.resolve()
    module_path = provenance.get("module_path")
    if not isinstance(module_path, str) or not Path(
        module_path
    ).resolve().is_relative_to(resolved_venv):
        raise RuntimeError(
            f"installed-wheel isolation failed for module_path: {module_path}"
        )

    python_prefix = provenance.get("python_prefix")
    if (
        not isinstance(python_prefix, str)
        or Path(python_prefix).resolve() != resolved_venv
    ):
        raise RuntimeError(
            f"installed-wheel isolation failed for python_prefix: {python_prefix}"
        )

    python_executable = provenance.get("python_executable")
    if not isinstance(python_executable, str) or not Path(
        python_executable
    ).absolute().is_relative_to(resolved_venv):
        raise RuntimeError(
            "installed-wheel isolation failed for python_executable entry point: "
            f"{python_executable}"
        )


def _assert_installed_package_source(
    pipeline_provenance: Mapping[str, Any],
    venv: Path,
) -> None:
    """Prove the selected built-in package came from the installed wheel."""
    package = pipeline_provenance.get("package")
    source = package.get("source_path") if isinstance(package, Mapping) else None
    if not isinstance(source, str) or not Path(source).resolve().is_relative_to(
        venv.resolve()
    ):
        raise RuntimeError(
            f"installed-wheel isolation failed for package source_path: {source}"
        )


def _configured_pipeline(
    args: argparse.Namespace,
    report: dict[str, Any],
    environment: Mapping[str, str],
    output_path: Path,
    mpi_executable: Path,
    run_id: str,
) -> Any:
    """Create and persist the exact one-package JARVIS pipeline."""
    from jarvis_cd.core.config import Jarvis
    from jarvis_cd.core.pipeline import Pipeline

    jarvis_root = args.root / "jarvis-root"
    Jarvis._instance = None
    jarvis = Jarvis.get_instance(str(jarvis_root))
    jarvis.initialize(
        config_dir=str(jarvis_root / "config"),
        private_dir=str(args.root / "private"),
        shared_dir=str(args.root / "shared"),
        force=True,
    )
    pipeline = Pipeline()
    pipeline.create(run_id)
    scheduler_log_root = output_path.parent / "scheduler-logs"
    scheduler_log_root.mkdir(parents=False, exist_ok=False)
    scheduler: dict[str, Any] = {
        "name": "slurm",
        "job_name": run_id,
        "nodes": 1,
        "ntasks": 1,
        "ntasks_per_node": 1,
        "time": "00:10:00",
        "partition": args.partition,
        "output": str((scheduler_log_root / "stdout-%j.log").resolve()),
        "error": str((scheduler_log_root / "stderr-%j.log").resolve()),
    }
    if args.account is not None:
        scheduler["account"] = args.account
    pipeline.scheduler = scheduler
    pipeline.mpi_cmd = str(mpi_executable)
    pipeline.env = _pipeline_environment(environment)
    config_args = [
        "nprocs=1",
        "ppn=1",
        f"executable={args.executable}",
        "width=32",
        "height=32",
        f"steps={EXPECTED_STEPS}",
        f"out_every={EXPECTED_PLOTGAP}",
        f"outdir={output_path}",
        "checkpoint=true",
        "checkpoint_freq=1",
        f"checkpoint_output={output_path}.checkpoint.bp",
        "adios_span=false",
        "adios_memory_selection=false",
        "mesh_type=image",
    ]
    pipeline.append(
        "builtin.gray_scott",
        package_alias=PACKAGE_ID,
        config_args=config_args,
    )
    package = pipeline.packages[-1]
    package_instance = pipeline._load_package_instance(package, pipeline.env)
    package_module = sys.modules.get(type(package_instance).__module__)
    package_source = getattr(package_module, "__file__", None)
    if not isinstance(package_source, str):
        raise RuntimeError("could not resolve the selected Gray-Scott package source")
    package_source = str(Path(package_source).resolve())
    expected_config = {
        "nprocs": 1,
        "ppn": 1,
        "executable": str(args.executable),
        "width": 32,
        "height": 32,
        "steps": EXPECTED_STEPS,
        "out_every": EXPECTED_PLOTGAP,
        "outdir": str(output_path),
        "checkpoint": True,
        "checkpoint_freq": 1,
        "checkpoint_output": f"{output_path}.checkpoint.bp",
        "adios_span": False,
        "adios_memory_selection": False,
        "mesh_type": "image",
    }
    actual_config = package.get("config", {})
    mismatches = {
        key: {"expected": expected, "actual": actual_config.get(key)}
        for key, expected in expected_config.items()
        if actual_config.get(key) != expected
    }
    if mismatches:
        raise RuntimeError(
            f"installed wheel rejected the live package contract: {mismatches}"
        )
    pipeline.save()
    report["pipeline"] = {
        "pipeline_id": run_id,
        "jarvis_root": str(jarvis.jarvis_root),
        "repositories": list(jarvis.repos.get("repos", [])),
        "scheduler": scheduler,
        "mpi_executable": str(mpi_executable),
        "base_deploy_mode": pipeline.base_deploy_mode,
        "package": {
            "pkg_type": package.get("pkg_type"),
            "pkg_id": package.get("pkg_id"),
            "source_path": package_source,
            "config": {key: actual_config.get(key) for key in expected_config},
            "effective_deploy_mode": pipeline.base_deploy_mode or "default",
        },
        "environment_keys": sorted(pipeline.env),
    }
    return pipeline


def _poll_execution(
    args: argparse.Namespace,
    report: dict[str, Any],
    pipeline: Any,
    execution_id: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Query durable execution, progress, and artifacts until terminal."""
    deadline = time.monotonic() + args.timeout_seconds
    states: list[dict[str, Any]] = []
    previous_state: tuple[Any, ...] | None = None
    while True:
        record = pipeline.get_execution(execution_id).to_dict()
        progress = pipeline.get_execution_progress(execution_id).to_dict()
        artifacts = pipeline.get_execution_artifacts(execution_id).to_dict()
        current_state = (
            record.get("state"),
            record.get("terminal"),
            tuple(
                (item.get("package_id"), item.get("event_count"))
                for item in progress.get("packages", [])
                if isinstance(item, Mapping)
            ),
            tuple(
                (item.get("logical_name"), item.get("revision"), item.get("state"))
                for item in artifacts.get("artifacts", [])
                if isinstance(item, Mapping)
            ),
        )
        if current_state != previous_state:
            states.append(
                {
                    "observed_at": utc_now(),
                    "record_state": record.get("state"),
                    "terminal": record.get("terminal"),
                    "progress": progress,
                    "artifacts": artifacts,
                }
            )
            previous_state = current_state
            report["queries"]["timeline"] = states
            atomic_write_json(args.report, report)
        if record.get("terminal") is True:
            return record, progress, artifacts
        if time.monotonic() >= deadline:
            if args.cancel_on_timeout:
                native_id = record.get("scheduler_native_id")
                if isinstance(native_id, str) and native_id.isdigit():
                    _run_command(report, ["scancel", native_id], check=False)
            raise TimeoutError(
                f"execution {execution_id} did not become terminal in "
                f"{args.timeout_seconds} seconds"
            )
        time.sleep(args.poll_seconds)


def _run_bpls(
    report: dict[str, Any],
    bpls: Path,
    output_path: Path,
    checkpoint_path: Path,
) -> dict[str, dict[str, Any]]:
    """Inspect physical output and restart datasets with ``bpls``."""
    commands = {
        "output_list": [str(bpls), "-l", str(output_path)],
        "output_steps": [str(bpls), "-d", str(output_path), "step"],
        "checkpoint_list": [str(bpls), "-l", str(checkpoint_path)],
        "checkpoint_steps": [str(bpls), "-d", str(checkpoint_path), "step"],
    }
    results: dict[str, dict[str, Any]] = {}
    for name, argv in commands.items():
        completed = _run_command(report, argv, check=False)
        results[name] = {
            "argv": argv,
            "return_code": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
    return results


def _execution_log_evidence(
    submission: Mapping[str, Any],
    artifacts: Mapping[str, Any],
    scheduler_native_id: str,
) -> dict[str, Any]:
    """Validate referenced core logs and return bounded physical evidence."""
    if not scheduler_native_id:
        raise RuntimeError("execution record has no scheduler-native identity")
    root_value = submission.get("execution_root_path")
    if not isinstance(root_value, str) or not root_value:
        raise RuntimeError("scheduler submission did not retain its execution root")
    execution_root = Path(root_value)
    evidence: dict[str, Any] = {}
    current_artifacts = artifacts.get("artifacts")
    for logical_name in ("stdout", "stderr"):
        artifact = _single_by_key(
            current_artifacts,
            "logical_name",
            logical_name,
        )
        if artifact is None or artifact.get("package_id") != "jarvis-core":
            raise RuntimeError(
                f"scheduler {logical_name} artifact reference is missing or duplicated"
            )
        if artifact.get("state") != "finalized":
            raise RuntimeError(
                f"scheduler {logical_name} artifact reference is not finalized"
            )
        location = artifact.get("location")
        if not isinstance(location, Mapping):
            raise RuntimeError(
                f"scheduler {logical_name} artifact has no resolved location"
            )
        location_kind = location.get("kind")
        location_value = location.get("value")
        if not isinstance(location_value, str) or not location_value:
            raise RuntimeError(f"scheduler {logical_name} artifact location is invalid")
        if "%" in location_value:
            raise RuntimeError(
                f"scheduler {logical_name} artifact location retains a Slurm token"
            )
        if scheduler_native_id not in location_value:
            raise RuntimeError(
                f"scheduler {logical_name} artifact location does not contain "
                "the execution scheduler-native ID"
            )
        if location_kind == "execution_path":
            path = execution_root / location_value
        elif location_kind == "cluster_path":
            path = Path(location_value)
        else:
            raise RuntimeError(
                f"scheduler {logical_name} artifact location kind is unsupported"
            )
        status = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(status.st_mode):
            raise RuntimeError(f"scheduler log is not a regular file: {path}")
        with path.open("rb") as stream:
            if status.st_size > 65_536:
                stream.seek(status.st_size - 65_536)
            tail = stream.read().decode("utf-8", errors="replace")
        evidence[logical_name] = {
            "artifact_id": artifact.get("artifact_id"),
            "artifact_state": artifact.get("state"),
            "location": dict(location),
            "path": str(path),
            "size_bytes": status.st_size,
            "sha256": sha256_file(path),
            "tail": tail,
        }
    return evidence


def _run_installed_probe(args: argparse.Namespace, report: dict[str, Any]) -> None:
    """Run provenance, Slurm execution, queries, and physical validation."""
    report["phase"] = "collecting_provenance"
    venv = args.root / ".jarvis-live-probe-venv"
    installed = _installed_distribution_provenance()
    _assert_installed_import(installed, venv)
    report["provenance"]["installed_jarvis"] = installed
    report["provenance"]["wheel"] = {
        "path": str(args.wheel),
        "sha256": sha256_file(args.wheel),
        "size_bytes": args.wheel.stat().st_size,
    }
    _assert_expected_release_artifact(
        args,
        report,
        installed_version=str(installed["version"]),
    )
    report["provenance"]["clio_core"] = _git_provenance(report, args.clio_core_root)
    report["provenance"]["binary"] = {
        "path": str(args.executable),
        "sha256": sha256_file(args.executable),
        "size_bytes": args.executable.stat().st_size,
    }
    spack_provenance = _spack_provenance(
        report,
        args.spack,
        args.spack_spec,
    )
    report["provenance"]["spack_adios2"] = spack_provenance
    adios_prefix = Path(spack_provenance["prefix"])
    linkage = _runtime_linkage(report, args.executable, adios_prefix)
    report["provenance"]["binary"]["runtime_linkage"] = linkage
    mpi_prefix = Path(linkage["mpi_prefix"])
    mpi_executable = Path(linkage["mpi_executable"])
    environment = _spack_environment(
        base=os.environ,
        executable=args.executable,
        adios_prefix=adios_prefix,
        mpi_prefix=mpi_prefix,
    )
    os.environ.update(environment)
    bpls = args.bpls or adios_prefix / "bin" / "bpls"
    if not bpls.is_absolute() or not bpls.is_file():
        raise RuntimeError("bpls was not resolved from the selected Spack environment")
    bpls = bpls.resolve()
    if not bpls.is_relative_to(adios_prefix.resolve()):
        raise RuntimeError("bpls does not belong to the selected Spack ADIOS2 prefix")
    if not os.access(bpls, os.X_OK):
        raise RuntimeError(f"bpls is not executable: {bpls}")
    report["provenance"]["bpls"] = {
        "path": str(bpls),
        "sha256": sha256_file(bpls),
        "size_bytes": bpls.stat().st_size,
    }

    run_id = "ares-gray-scott-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id += "-" + uuid4().hex[:8]
    output_root = args.root / "outputs" / run_id
    output_root.mkdir(parents=True, exist_ok=False)
    output_path = output_root / "gray-scott.bp"
    checkpoint_path = Path(f"{output_path}.checkpoint.bp")

    report["phase"] = "configuring_pipeline"
    pipeline = _configured_pipeline(
        args,
        report,
        environment,
        output_path,
        mpi_executable,
        run_id,
    )
    _assert_installed_package_source(report["pipeline"], venv)
    atomic_write_json(args.report, report)

    report["phase"] = "submitting"
    handle = pipeline.submit(submit=True, wait=False)
    report["execution"]["handle"] = handle.to_dict()
    report["execution"]["submission"] = dict(pipeline.last_submission or {})
    atomic_write_json(args.report, report)

    report["phase"] = "polling"
    record, progress, artifacts = _poll_execution(
        args,
        report,
        pipeline,
        handle.execution_id,
    )
    report["queries"].update(
        {"record": record, "progress": progress, "artifacts": artifacts}
    )
    report["execution"]["logs"] = _execution_log_evidence(
        report["execution"]["submission"],
        artifacts,
        str(record.get("scheduler_native_id") or ""),
    )
    report["phase"] = "physical_validation"
    bpls_output = _run_bpls(
        report,
        bpls,
        output_path,
        checkpoint_path,
    )
    report["physical_validation"] = bpls_output
    report["assertions"].extend(
        evaluate_semantics(
            record=record,
            progress=progress,
            artifacts=artifacts,
            output_path=output_path,
            checkpoint_path=checkpoint_path,
            bpls_output=bpls_output,
        )
    )
    failures = [item for item in report["assertions"] if not item["passed"]]
    if failures:
        names = ", ".join(str(item["name"]) for item in failures)
        raise AssertionError(f"live acceptance assertions failed: {names}")
    report["phase"] = "complete"
    report["success"] = True


def _parser() -> argparse.ArgumentParser:
    """Build the live probe command-line parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--expected-version")
    parser.add_argument("--expected-wheel-sha256", type=str.lower)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--partition", required=True)
    parser.add_argument("--account")
    parser.add_argument(
        "--clio-core-root", type=Path, default=Path.home() / "clio-core"
    )
    parser.add_argument(
        "--executable",
        type=Path,
        default=(
            Path.home() / "clio-core/external/iowarp-gray-scott/install/bin/gray-scott"
        ),
    )
    parser.add_argument("--spack", type=Path, default=Path.home() / "spack/bin/spack")
    parser.add_argument("--spack-spec", default="adios2")
    parser.add_argument("--bpls", type=Path)
    parser.add_argument("--uv")
    parser.add_argument("--timeout-seconds", type=float, default=600.0)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--cancel-on-timeout", action="store_true")
    parser.add_argument(
        "--installed-child", action="store_true", help=argparse.SUPPRESS
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the probe and return zero only for a fully passing report."""
    args = _parser().parse_args(argv)
    args.wheel = args.wheel.expanduser().resolve()
    args.root = args.root.expanduser().resolve()
    args.root.mkdir(parents=True, exist_ok=True)
    args.report = (
        args.report.expanduser().resolve()
        if args.report is not None
        else args.root / "live-ares-gray-scott-report.json"
    )
    args.clio_core_root = args.clio_core_root.expanduser().resolve()
    args.executable = args.executable.expanduser().resolve()
    args.spack = args.spack.expanduser().resolve()
    if args.bpls is not None:
        args.bpls = args.bpls.expanduser().resolve()
    report = _new_report(args)
    if args.installed_child and args.report.is_file():
        try:
            bootstrap = json.loads(args.report.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            bootstrap = None
        if isinstance(bootstrap, Mapping):
            report["bootstrap"] = {
                "started_at": bootstrap.get("started_at"),
                "phase": bootstrap.get("phase"),
                "commands": bootstrap.get("commands", []),
                "wheel": bootstrap.get("provenance", {}).get("wheel")
                if isinstance(bootstrap.get("provenance"), Mapping)
                else None,
            }
    if not args.installed_child:
        try:
            _validate_arguments(args)
            atomic_write_json(args.report, report)
            return _bootstrap_installed_child(args, report)
        except BaseException as error:
            report["success"] = False
            report["error"] = {
                "type": type(error).__name__,
                "message": str(error),
                "traceback": traceback.format_exc(),
            }
            report["finished_at"] = utc_now()
            try:
                atomic_write_json(args.report, report)
            except BaseException as report_error:
                print(
                    f"could not write live report {args.report}: {report_error}",
                    file=sys.stderr,
                )
            return 1

    exit_code = 1
    try:
        _validate_arguments(args)
        atomic_write_json(args.report, report)
        _run_installed_probe(args, report)
        exit_code = 0
    except BaseException as error:
        report["success"] = False
        report["error"] = {
            "type": type(error).__name__,
            "message": str(error),
            "traceback": traceback.format_exc(),
        }
    finally:
        report["finished_at"] = utc_now()
        try:
            atomic_write_json(args.report, report)
        except BaseException as report_error:
            print(
                f"could not write live report {args.report}: {report_error}",
                file=sys.stderr,
            )
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
