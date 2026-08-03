"""Legacy script-driven OpenFOAM launcher retained for compatibility."""

from __future__ import annotations

from typing import Any, cast

from jarvis_cd.core.pkg import Application
from jarvis_cd.shell import Exec, MpiExecInfo


class OpenfoamLegacy(Application):
    """Run an operator-provided OpenFOAM case script natively or in a container."""

    def _init(self) -> None:
        """Initialize the stateless legacy launcher."""

    def _configure_menu(self) -> list[dict[str, Any]]:
        """Return the legacy OpenFOAM launch controls."""

        return [
            {
                "name": "nprocs",
                "msg": "Number of MPI processes",
                "type": int,
                "default": 1,
            },
            {
                "name": "ppn",
                "msg": "Processes per node",
                "type": int,
                "default": 4,
            },
            {
                "name": "script_location",
                "msg": "Case directory containing Allrun script",
                "type": str,
                "default": None,
            },
            {
                "name": "script",
                "msg": "Script to execute inside script_location",
                "type": str,
                "default": "./Allrun",
            },
            {
                "name": "base_image",
                "msg": "Base Docker image for build container",
                "type": str,
                "default": "sci-hpc-base",
            },
        ]

    # Pkg leaves this override point untyped, so Pyright infers a None-only return.
    def _build_phase(  # pyright: ignore[reportIncompatibleMethodOverride]
        self,
    ) -> tuple[str, str] | None:
        """Return the legacy container build script when explicitly selected."""

        config = cast(dict[str, Any], self.config)
        if config.get("deploy_mode") != "container":
            return None
        content = self._read_build_script(
            "build.sh",
            {"BASE_IMAGE": config.get("base_image", "sci-hpc-base")},
        )
        return content, "openfoam-dev"

    def _build_deploy_phase(  # pyright: ignore[reportIncompatibleMethodOverride]
        self,
    ) -> tuple[str, str] | None:
        """Return the legacy deploy image recipe when explicitly selected."""

        config = cast(dict[str, Any], self.config)
        if config.get("deploy_mode") != "container":
            return None
        content = self._read_dockerfile(
            "Dockerfile.deploy",
            {
                "BUILD_IMAGE": self.build_image_name(),
                "DEPLOY_BASE": "ubuntu:24.04",
            },
        )
        return content, "openfoam-dev"

    def _configure(self, **kwargs: Any) -> None:
        """Persist legacy launcher configuration."""

        Application._configure(self, **kwargs)

    def start(self) -> None:
        """Run the configured case script under MPI."""

        config = cast(dict[str, Any], self.config)
        script = config.get("script", "./Allrun")
        cwd = config.get("script_location")
        if not isinstance(script, str) or not script:
            raise ValueError("script must be a non-empty string")
        if cwd is not None and not isinstance(cwd, str):
            raise ValueError("script_location must be a path string")
        foam_env = "source /opt/OpenFOAM/OpenFOAM-dev/etc/bashrc"
        command = f'bash -c "{foam_env} && {script}"'
        execution = {
            "nprocs": config["nprocs"],
            "ppn": config["ppn"],
            "hostfile": self.hostfile,
            "env": self.mod_env,
            "cwd": cwd,
        }
        if config.get("deploy_mode") == "container":
            execution.update(
                {
                    "port": self.ssh_port,
                    "container": self._container_engine,
                    "container_image": self.deploy_image_name(),
                    "shared_dir": self.shared_dir,
                    "private_dir": self.private_dir,
                }
            )
        Exec(command, MpiExecInfo(**execution)).run()

    def stop(self) -> None:
        """Do nothing because the legacy launcher runs to completion."""

    def clean(self) -> None:
        """Leave case outputs in the operator-provided script location."""
