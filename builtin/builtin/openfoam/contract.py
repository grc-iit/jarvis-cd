"""Pure input, execution-directory, and result contract for OpenFOAM airfoil."""

from __future__ import annotations

import json
import math
import os
import shutil
import statistics
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

from jarvis_cd.input_bundle import InputBundleError, MaterializedInputBundle

RESULT_FILE_SCHEMA = "scientific-benchmark.openfoam-airfoil-result.v1"
RESULT_NAME = "airfoil-incidence.json"
RESULT_PREFIX = "BENCHMARK_OPENFOAM "
ANGLES = (0, 6, 12)
CASE_NAMES = {angle: f"angle-{angle:02d}" for angle in ANGLES}
CASE_FILES = frozenset(
    {
        "0/U",
        "0/nut",
        "0/nuTilda",
        "0/p",
        "case-metadata.json",
        "constant/polyMesh/boundary.gz",
        "constant/polyMesh/cells.gz",
        "constant/polyMesh/faces.gz",
        "constant/polyMesh/neighbour.gz",
        "constant/polyMesh/owner.gz",
        "constant/polyMesh/points.gz",
        "constant/transportProperties",
        "constant/turbulenceProperties",
        "system/controlDict",
        "system/decomposeParDict",
        "system/fvSchemes",
        "system/fvSolution",
    }
)


@dataclass(frozen=True, slots=True)
class ForceCoefficientSummary:
    """Terminal and tail-averaged force coefficients for one incidence angle."""

    angle_degrees: int
    sample_count: int
    final_time: float
    final_cd: float
    final_cl: float
    tail_mean_cd: float
    tail_mean_cl: float
    tail_std_cd: float
    tail_std_cl: float
    coefficient_file: str


def validate_airfoil_bundle(bundle: MaterializedInputBundle) -> None:
    """Require exactly three complete, digest-bound OpenFOAM case trees."""

    paths = {item.path for item in bundle.manifest.files}
    expected = {
        f"{case_name}/{relative}"
        for case_name in CASE_NAMES.values()
        for relative in CASE_FILES
    }
    if paths != expected:
        missing = sorted(expected - paths)
        unexpected = sorted(paths - expected)
        raise InputBundleError(
            f"OpenFOAM airfoil bundle paths differ; missing={missing}, unexpected={unexpected}"
        )
    if bundle.manifest.entrypoint != "angle-00/system/controlDict":
        raise InputBundleError(
            "OpenFOAM airfoil entrypoint is not the zero-angle control"
        )
    for angle, case_name in CASE_NAMES.items():
        metadata_path = bundle.root / case_name / "case-metadata.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        expected_metadata = {
            "angle_degrees": angle,
            "schema_version": "scientific-benchmark.openfoam-airfoil-case.v1",
            "speed_metres_per_second": 26.0,
        }
        if metadata != expected_metadata:
            raise InputBundleError(f"OpenFOAM metadata differs for {case_name}")


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
    """Copy immutable OpenFOAM cases into a new execution-specific directory."""

    validate_airfoil_bundle(bundle)
    token = _safe_execution_id(execution_id)
    run_parent = run_parent.resolve()
    run_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = run_parent / token
    if destination.exists():
        raise InputBundleError("OpenFOAM execution working directory already exists")
    temporary = Path(tempfile.mkdtemp(prefix=f".{token}.", dir=run_parent))
    try:
        for item in bundle.manifest.files:
            source = bundle.root / PurePosixPath(item.path)
            target = temporary / PurePosixPath(item.path)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(source, target, follow_symlinks=False)
            os.chmod(target, 0o600)
        provenance = {
            "bundle_sha256": bundle.bundle_sha256,
            "case_names": [CASE_NAMES[angle] for angle in ANGLES],
            "schema_version": "scientific-benchmark.openfoam-airfoil-run.v1",
        }
        (temporary / "input-provenance.json").write_text(
            json.dumps(provenance, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary / "input-provenance.json", 0o600)
        os.replace(temporary, destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return destination


def _coefficient_file(case_root: Path) -> Path:
    matches = [
        path
        for path in case_root.glob("postProcessing/forceCoeffs/*/coefficient.dat")
        if path.is_file() and not path.is_symlink()
    ]
    if len(matches) != 1:
        raise ValueError(
            f"expected one OpenFOAM coefficient file below {case_root}, found {len(matches)}"
        )
    if matches[0].stat().st_size > 4 * 1024 * 1024:
        raise ValueError("OpenFOAM coefficient file exceeds the parser bound")
    return matches[0]


def parse_force_coefficients(
    case_root: Path, angle_degrees: int
) -> ForceCoefficientSummary:
    """Parse one complete OpenFOAM coefficient series with named columns."""

    coefficient_path = _coefficient_file(case_root)
    header: list[str] | None = None
    rows: list[dict[str, float]] = []
    for raw_line in coefficient_path.read_text(
        encoding="utf-8", errors="strict"
    ).splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            candidate = line.removeprefix("#").strip().split()
            if {"Time", "Cd", "Cl"}.issubset(candidate):
                header = candidate
            continue
        if header is None:
            raise ValueError(
                "OpenFOAM coefficient data appeared before its named header"
            )
        fields = line.split()
        if len(fields) != len(header):
            raise ValueError("OpenFOAM coefficient row width differs from its header")
        try:
            row = {
                name: float(value) for name, value in zip(header, fields, strict=True)
            }
        except ValueError as error:
            raise ValueError("OpenFOAM coefficient row is not numeric") from error
        if not all(math.isfinite(value) for value in row.values()):
            raise ValueError("OpenFOAM coefficient row contains a non-finite value")
        rows.append(row)
    if len(rows) < 10:
        raise ValueError("OpenFOAM coefficient series has fewer than ten samples")
    if rows[-1]["Time"] < 500:
        raise ValueError(
            "OpenFOAM coefficient series did not reach the configured end time"
        )
    tail = rows[-10:]
    cd_values = [row["Cd"] for row in tail]
    cl_values = [row["Cl"] for row in tail]
    return ForceCoefficientSummary(
        angle_degrees=angle_degrees,
        sample_count=len(rows),
        final_time=rows[-1]["Time"],
        final_cd=rows[-1]["Cd"],
        final_cl=rows[-1]["Cl"],
        tail_mean_cd=statistics.fmean(cd_values),
        tail_mean_cl=statistics.fmean(cl_values),
        tail_std_cd=statistics.pstdev(cd_values),
        tail_std_cl=statistics.pstdev(cl_values),
        coefficient_file=coefficient_path.relative_to(case_root).as_posix(),
    )


def _result_document(
    run_root: Path, bundle: MaterializedInputBundle
) -> dict[str, object]:
    summaries = [
        parse_force_coefficients(run_root / CASE_NAMES[angle], angle)
        for angle in ANGLES
    ]
    by_angle = {summary.angle_degrees: summary for summary in summaries}
    zero = by_angle[0]
    six = by_angle[6]
    twelve = by_angle[12]
    if six.tail_mean_cl - zero.tail_mean_cl <= 0.01:
        raise ValueError(
            "six-degree lift increase is not distinguishable from zero degrees"
        )
    if six.tail_mean_cl - twelve.tail_mean_cl <= 0.01:
        raise ValueError(
            "the twelve-degree case did not show the expected post-peak lift drop"
        )
    if twelve.tail_mean_cl - zero.tail_mean_cl <= 0.01:
        raise ValueError(
            "twelve-degree lift remains indistinguishable from zero degrees"
        )
    if twelve.tail_mean_cd - six.tail_mean_cd <= 0.001:
        raise ValueError(
            "the twelve-degree case did not show the expected drag penalty"
        )
    return {
        "cases": [asdict(summary) for summary in summaries],
        "input_bundle_sha256": bundle.bundle_sha256,
        "metrics": {
            "cd_change_0_to_12": twelve.tail_mean_cd - zero.tail_mean_cd,
            "drag_increase_6_to_12": twelve.tail_mean_cd - six.tail_mean_cd,
            "cl_change_0_to_12": twelve.tail_mean_cl - zero.tail_mean_cl,
            "lift_drop_6_to_12": six.tail_mean_cl - twelve.tail_mean_cl,
            "peak_lift_angle_degrees": 6,
        },
        "schema_version": RESULT_FILE_SCHEMA,
    }


def write_incidence_result(
    run_root: Path,
    destination: Path,
    bundle: MaterializedInputBundle,
) -> dict[str, object]:
    """Parse all cases and atomically write the closed incidence result."""

    validate_airfoil_bundle(bundle)
    document = _result_document(run_root, bundle)
    payload = json.dumps(document, indent=2, sort_keys=True) + "\n"
    if destination.exists():
        raise ValueError("OpenFOAM result destination already exists")
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return document


def validate_result_document(
    run_root: Path,
    result_path: Path,
    bundle: MaterializedInputBundle,
) -> dict[str, object]:
    """Require the result document to exactly match all coefficient files."""

    if result_path.is_symlink() or not result_path.is_file():
        raise ValueError("OpenFOAM result document is missing or unsafe")
    observed = json.loads(result_path.read_text(encoding="utf-8", errors="strict"))
    expected = _result_document(run_root, bundle)
    if observed != expected:
        raise ValueError(
            "OpenFOAM result document does not match the coefficient series"
        )
    return expected


def result_summary_line(document: dict[str, object]) -> str:
    """Render the closed terminal summary consumed by JARVIS progress."""

    metrics = document.get("metrics")
    if not isinstance(metrics, dict):
        raise ValueError("OpenFOAM result metrics are missing")
    return (
        f"{RESULT_PREFIX}schema={RESULT_FILE_SCHEMA} angles=0,6,12 "
        f"peak_lift_angle={metrics['peak_lift_angle_degrees']} "
        f"lift_drop_6_to_12={metrics['lift_drop_6_to_12']:.8g} "
        f"drag_increase_6_to_12={metrics['drag_increase_6_to_12']:.8g}"
    )
