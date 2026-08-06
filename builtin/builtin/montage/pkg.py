"""Deploy bounded native or containerized Montage mosaic workflows."""

from __future__ import annotations

import json
import math
import os
import re
import shlex
import shutil
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from jarvis_cd.core.pkg import Application
from jarvis_cd.deployment import (
    ConfigurationCondition,
    ConfigurationInputBinding,
    ConfigurationRule,
    ExecutionProfile,
    PackageDeploymentContract,
    ProviderResolution,
    ReadinessContract,
    RuntimeRequirement,
    RuntimeStatus,
    probe_program,
)
from jarvis_cd.input_bundle import (
    MaterializedInputBundle,
    extract_input_bundle,
    stage_input_bundle,
)
from jarvis_cd.runtime_callback import RuntimePhaseLineCallback
from jarvis_cd.shell import Exec, LocalExecInfo, PsshExecInfo
from jarvis_cd.shell.process import Mkdir, Rm

_BANDS = ("j", "h", "k")
_RESULT_SCHEMA = "jarvis.montage-result.v1"
_RESULT_NAME = "montage-result.json"
_COMPOSITE_NAME = "montage-jhk.png"
_STATUS_FIELD = re.compile(r'(\w+)=(?:"([^"]*)"|([^,\]\s]+))')


class Montage(Application):
    """Run Montage with offline supplied inputs or its legacy archive profile."""

    def _init(self) -> None:
        pass

    def _configure_menu(self) -> list[dict[str, Any]]:
        menu: list[dict[str, Any]] = [
            {
                "name": "nprocs",
                "msg": "Number of MPI processes reserved for the workflow",
                "type": int,
                "default": 1,
            },
            {
                "name": "ppn",
                "msg": "Processes per node reserved for the workflow",
                "type": int,
                "default": 1,
            },
            {
                "name": "region",
                "msg": "Printable astronomical target or region label",
                "type": str,
                "default": "M17",
            },
            {
                "name": "band",
                "msg": "2MASS band for the legacy archive profile (j, h, or k)",
                "type": str,
                "default": "j",
            },
            {
                "name": "size",
                "msg": "Square legacy archive region size in degrees",
                "type": float,
                "default": 0.2,
            },
            {
                "name": "scratch_dir",
                "msg": "Legacy container scratch directory",
                "type": str,
                "default": "${HOME}/montage-scratch",
            },
            {
                "name": "parallel_scratch_root",
                "msg": "Legacy container per-replicate scratch root",
                "type": str,
                "default": "/dev/shm",
            },
            {
                "name": "out",
                "msg": (
                    "Output directory. Relative paths resolve below the package "
                    "shared directory; '.' is the durable execution-owned root."
                ),
                "type": str,
                "default": ".",
            },
            {
                "name": "mosaic_replicates",
                "msg": "Legacy container mosaic replicate count",
                "type": int,
                "default": 1,
            },
            {
                "name": "parallel_reps",
                "msg": "Legacy container per-host replicate concurrency",
                "type": int,
                "default": 1,
            },
            {
                "name": "omp_threads",
                "msg": "Legacy container OpenMP threads per replicate; zero leaves it unset",
                "type": int,
                "default": 0,
            },
            {
                "name": "base_image",
                "msg": "Base image used only by the legacy container profile",
                "type": str,
                "default": "sci-hpc-base",
            },
        ]
        for band in _BANDS:
            menu.append(
                {
                    "name": f"{band}_bundle",
                    "msg": (
                        f"Optional digest-verified {band.upper()}-band FITS bundle. "
                        "The manifest entrypoint is the mosaic header and files "
                        "with role fits_source form the offline source collection. "
                        "J, H, and K bundles must be supplied together."
                    ),
                    "type": str,
                    "default": "",
                    "input_binding": ConfigurationInputBinding(
                        kind="local_file",
                        structure="regular_file",
                    ).to_dict(),
                }
            )
        return menu

    def _deployment_contract(self) -> PackageDeploymentContract:
        """Describe the offline and legacy Montage execution profiles."""
        probes = tuple(
            probe_program(
                program,
                environment=self._deployment_environment(),
                arguments=(),
                accepted_return_codes=(0, 1),
            )
            for program in ("mExec", "mExamine", "mViewer")
        )
        usable = all(probe.status.usable is True for probe in probes)
        unavailable = next(
            (probe.status for probe in probes if probe.status.usable is False),
            None,
        )
        status = (
            RuntimeStatus("ready", "all_montage_commands_available")
            if usable
            else unavailable
            or RuntimeStatus("unknown", "montage_commands_not_fully_probed")
        )
        runtime = RuntimeRequirement(
            requirement_id="montage",
            description="Montage 6 FITS mosaic, statistics, and rendering commands",
            required_capabilities=(
                "fits_mosaic",
                "image_statistics",
                "three_band_composite",
            ),
            available_capabilities=(
                ("fits_mosaic", "image_statistics", "three_band_composite")
                if usable
                else ()
            ),
            status=status,
            provider_resolutions=(
                ProviderResolution(
                    provider="spack",
                    query_kind="spec",
                    query_value="montage@6.0",
                ),
            ),
        )
        completed = ReadinessContract(
            mechanism="process_exit",
            condition="successful_exit",
        )
        return PackageDeploymentContract(
            package="builtin.montage",
            execution_profiles=(
                ExecutionProfile(
                    name="legacy_archive",
                    execution_kind="batch",
                    when=tuple(
                        ConfigurationCondition(f"{band}_bundle", "is_empty")
                        for band in _BANDS
                    ),
                    runtime_requirements=("montage",),
                    readiness=completed,
                    description=(
                        "Legacy single-band profile that may acquire archive data at "
                        "runtime and therefore is not suitable for offline replay."
                    ),
                ),
                ExecutionProfile(
                    name="offline_three_band",
                    execution_kind="batch",
                    when=tuple(
                        ConfigurationCondition(f"{band}_bundle", "is_not_empty")
                        for band in _BANDS
                    ),
                    runtime_requirements=("montage",),
                    readiness=completed,
                    description=(
                        "Network-independent J/H/K workflow using three immutable "
                        "FITS collections and one fixed mosaic header per bundle."
                    ),
                ),
            ),
            runtime_requirements=(runtime,),
            configuration_rules=tuple(
                ConfigurationRule(
                    when=(ConfigurationCondition(f"{band}_bundle", "is_not_empty"),),
                    requires=tuple(
                        ConfigurationCondition(f"{other}_bundle", "is_not_empty")
                        for other in _BANDS
                        if other != band
                    ),
                    description="Offline Montage requires all three J, H, and K bundles.",
                )
                for band in _BANDS
            ),
        )

    def _build_phase(self) -> tuple[str, str] | None:  # pyright: ignore[reportIncompatibleMethodOverride]
        if self.config.get("deploy_mode") != "container":
            return None
        import base64

        package_dir = self.pkg_dir
        if not isinstance(package_dir, str) or not package_dir:
            raise RuntimeError("Montage container build requires its package directory")
        run_mosaic_path = os.path.join(package_dir, "run_mosaic.sh")
        with open(run_mosaic_path, "rb") as stream:
            run_mosaic_b64 = base64.b64encode(stream.read()).decode("ascii")
        content = self._read_build_script(
            "build.sh",
            {
                "BASE_IMAGE": self.config.get("base_image", "sci-hpc-base"),
                "RUN_MOSAIC_B64": run_mosaic_b64,
            },
        )
        return content, "default"

    def _build_deploy_phase(self) -> tuple[str, str] | None:  # pyright: ignore[reportIncompatibleMethodOverride]
        if self.config.get("deploy_mode") != "container":
            return None
        content = self._read_dockerfile(
            "Dockerfile.deploy",
            {
                "BUILD_IMAGE": self.build_image_name(),
                "DEPLOY_BASE": "ubuntu:24.04",
            },
        )
        return content, "default"

    def _configure(self, **kwargs: Any) -> None:
        """Validate profile selection and prepare the package-owned output."""
        super()._configure(**kwargs)
        self._validate_configuration()
        self.setenv("MONTAGE_REGION", str(self.config.get("region", "M17")))
        self.setenv("MONTAGE_BAND", str(self.config.get("band", "j")).upper())
        self.setenv("MONTAGE_SIZE", str(self.config.get("size", 0.2)))
        self.setenv("MONTAGE_OUT", self._output_dir())
        self.setenv(
            "MONTAGE_SCRATCH_DIR",
            str(self.config.get("scratch_dir", "${HOME}/montage-scratch")),
        )
        if self.config.get("deploy_mode", "default") == "default":
            self._ensure_output_dir()

    def _configured_bundles(self) -> dict[str, str]:
        """Return configured offline bundle paths after type validation."""
        configured: dict[str, str] = {}
        for band in _BANDS:
            value = self.config.get(f"{band}_bundle")
            if value in (None, ""):
                continue
            if not isinstance(value, str):
                raise TypeError(f"{band}_bundle must be a path string")
            configured[band] = value
        return configured

    def _validate_configuration(self) -> None:
        """Reject ambiguous, unsafe, or unsupported profile combinations."""
        bundles = self._configured_bundles()
        if bundles and set(bundles) != set(_BANDS):
            raise ValueError("offline Montage requires all three J, H, and K bundles")
        if bundles and self.config.get("deploy_mode", "default") != "default":
            raise ValueError("offline Montage bundles support native execution only")
        region = self.config.get("region", "M17")
        if (
            not isinstance(region, str)
            or not region.strip()
            or len(region) > 128
            or any(ord(character) < 32 for character in region)
        ):
            raise ValueError("region must be a bounded printable label")
        if not bundles:
            band = str(self.config.get("band", "j")).casefold()
            if band not in _BANDS:
                raise ValueError("band must be j, h, or k")
            size = self.config.get("size", 0.2)
            if (
                isinstance(size, bool)
                or not isinstance(size, (int, float))
                or size <= 0
            ):
                raise ValueError("size must be positive")

    def _output_dir(self) -> str:
        """Resolve the durable output directory under package authority."""
        return str(self.resolve_shared_path(self.config.get("out"), field="out"))

    def _node_exec_info(self, **kwargs: Any) -> LocalExecInfo | PsshExecInfo:
        """Use local execution for one host and PSSH only for legacy fan-out."""
        hostfile = self.hostfile
        if hostfile is None or hostfile.is_local():
            return LocalExecInfo(**kwargs)
        return PsshExecInfo(hostfile=hostfile, **kwargs)

    def _ensure_output_dir(self) -> None:
        """Create the output on each participating host and propagate failures."""
        output = Path(self._output_dir())
        if self.hostfile is None or self.hostfile.is_local():
            output.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not output.is_dir():
                raise RuntimeError(
                    f"Montage output directory creation failed: {output}"
                )
            return
        result = Mkdir(str(output), self._node_exec_info(env=self.env)).run()
        self._require_success(result, "Montage output directory creation")

    @staticmethod
    def _require_success(result: Any, label: str) -> None:
        """Raise when any participating host reports command failure."""
        failures = {host: code for host, code in result.exit_code.items() if code != 0}
        if failures:
            raise RuntimeError(f"{label} failed: {failures}")

    @staticmethod
    def _bundle_raw_directory(bundle: MaterializedInputBundle) -> PurePosixPath:
        """Require one nonempty, single-directory FITS source collection."""
        fits = tuple(
            PurePosixPath(item.path)
            for item in bundle.manifest.files
            if item.role == "fits_source"
        )
        if not fits:
            raise ValueError("Montage input bundle requires at least one fits_source")
        parents = {path.parent for path in fits}
        if len(parents) != 1:
            raise ValueError("Montage fits_source files must share one directory")
        entrypoint = next(
            (
                item
                for item in bundle.manifest.files
                if item.path == bundle.manifest.entrypoint
            ),
            None,
        )
        if entrypoint is None or entrypoint.role not in {
            "mosaic_header",
            "scientific_input",
        }:
            raise ValueError("Montage bundle entrypoint must be a mosaic_header")
        return parents.pop()

    def _prepare_offline_inputs(
        self,
    ) -> tuple[
        dict[str, tuple[MaterializedInputBundle, PurePosixPath]],
        dict[str, str],
    ]:
        """Extract and validate the exact J/H/K bundle bytes for one execution."""
        output = Path(self._output_dir())
        prepared: dict[str, tuple[MaterializedInputBundle, PurePosixPath]] = {}
        digests: dict[str, str] = {}
        common_header: bytes | None = None
        for band, configured in self._configured_bundles().items():
            bundle = extract_input_bundle(configured, output / "input-bundles")
            raw_relative = self._bundle_raw_directory(bundle)
            header_bytes = bundle.entrypoint.read_bytes()
            if common_header is None:
                common_header = header_bytes
            elif header_bytes != common_header:
                raise ValueError(
                    "Montage J, H, and K bundles must share one mosaic header"
                )
            prepared[band] = (bundle, raw_relative)
            digests[band] = bundle.bundle_sha256
        return prepared, digests

    @staticmethod
    def _short_scratch_parent() -> Path:
        """Select a writable real directory for Montage's bounded path workspace."""
        candidates = (Path("/dev/shm"), Path(tempfile.gettempdir()))
        for candidate in candidates:
            try:
                if (
                    candidate.is_dir()
                    and not candidate.is_symlink()
                    and os.access(candidate, os.W_OK | os.X_OK)
                ):
                    return candidate
            except OSError:
                continue
        raise RuntimeError("Montage requires a writable local scratch directory")

    @staticmethod
    def _publish_scratch_file(source: Path, destination: Path) -> None:
        """Atomically copy one regular scratch product into durable package storage."""
        if source.is_symlink() or not source.is_file():
            raise RuntimeError(f"Montage scratch product is missing: {source.name}")
        if destination.exists() or destination.is_symlink():
            raise RuntimeError(
                f"Montage durable product already exists: {destination.name}"
            )
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            dir=destination.parent,
        )
        temporary = Path(temporary_name)
        try:
            with (
                source.open("rb") as source_stream,
                os.fdopen(descriptor, "wb") as destination_stream,
            ):
                shutil.copyfileobj(source_stream, destination_stream)
                destination_stream.flush()
                os.fsync(destination_stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _parse_statistics(text: str, *, band: str, mosaic: str) -> dict[str, Any]:
        """Parse one successful structured mExamine status record."""
        records = [
            line.strip()
            for line in text.splitlines()
            if line.strip().startswith("[struct")
        ]
        if len(records) != 1:
            raise RuntimeError(
                f"Montage {band.upper()} statistics require one mExamine record"
            )
        fields = {
            name: quoted if quoted else unquoted
            for name, quoted, unquoted in _STATUS_FIELD.findall(records[0])
        }
        if fields.get("stat") != "OK":
            raise RuntimeError(f"Montage {band.upper()} mExamine did not succeed")
        try:
            npixel = int(fields["npixel"])
            nnull = int(fields["nnull"])
            mean_flux = float(fields["aveflux"])
            rms_flux = float(fields["rmsflux"])
        except (KeyError, ValueError) as error:
            raise RuntimeError("Montage mExamine omitted numeric statistics") from error
        if (
            npixel <= 0
            or nnull < 0
            or nnull >= npixel
            or not math.isfinite(mean_flux)
            or not math.isfinite(rms_flux)
            or rms_flux <= 0
        ):
            raise RuntimeError("Montage mExamine statistics are not usable")
        return {
            "band": band.upper(),
            "mean_flux": mean_flux,
            "mosaic": mosaic,
            "non_null_pixels": npixel - nnull,
            "null_pixels": nnull,
            "pixels": npixel,
            "rms_flux": rms_flux,
        }

    @staticmethod
    def _validate_fits(path: Path) -> None:
        """Require a bounded regular FITS product with the canonical card prefix."""
        valid = False
        if not path.is_symlink() and path.is_file():
            size = path.stat().st_size
            if 2880 <= size <= 512 * 1024 * 1024:
                with path.open("rb") as stream:
                    valid = stream.read(8) == b"SIMPLE  "
        if not valid:
            raise RuntimeError(
                f"Montage FITS product is missing or invalid: {path.name}"
            )

    @staticmethod
    def _validate_composite(path: Path) -> None:
        """Require a bounded regular PNG composite."""
        valid = False
        if not path.is_symlink() and path.is_file():
            size = path.stat().st_size
            if 1024 < size <= 128 * 1024 * 1024:
                with path.open("rb") as stream:
                    valid = stream.read(8) == b"\x89PNG\r\n\x1a\n"
        if not valid:
            raise RuntimeError("Montage three-band composite is missing or invalid")

    def _write_result(
        self,
        output: Path,
        bundle_digests: dict[str, str],
    ) -> dict[str, Any]:
        """Validate products and atomically write the general result document."""
        bands: list[dict[str, Any]] = []
        for band in _BANDS:
            mosaic = output / f"montage-{band}.fits"
            self._validate_fits(mosaic)
            statistics = output / f"montage-{band}-statistics.txt"
            if statistics.is_symlink() or not statistics.is_file():
                raise RuntimeError(f"Montage {band.upper()} statistics are missing")
            bands.append(
                self._parse_statistics(
                    statistics.read_text(encoding="utf-8", errors="strict"),
                    band=band,
                    mosaic=mosaic.name,
                )
            )
        composite = output / _COMPOSITE_NAME
        self._validate_composite(composite)
        document: dict[str, Any] = {
            "bands": bands,
            "composite": _COMPOSITE_NAME,
            "input_bundle_sha256": bundle_digests,
            "region": self.config.get("region", "M17"),
            "schema_version": _RESULT_SCHEMA,
        }
        destination = output / _RESULT_NAME
        if destination.exists() or destination.is_symlink():
            raise RuntimeError("Montage result destination already exists")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            dir=output,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(document, stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return document

    def _run_offline(self) -> None:
        """Run the replayable J/H/K mosaic and composite profile."""
        self._validate_configuration()
        self._ensure_output_dir()
        prepared, digests = self._prepare_offline_inputs()
        output = Path(self._output_dir())
        callback = self.runtime_line_callback()
        intermediate = (
            RuntimePhaseLineCallback(callback, terminal=False)
            if callback is not None
            else None
        )
        scratch_parent = self._short_scratch_parent()
        with tempfile.TemporaryDirectory(prefix="jm-", dir=scratch_parent) as name:
            scratch = Path(name)
            if len(str(scratch)) >= 128:
                raise RuntimeError(
                    "Montage local scratch path exceeds its safe execution bound"
                )
            for band in _BANDS:
                bundle, raw_relative = prepared[band]
                band_scratch = scratch / band
                band_scratch.mkdir(mode=0o700)
                staged = band_scratch / "staged"
                header = stage_input_bundle(bundle, staged)
                raw_argument = (PurePosixPath("staged") / raw_relative).as_posix()
                header_argument = header.relative_to(band_scratch).as_posix()
                if max(len(raw_argument), len(header_argument)) >= 96:
                    raise RuntimeError(
                        "Montage bundle paths exceed its safe execution bound"
                    )
                info = LocalExecInfo(
                    env=self.mod_env,
                    cwd=str(band_scratch),
                    timeout=1800,
                    line_callback=intermediate,
                )
                command = " ".join(
                    (
                        "mExec",
                        "-r",
                        shlex.quote(raw_argument),
                        "-f",
                        shlex.quote(header_argument),
                        "-o",
                        "mosaic.fits",
                        "2MASS",
                        band.upper(),
                        "workspace",
                    )
                )
                self._require_success(
                    Exec(command, info).run(), f"Montage {band.upper()} mosaic"
                )
                scratch_mosaic = band_scratch / "mosaic.fits"
                self._validate_fits(scratch_mosaic)
                self._require_success(
                    Exec("mExamine mosaic.fits > statistics.txt", info).run(),
                    f"Montage {band.upper()} statistics",
                )
                scratch_statistics = band_scratch / "statistics.txt"
                if scratch_statistics.is_symlink() or not scratch_statistics.is_file():
                    raise RuntimeError(f"Montage {band.upper()} statistics are missing")
                self._parse_statistics(
                    scratch_statistics.read_text(encoding="utf-8", errors="strict"),
                    band=band,
                    mosaic=f"montage-{band}.fits",
                )
                self._publish_scratch_file(
                    scratch_mosaic,
                    output / f"montage-{band}.fits",
                )
                self._publish_scratch_file(
                    scratch_statistics,
                    output / f"montage-{band}-statistics.txt",
                )

            composite = scratch / _COMPOSITE_NAME
            viewer = " ".join(
                (
                    "mViewer",
                    "-blue",
                    "j/mosaic.fits",
                    "-1s max gaussian-log",
                    "-green",
                    "h/mosaic.fits",
                    "-1s max gaussian-log",
                    "-red",
                    "k/mosaic.fits",
                    "-1s max gaussian-log",
                    "-out",
                    _COMPOSITE_NAME,
                )
            )
            viewer_info = LocalExecInfo(
                env=self.mod_env,
                cwd=str(scratch),
                timeout=1800,
                line_callback=intermediate,
            )
            self._require_success(
                Exec(viewer, viewer_info).run(), "Montage J/H/K composite"
            )
            self._validate_composite(composite)
            self._publish_scratch_file(composite, output / _COMPOSITE_NAME)
        self._write_result(output, digests)
        terminal = (
            RuntimePhaseLineCallback(callback, terminal=True)
            if callback is not None
            else None
        )
        final_info = LocalExecInfo(
            env=self.mod_env,
            cwd=str(output),
            timeout=30,
            line_callback=terminal,
        )
        summary = (
            f"JARVIS_MONTAGE_RESULT schema={_RESULT_SCHEMA} "
            f"region={self.config.get('region', 'M17')} bands=J,H,K "
            f"composite={_COMPOSITE_NAME}"
        )
        self._require_success(
            Exec(f"printf '%s\\n' {shlex.quote(summary)}", final_info).run(),
            "Montage result finalization",
        )

    def _run_legacy_native(self) -> None:
        """Run the network-dependent single-band profile under package storage."""
        self._ensure_output_dir()
        output = Path(self._output_dir())
        raw = output / "archive-input"
        header = output / "region.hdr"
        script = Path(str(self.pkg_dir)) / "run_mosaic.sh"
        command = " ".join(
            (
                "bash",
                shlex.quote(str(script)),
                shlex.quote(str(raw)),
                shlex.quote(str(header)),
                shlex.quote(str(output)),
            )
        )
        result = Exec(
            command,
            LocalExecInfo(
                env=self.mod_env,
                cwd=str(output),
                line_callback=self.runtime_line_callback(),
            ),
        ).run()
        self._require_success(result, "Montage legacy archive mosaic")

    def _run_legacy_container(self) -> None:
        """Preserve the existing container replication workflow."""
        self._ensure_output_dir()
        replicates = self._positive_integer("mosaic_replicates", 1)
        parallel = self._positive_integer("parallel_reps", 1)
        output = self._output_dir()
        if parallel <= 1:
            command = (
                "set -e; host_tag=$(hostname -s 2>/dev/null || echo localhost); "
                f"for i in $(seq 1 {replicates}); do "
                "rep=$(printf 'rep_%03d' \"$i\"); "
                f"/opt/run_mosaic.sh /opt/montage-bench/raw_images "
                f"/opt/montage-bench/region.hdr {shlex.quote(output)}/$rep; done"
            )
            info: LocalExecInfo | PsshExecInfo = LocalExecInfo(
                env=self.mod_env,
                container=self._container_engine,
                container_image=self.deploy_image_name(),
                shared_dir=self.shared_dir,
                private_dir=self.private_dir,
                line_callback=self.runtime_line_callback(),
            )
        else:
            scratch = str(
                self.config.get("parallel_scratch_root") or "/dev/shm"
            ).rstrip("/")
            raw_omp = self.config.get("omp_threads", 0)
            if isinstance(raw_omp, bool) or not isinstance(raw_omp, int) or raw_omp < 0:
                raise ValueError("omp_threads must be a non-negative integer")
            omp = raw_omp
            omp_export = f"export OMP_NUM_THREADS={omp}; " if omp > 0 else ""
            command = (
                f"set -e; {omp_export}OUT={shlex.quote(output)}; H=$(hostname -s); "
                f"for i in $(seq 1 {replicates}); do "
                'rep=$(printf \'rep_%03d-%s\' "$i" "$H"); '
                f"MONTAGE_SCRATCH_DIR={shlex.quote(scratch)}/montage-scratch-$rep "
                "/opt/run_mosaic.sh /opt/montage-bench/raw_images "
                '/opt/montage-bench/region.hdr "$OUT/$rep"; done'
            )
            info = PsshExecInfo(
                hostfile=self.hostfile,
                container=self._container_engine,
                container_image=self.deploy_image_name(),
                shared_dir=self.shared_dir,
                private_dir=self.private_dir,
                env=self.mod_env,
                line_callback=self.runtime_line_callback(),
            )
        self._require_success(Exec(command, info).run(), "Montage container workflow")

    def _positive_integer(self, name: str, default: int) -> int:
        """Return one positive integer configuration value."""
        value = self.config.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
        return value

    def start(self) -> None:
        """Run exactly one validated Montage profile and propagate failures."""
        self._validate_configuration()
        if self._configured_bundles():
            self._run_offline()
        elif self.config.get("deploy_mode") == "container":
            self._run_legacy_container()
        else:
            self._run_legacy_native()

    def stop(self) -> None:
        """Do nothing because every Montage profile is bounded batch work."""

    def clean(self) -> None:
        """Remove only the resolved package-owned output directory."""
        output = Path(self._output_dir())
        if output == Path(output.anchor):
            raise ValueError("refusing to clean a filesystem root as Montage output")
        result = Rm(
            str(output),
            self._node_exec_info(env=self.env),
            recursive=True,
        ).run()
        self._require_success(result, "Montage output cleanup")
