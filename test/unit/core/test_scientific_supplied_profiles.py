"""Contracts for maintained OpenFOAM, WRF, and LBM-CFD supplied studies."""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import sys
import tarfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from jarvis_cd.artifacts import schema as artifact_schema
from jarvis_cd.core.config import Jarvis

from jarvis_cd.input_bundle import (
    INPUT_BUNDLE_MANIFEST_NAME,
    INPUT_BUNDLE_SCHEMA_VERSION,
    InputBundleError,
    extract_input_bundle,
)

BUILTIN_ROOT = Path(__file__).resolve().parents[3] / "builtin"
REPOSITORY_ROOT = BUILTIN_ROOT.parent
sys.path.insert(0, str(BUILTIN_ROOT))

from builtin.lbm_cfd.contract import (  # pyright: ignore[reportMissingImports]  # noqa: E402
    DIMENSIONS,
    EXPECTED_FILES as LBM_FILES,
    FINAL_STEP,
    LATTICES,
    RESULT_NAME as LBM_RESULT_NAME,
    RESULT_SCHEMA as LBM_RESULT_SCHEMA,
    materialize_run as materialize_lbm,
    validate_result_document as validate_lbm_result,
    write_lbm_result,
)
from builtin.lbm_cfd.artifacts import (  # pyright: ignore[reportMissingImports]  # noqa: E402
    adapter_from_package as lbm_artifact_adapter,
)
from builtin.lbm_cfd.pkg import LbmCfd  # pyright: ignore[reportMissingImports]  # noqa: E402
from builtin.openfoam.artifacts import (  # pyright: ignore[reportMissingImports]  # noqa: E402
    adapter_from_package as openfoam_artifact_adapter,
)
from builtin.openfoam.contract import (  # pyright: ignore[reportMissingImports]  # noqa: E402
    CASE_FILES,
    CASE_NAMES,
    RESULT_FILE_SCHEMA,
    RESULT_NAME as OPENFOAM_RESULT_NAME,
    materialize_run as materialize_openfoam,
    validate_result_document as validate_openfoam_result,
    write_incidence_result,
)
from builtin.openfoam.pkg import Openfoam  # pyright: ignore[reportMissingImports]  # noqa: E402
from builtin.wrf.contract import (  # pyright: ignore[reportMissingImports]  # noqa: E402
    FORMULATIONS,
    RESULT_NAME as WRF_RESULT_NAME,
    RESULT_SCHEMA as WRF_RESULT_SCHEMA,
    materialize_run as materialize_wrf,
    prepare_case,
    validate_result_document as validate_wrf_result,
    write_result as write_wrf_result,
)
from builtin.wrf.artifacts import (  # pyright: ignore[reportMissingImports]  # noqa: E402
    adapter_from_package as wrf_artifact_adapter,
)
from builtin.wrf.pkg import Wrf  # pyright: ignore[reportMissingImports]  # noqa: E402


def _write_bundle(
    destination: Path,
    files: dict[str, tuple[str, bytes]],
    *,
    entrypoint: str,
) -> Path:
    manifest = {
        "schema_version": INPUT_BUNDLE_SCHEMA_VERSION,
        "entrypoint": entrypoint,
        "files": [
            {
                "path": name,
                "role": role,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
            }
            for name, (role, payload) in sorted(files.items())
        ],
    }
    encoded = json.dumps(manifest, sort_keys=True).encode("utf-8")
    with tarfile.open(destination, mode="w") as archive:
        info = tarfile.TarInfo(INPUT_BUNDLE_MANIFEST_NAME)
        info.size = len(encoded)
        archive.addfile(info, io.BytesIO(encoded))
        for name, (_role, payload) in sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return destination


def _openfoam_bundle(path: Path) -> Path:
    files: dict[str, tuple[str, bytes]] = {}
    for angle, case_name in CASE_NAMES.items():
        for relative in CASE_FILES:
            name = f"{case_name}/{relative}"
            if relative == "case-metadata.json":
                payload = json.dumps(
                    {
                        "angle_degrees": angle,
                        "schema_version": "scientific-benchmark.openfoam-airfoil-case.v1",
                        "speed_metres_per_second": 26.0,
                    }
                ).encode("utf-8")
                role = "case_metadata"
            else:
                payload = f"OpenFOAM fixture {name}\n".encode()
                role = "openfoam_case"
            files[name] = (role, payload)
    return _write_bundle(
        path,
        files,
        entrypoint="angle-00/system/controlDict",
    )


def _write_coefficients(case_root: Path, *, cd: float, cl: float) -> None:
    target = case_root / "postProcessing" / "forceCoeffs" / "0" / "coefficient.dat"
    target.parent.mkdir(parents=True)
    rows = ["# Time Cd Cs Cl"]
    for index in range(1, 51):
        perturbation = (index % 3 - 1) * 0.0001
        rows.append(f"{index * 10} {cd + perturbation} 0 {cl + perturbation}")
    target.write_text("\n".join(rows) + "\n", encoding="utf-8")


def _wrf_bundle(path: Path) -> Path:
    assignments = {
        "run_days": "0",
        "run_hours": "1",
        "end_day": "1",
        "end_hour": "1",
        "history_interval": "60",
        "e_we": "81",
        "e_sn": "81",
        "isftcflx": "1",
    }
    namelist = (
        "&domains\n"
        + "".join(f" {name:<35} = {value},\n" for name, value in assignments.items())
        + "/\n"
    )
    return _write_bundle(
        path,
        {
            "input_sounding": ("atmospheric_sounding", b"1000 300 10\n"),
            "namelist.input": ("wrf_namelist", namelist.encode()),
        },
        entrypoint="namelist.input",
    )


def _fake_wrf_prefix(root: Path) -> Path:
    prefix = root / "wrf"
    (prefix / "main").mkdir(parents=True)
    (prefix / "run").mkdir()
    for name in ("ideal.exe", "wrf.exe"):
        (prefix / "main" / name).write_text("executable\n", encoding="utf-8")
    (prefix / "run" / "LANDUSE.TBL").write_text("table\n", encoding="utf-8")
    return prefix


def _write_wrf_cdl(path: Path, *, scale: float) -> None:
    path.write_text(
        f"""netcdf wrfout {{
dimensions:
 Time = UNLIMITED ; // (2 currently)
data:
 U10 = 1, 2, 3, 4, {scale}, {2 * scale}, {3 * scale}, {4 * scale} ;
 V10 = 0, 0, 0, 0, 0, 0, 0, 0 ;
 PSFC = 100000, 99900, 99800, 99700, 99000, 98900, 98800, 98700 ;
}}
""",
        encoding="utf-8",
    )


def _lbm_bundle(path: Path) -> Path:
    roles = {
        "COPYING": "license",
        "Makefile": "build_recipe",
        "README.md": "documentation",
        "include/lbm_mpi.hpp": "application_source",
        "include/paraview_sim.hpp": "application_source",
        "src/main.cpp": "application_source",
    }
    assert set(roles) == LBM_FILES
    return _write_bundle(
        path,
        {name: (role, f"fixture {name}\n".encode()) for name, role in roles.items()},
        entrypoint="Makefile",
    )


def _write_vorticity(path: Path, *, scale: float) -> None:
    count = math.prod(DIMENSIONS)
    values = " ".join(str(scale * ((index % 11) - 5)) for index in range(count))
    extent = f"0 {DIMENSIONS[0] - 1} 0 {DIMENSIONS[1] - 1} 0 {DIMENSIONS[2] - 1}"
    path.parent.mkdir(parents=True)
    path.write_text(
        '<?xml version="1.0"?>\n<VTKFile type="StructuredGrid">\n'
        f'<StructuredGrid WholeExtent="{extent}"><Piece><PointData>'
        f'<DataArray type="Float32" Name="vorticity" format="ascii">{values}'
        "</DataArray></PointData></Piece></StructuredGrid></VTKFile>\n",
        encoding="utf-8",
    )


def _permit_windows_cluster_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep filesystem-backed adapter tests portable; Ares covers POSIX paths."""

    if os.name == "nt":
        monkeypatch.setattr(
            artifact_schema, "_validate_cluster_path", lambda _value: None
        )


def test_maintained_profiles_are_agent_visible_and_keep_legacy_controls() -> None:
    openfoam = object.__new__(Openfoam)
    openfoam.env = {}
    openfoam.mod_env = {}
    openfoam_menu = {item["name"]: item for item in openfoam._configure_menu()}
    assert {"input_bundle", "script_location", "script"}.issubset(openfoam_menu)
    assert openfoam_menu["input_bundle"]["input_binding"]["kind"] == "local_file"
    openfoam_contract = openfoam._deployment_contract()
    assert openfoam_contract.package == "builtin.openfoam"
    assert {profile.name for profile in openfoam_contract.execution_profiles} == {
        "legacy_case_script",
        "supplied_airfoil_incidence",
    }
    assert (
        openfoam_contract.configuration_rules[0].requires[0].parameter
        == "script_location"
    )

    wrf = object.__new__(Wrf)
    wrf.env = {}
    wrf.mod_env = {}
    wrf_menu = {item["name"]: item for item in wrf._configure_menu()}
    assert {"input_bundle", "wrf_prefix", "wrf_location", "engine"}.issubset(wrf_menu)
    assert wrf_menu["input_bundle"]["input_binding"]["kind"] == "local_file"
    wrf_contract = wrf._deployment_contract()
    assert wrf_contract.package == "builtin.wrf"
    assert {profile.name for profile in wrf_contract.execution_profiles} == {
        "legacy_wrf_location",
        "supplied_surface_exchange_comparison",
    }
    assert wrf_contract.configuration_rules[0].requires[0].parameter == "wrf_location"
    assert wrf_contract.configuration_rules[1].when[0].operator == "is_not_empty"

    lbm = object.__new__(LbmCfd)
    lbm.env = {}
    lbm.mod_env = {}
    lbm_menu = {item["name"]: item for item in lbm._configure_menu()}
    assert lbm_menu["input_bundle"]["input_binding"]["structure"] == "regular_file"
    assert lbm._deployment_contract().package == "builtin.lbm_cfd"


def test_maintained_profiles_are_discoverable_by_exact_package_name(
    tmp_path: Path,
) -> None:
    """The same package identifiers advertised to agents resolve from the catalog."""

    previous = Jarvis._instance
    Jarvis._instance = None
    try:
        jarvis = Jarvis(jarvis_root=str(tmp_path / "jarvis"))
        assert jarvis.find_package("openfoam") == "builtin.openfoam"
        assert jarvis.find_package("wrf") == "builtin.wrf"
        assert jarvis.find_package("lbm_cfd") == "builtin.lbm_cfd"
    finally:
        Jarvis._instance = previous


def test_release_manifest_includes_complete_maintained_profile_modules() -> None:
    """The distribution source list must not collapse profiles to legacy pkg.py."""

    manifest = (REPOSITORY_ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    for package in ("lbm_cfd", "openfoam", "wrf"):
        assert f"recursive-include builtin/builtin/{package} *.md *.py" in manifest


def test_openfoam_supplied_profile_materializes_and_validates_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _permit_windows_cluster_paths(monkeypatch)
    bundle = extract_input_bundle(
        _openfoam_bundle(tmp_path / "openfoam.tar"), tmp_path / "in"
    )
    run = materialize_openfoam(bundle, tmp_path / "runs", "jarvis_openfoam_test")
    for angle, cd, cl in ((0, 0.02, 0.001), (6, 0.04, 1.0), (12, 0.08, 0.5)):
        _write_coefficients(run / CASE_NAMES[angle], cd=cd, cl=cl)

    document = write_incidence_result(run, run / OPENFOAM_RESULT_NAME, bundle)

    assert document["schema_version"] == RESULT_FILE_SCHEMA
    assert validate_openfoam_result(run, run / OPENFOAM_RESULT_NAME, bundle) == document
    assert document["metrics"]["peak_lift_angle_degrees"] == 6
    adapter = openfoam_artifact_adapter(
        {
            "pkg_type": "builtin.openfoam",
            "input_bundle": str(tmp_path / "openfoam.tar"),
            "out": str(run),
            "shared_dir": str(tmp_path),
        }
    )
    assert adapter is not None
    assert len(adapter.finalize_artifacts_for_exit(0)) == 5
    with pytest.raises(InputBundleError, match="already exists"):
        materialize_openfoam(bundle, tmp_path / "runs", "jarvis_openfoam_test")


def test_wrf_supplied_profile_materializes_both_formulations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _permit_windows_cluster_paths(monkeypatch)
    bundle = extract_input_bundle(_wrf_bundle(tmp_path / "wrf.tar"), tmp_path / "in")
    run = materialize_wrf(bundle, tmp_path / "runs", "jarvis_wrf_test")
    prefix = _fake_wrf_prefix(tmp_path)
    for index, (name, option) in enumerate(FORMULATIONS, start=1):
        case = prepare_case(run, bundle, prefix, name=name, isftcflx=option)
        (case / "wrfout_d01_test").write_bytes(bytes([index]) * 1024)
        _write_wrf_cdl(case / "surface-diagnostics.cdl", scale=float(index))

    document = write_wrf_result(run, run / WRF_RESULT_NAME, bundle)

    assert document["schema_version"] == WRF_RESULT_SCHEMA
    assert validate_wrf_result(run, run / WRF_RESULT_NAME, bundle) == document
    assert [case["formulation"] for case in document["cases"]] == [
        name for name, _option in FORMULATIONS
    ]
    adapter = wrf_artifact_adapter(
        {
            "pkg_type": "builtin.wrf",
            "input_bundle": str(tmp_path / "wrf.tar"),
            "out": str(run),
            "shared_dir": str(tmp_path),
        }
    )
    assert adapter is not None
    assert len(adapter.finalize_artifacts_for_exit(0)) == 6


def test_lbm_supplied_profile_validates_three_distinct_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _permit_windows_cluster_paths(monkeypatch)
    bundle = extract_input_bundle(_lbm_bundle(tmp_path / "lbm.tar"), tmp_path / "in")
    run = materialize_lbm(bundle, tmp_path / "runs", "jarvis_lbm_test")
    for index, lattice in enumerate(LATTICES, start=1):
        _write_vorticity(
            run / lattice / f"simulation_state_t{FINAL_STEP:05d}.vts",
            scale=float(index),
        )

    document = write_lbm_result(run, run / LBM_RESULT_NAME, bundle)

    assert document["schema_version"] == LBM_RESULT_SCHEMA
    assert validate_lbm_result(run, run / LBM_RESULT_NAME, bundle) == document
    assert [case["lattice"] for case in document["cases"]] == list(LATTICES)
    adapter = lbm_artifact_adapter(
        {
            "pkg_type": "builtin.lbm_cfd",
            "input_bundle": str(tmp_path / "lbm.tar"),
            "out": str(run),
            "shared_dir": str(tmp_path),
        }
    )
    assert adapter is not None
    assert len(adapter.finalize_artifacts_for_exit(0)) == 5


@pytest.mark.parametrize(
    ("factory", "package"),
    (
        (openfoam_artifact_adapter, "builtin.openfoam"),
        (wrf_artifact_adapter, "builtin.wrf"),
        (lbm_artifact_adapter, "builtin.lbm_cfd"),
    ),
)
def test_failed_supplied_profiles_do_not_finalize_products(
    tmp_path: Path,
    factory: Callable[[dict[str, object]], Any],
    package: str,
) -> None:
    adapter = factory(
        {
            "pkg_type": package,
            "input_bundle": "supplied.tar",
            "out": str(tmp_path / "missing"),
            "shared_dir": str(tmp_path),
        }
    )
    assert adapter is not None
    assert adapter.finalize_artifacts_for_exit(1) == []
