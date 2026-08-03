"""Pure input, execution-directory, and result contracts for WRF."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

from jarvis_cd.input_bundle import InputBundleError, MaterializedInputBundle

FORMULATIONS = (("constant-z0q", 1), ("garratt", 2))
RUN_HOURS = 24
GRID_POINTS = 121
HISTORY_INTERVAL_MINUTES = 360
RESULT_NAME = "wrf-tropical-cyclone-result.json"
RESULT_SCHEMA = "scientific-benchmark.wrf-tropical-cyclone-result.v1"
RESULT_PREFIX = "BENCHMARK_WRF_TC "
EXPECTED_FILES = {"input_sounding", "namelist.input"}


def sha256_file(path: Path) -> str:
    """Return the SHA-256 digest of one regular file."""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def validate_wrf_bundle(bundle: MaterializedInputBundle) -> None:
    """Require the closed WRF v4.6.1 tropical-cyclone input pair."""

    observed = {item.path for item in bundle.manifest.files}
    if observed != EXPECTED_FILES:
        raise InputBundleError("WRF bundle does not contain the closed input pair")
    roles = {item.path: item.role for item in bundle.manifest.files}
    if roles != {
        "input_sounding": "atmospheric_sounding",
        "namelist.input": "wrf_namelist",
    }:
        raise InputBundleError("WRF input roles are invalid")
    if bundle.manifest.entrypoint != "namelist.input":
        raise InputBundleError("WRF bundle entrypoint is not namelist.input")


def validate_wrf_prefix(prefix: Path) -> Path:
    """Require a complete native WRF installation with the ideal TC executables."""

    resolved = prefix.resolve(strict=True)
    required = (
        resolved / "main" / "ideal.exe",
        resolved / "main" / "wrf.exe",
        resolved / "run",
    )
    if not all(path.exists() for path in required):
        raise ValueError("WRF prefix omitted ideal.exe, wrf.exe, or runtime data")
    if (
        not required[0].is_file()
        or not required[1].is_file()
        or not required[2].is_dir()
    ):
        raise ValueError("WRF prefix has invalid runtime object types")
    return resolved


def resolve_runtime_program(name: str, environment: Mapping[str, str]) -> Path:
    """Resolve one regular executable from JARVIS' loaded runtime environment."""

    configured_path = environment.get("PATH")
    candidate = shutil.which(name, path=configured_path)
    if candidate is None:
        raise ValueError(f"{name} is unavailable in the loaded runtime")
    resolved = Path(candidate).resolve(strict=True)
    if not resolved.is_file():
        raise ValueError(f"{name} did not resolve to a regular file")
    return resolved


def _safe_execution_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 128
        or any(
            character
            not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
            for character in value
        )
    ):
        raise InputBundleError("JARVIS execution identity is not a safe path token")
    return value


def materialize_run(
    bundle: MaterializedInputBundle, run_parent: Path, execution_id: object
) -> Path:
    """Copy immutable inputs into a new execution-specific directory."""

    validate_wrf_bundle(bundle)
    token = _safe_execution_id(execution_id)
    run_parent = run_parent.resolve()
    run_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = run_parent / token
    if destination.exists():
        raise InputBundleError("WRF execution working directory already exists")
    temporary = Path(tempfile.mkdtemp(prefix=f".{token}.", dir=run_parent))
    try:
        for item in bundle.manifest.files:
            source = bundle.root / PurePosixPath(item.path)
            target = temporary / PurePosixPath(item.path)
            shutil.copyfile(source, target, follow_symlinks=False)
            os.chmod(target, 0o400)
        (temporary / "input-provenance.json").write_text(
            json.dumps(
                {
                    "bundle_sha256": bundle.bundle_sha256,
                    "formulations": [name for name, _value in FORMULATIONS],
                    "grid_points": [GRID_POINTS, GRID_POINTS],
                    "run_hours": RUN_HOURS,
                    "schema_version": "scientific-benchmark.wrf-tropical-cyclone-input.v1",
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary / "input-provenance.json", 0o400)
        os.replace(temporary, destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return destination


def _replace_assignment(text: str, name: str, value: str) -> str:
    pattern = rf"(?m)^(\s*{re.escape(name)}\s*=\s*)[^,\n]+(,?)\s*$"
    updated, count = re.subn(pattern, rf"\g<1>{value}\2", text)
    if count != 1:
        raise ValueError(f"WRF namelist assignment is not unique: {name}")
    return updated


def prepare_case(
    run_root: Path,
    bundle: MaterializedInputBundle,
    wrf_prefix: Path,
    *,
    name: str,
    isftcflx: int,
) -> Path:
    """Create one bounded WRF case without mutating the installed prefix."""

    if (name, isftcflx) not in FORMULATIONS:
        raise ValueError("unsupported WRF surface-exchange formulation")
    prefix = validate_wrf_prefix(wrf_prefix)
    case_root = run_root / name
    case_root.mkdir(mode=0o700)
    for source in sorted((prefix / "run").iterdir(), key=lambda item: item.name):
        if source.name == "namelist.input" or not source.is_file():
            continue
        target = case_root / source.name
        if os.name == "nt":
            shutil.copyfile(source, target, follow_symlinks=False)
        else:
            target.symlink_to(source)
    shutil.copyfile(bundle.root / "input_sounding", case_root / "input_sounding")
    base = (bundle.root / "namelist.input").read_text(encoding="utf-8", errors="strict")
    replacements = {
        "run_days": "0",
        "run_hours": str(RUN_HOURS),
        "end_day": "2",
        "end_hour": "0",
        "history_interval": str(HISTORY_INTERVAL_MINUTES),
        "e_we": str(GRID_POINTS),
        "e_sn": str(GRID_POINTS),
        "isftcflx": str(isftcflx),
    }
    for setting, value in replacements.items():
        base = _replace_assignment(base, setting, value)
    (case_root / "namelist.input").write_text(base, encoding="utf-8", newline="\n")
    return case_root


def _cdl_variable(text: str, name: str) -> list[float]:
    match = re.search(rf"(?ms)^\s*{re.escape(name)}\s*=\s*(.*?);\s*$", text)
    if match is None:
        raise ValueError(f"WRF diagnostics omitted {name}")
    tokens = re.split(r"[\s,]+", match.group(1).strip())
    try:
        values = [
            float(token.rstrip("fF")) for token in tokens if token and token != "_"
        ]
    except ValueError as error:
        raise ValueError(f"WRF diagnostics contain malformed {name} values") from error
    if not values or not all(math.isfinite(value) for value in values):
        raise ValueError(f"WRF diagnostics contain empty or non-finite {name} values")
    return values


def parse_diagnostics(path: Path) -> dict[str, float | int]:
    """Extract final-time surface wind and pressure metrics from ncdump CDL."""

    if (
        path.is_symlink()
        or not path.is_file()
        or path.stat().st_size > 128 * 1024 * 1024
    ):
        raise ValueError("WRF diagnostic CDL is missing, unsafe, or oversized")
    text = path.read_text(encoding="utf-8", errors="strict")
    time_match = re.search(r"Time\s*=\s*UNLIMITED\s*;\s*//\s*\((\d+) currently\)", text)
    if time_match is None:
        raise ValueError("WRF diagnostics omitted the Time dimension")
    time_count = int(time_match.group(1))
    if time_count < 2:
        raise ValueError("WRF diagnostics did not cover an evolving forecast")
    u10 = _cdl_variable(text, "U10")
    v10 = _cdl_variable(text, "V10")
    psfc = _cdl_variable(text, "PSFC")
    if len(u10) != len(v10) or len(u10) != len(psfc) or len(u10) % time_count:
        raise ValueError("WRF surface fields have inconsistent dimensions")
    points = len(u10) // time_count
    final_u = u10[-points:]
    final_v = v10[-points:]
    final_p = psfc[-points:]
    maximum_wind = max(math.hypot(u, v) for u, v in zip(final_u, final_v, strict=True))
    minimum_pressure_hpa = min(final_p) / 100.0
    if not 0.0 < maximum_wind < 200.0 or not 700.0 < minimum_pressure_hpa < 1100.0:
        raise ValueError("WRF final surface metrics are physically implausible")
    return {
        "final_maximum_ten_meter_wind_m_s": maximum_wind,
        "final_minimum_surface_pressure_hpa": minimum_pressure_hpa,
        "surface_point_count": points,
        "time_count": time_count,
    }


def build_result(run_root: Path, bundle: MaterializedInputBundle) -> dict[str, object]:
    """Build the closed comparison document from two completed WRF cases."""

    cases: list[dict[str, object]] = []
    for name, value in FORMULATIONS:
        case_root = run_root / name
        outputs = sorted(case_root.glob("wrfout_d01_*"))
        if len(outputs) != 1 or outputs[0].is_symlink() or not outputs[0].is_file():
            raise ValueError(f"WRF {name} did not produce one closed history file")
        diagnostics = case_root / "surface-diagnostics.cdl"
        cases.append(
            {
                "formulation": name,
                "isftcflx": value,
                **parse_diagnostics(diagnostics),
                "diagnostics_sha256": sha256_file(diagnostics),
                "history_file": outputs[0].name,
                "history_sha256": sha256_file(outputs[0]),
                "namelist_sha256": sha256_file(case_root / "namelist.input"),
            }
        )
    by_name = {str(case["formulation"]): case for case in cases}
    constant = by_name["constant-z0q"]
    garratt = by_name["garratt"]

    def metric(case: dict[str, object], name: str) -> float:
        value = case.get(name)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(f"WRF result metric is malformed: {name}")
        return float(value)

    return {
        "cases": cases,
        "comparison": {
            "garratt_minus_constant_final_maximum_ten_meter_wind_m_s": (
                metric(garratt, "final_maximum_ten_meter_wind_m_s")
                - metric(constant, "final_maximum_ten_meter_wind_m_s")
            ),
            "garratt_minus_constant_final_minimum_surface_pressure_hpa": (
                metric(garratt, "final_minimum_surface_pressure_hpa")
                - metric(constant, "final_minimum_surface_pressure_hpa")
            ),
        },
        "grid_points": [GRID_POINTS, GRID_POINTS],
        "input_bundle_sha256": bundle.bundle_sha256,
        "run_hours": RUN_HOURS,
        "schema_version": RESULT_SCHEMA,
    }


def write_result(
    run_root: Path, destination: Path, bundle: MaterializedInputBundle
) -> dict[str, object]:
    """Validate both cases and atomically write their comparison."""

    document = build_result(run_root, bundle)
    if destination.exists():
        raise ValueError("WRF result destination already exists")
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(document, stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return document


def validate_result_document(
    run_root: Path, result_path: Path, bundle: MaterializedInputBundle
) -> dict[str, object]:
    """Require the result document to exactly match both WRF products."""

    if result_path.is_symlink() or not result_path.is_file():
        raise ValueError("WRF result document is missing or unsafe")
    observed = json.loads(result_path.read_text(encoding="utf-8", errors="strict"))
    expected = build_result(run_root, bundle)
    if observed != expected:
        raise ValueError("WRF result document does not match generated fields")
    return expected


def result_summary_line(document: dict[str, object]) -> str:
    """Render the closed terminal summary consumed by JARVIS progress."""

    cases = document.get("cases")
    if not isinstance(cases, list) or len(cases) != len(FORMULATIONS):
        raise ValueError("WRF result cases are missing")
    return (
        f"{RESULT_PREFIX}schema={RESULT_SCHEMA} formulations=constant-z0q,garratt "
        f"hours={RUN_HOURS} grid={GRID_POINTS}x{GRID_POINTS}"
    )
