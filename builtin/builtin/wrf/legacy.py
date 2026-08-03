"""Legacy WRF launcher retained for compatibility."""

from __future__ import annotations

from typing import Any, cast

from jarvis_cd.core.pkg import Application
from jarvis_cd.shell import Exec, MpiExecInfo, PsshExecInfo
from jarvis_cd.shell.process import Rm


class WrfLegacy(Application):
    """Run WRF from an operator-provided installation directory."""

    def _init(self) -> None:
        """Initialize the stateless legacy launcher."""

    def _configure_menu(self) -> list[dict[str, Any]]:
        """Return the legacy WRF launch controls."""

        return [
            {
                "name": "nprocs",
                "msg": "Number of processes",
                "type": int,
                "default": 1,
            },
            {
                "name": "ppn",
                "msg": "The number of processes per node",
                "type": int,
                "default": None,
            },
            {
                "name": "wrf_location",
                "msg": "The location of wrf.exe",
                "type": str,
                "default": None,
            },
            {
                "name": "engine",
                "msg": "Engine to be used",
                "choices": ["bp5", "hermes"],
                "type": str,
                "default": "bp5",
            },
            {
                "name": "Execution_order",
                "msg": "Path where the bp5 will be stored",
                "type": str,
                "default": None,
            },
            {
                "name": "db_path",
                "msg": "Path where the DB will be stored",
                "type": str,
                "default": "benchmark_metadata.db",
            },
        ]

    def _configure(self, **kwargs: Any) -> None:
        """Persist configuration and install the selected ADIOS2 template."""

        Application._configure(self, **kwargs)
        config = cast(dict[str, Any], self.config)
        engine = config.get("engine")
        wrf_location = config.get("wrf_location")
        if not isinstance(engine, str):
            raise ValueError("engine must be a string")
        if not isinstance(wrf_location, str) or not wrf_location:
            raise ValueError("wrf_location is required")
        if engine.lower() == "bp5":
            self.copy_template_file(
                f"{self.pkg_dir}/config/adios2.xml",
                f"{wrf_location}/adios2.xml",
            )
            return
        if engine.lower() in {"hermes", "hermes_derived"}:
            self.copy_template_file(
                f"{self.pkg_dir}/config/hermes.xml",
                f"{wrf_location}/adios2.xml",
                replacements={
                    "ppn": config["ppn"],
                    "db_path": config["db_path"],
                    "Order": config["Execution_order"],
                },
            )
            return
        raise ValueError("engine is not supported")

    def start(self) -> None:
        """Launch wrf.exe under MPI from the configured directory."""

        config = cast(dict[str, Any], self.config)
        wrf_location = config.get("wrf_location")
        if not isinstance(wrf_location, str) or not wrf_location:
            raise ValueError("wrf_location is required")
        Exec(
            "wrf.exe",
            MpiExecInfo(
                nprocs=config["nprocs"],
                ppn=config["ppn"],
                hostfile=self.hostfile,
                env=self.mod_env,
                cwd=wrf_location,
            ),
        ).run()

    def stop(self) -> None:
        """Do nothing because the legacy launcher runs to completion."""

    def clean(self) -> None:
        """Remove the configured legacy metadata database."""

        config = cast(dict[str, Any], self.config)
        db_path = config.get("db_path")
        if not isinstance(db_path, str) or not db_path:
            raise ValueError("db_path is required")
        Rm([db_path], PsshExecInfo(hostfile=self.hostfile)).run()
