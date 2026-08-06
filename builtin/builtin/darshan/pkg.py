"""
This module provides classes and methods to inject the Darshan interceptor.
Darshan is ....
"""

import os
from typing import Any, cast

from jarvis_cd.core.pkg import Interceptor
from jarvis_cd.shell import PsshExecInfo
from jarvis_cd.shell.process import Mkdir


class Darshan(Interceptor):
    """
    This class provides methods to inject the Darshan interceptor.
    """

    def _init(self) -> None:
        """
        Initialize paths
        """
        pass

    def _configure_menu(self) -> list[dict[str, Any]]:
        """
        Create a CLI menu for the configurator method.
        For thorough documentation of these parameters, view:
        https://github.com/scs-lab/jarvis-util/wiki/3.-Argument-Parsing

        :return: List(dict)
        """
        return [
            {
                "name": "log_dir",
                "msg": "Where darshan should place data",
                "type": str,
                "default": f"{os.getenv('HOME')}/darshan_logs",
            },
            {
                "name": "job_id",
                "msg": "A semantic ID for the job to identify log files",
                "type": str,
                "default": "myjob",
            },
            {
                "name": "darshan_lib_container",
                "msg": "Path to libdarshan.so inside the container (container mode only)",
                "type": str,
                "default": "/opt/darshan/lib/libdarshan.so",
            },
        ]

    def _configure(self, **kwargs: Any) -> None:
        """
        Converts the Jarvis configuration to application-specific configuration.
        E.g., OrangeFS produces an orangefs.xml file.

        :param kwargs: Configuration parameters for this pkg.
        :return: None
        """
        config = cast(dict[str, Any], self.config)
        log_dir = config.get("log_dir")
        job_id = config.get("job_id")
        deploy_mode = config.get("deploy_mode")
        if not isinstance(log_dir, str) or not log_dir:
            raise ValueError("Darshan log_dir must be a non-empty string")
        if not isinstance(job_id, str) or not job_id:
            raise ValueError("Darshan job_id must be a non-empty string")
        if deploy_mode == "container":
            library = config.get("darshan_lib_container")
            if not isinstance(library, str) or not library:
                raise ValueError(
                    "Darshan container library path must be a non-empty string"
                )
        else:
            library = self.find_library("darshan")
            if library is None:
                raise RuntimeError("Could not find darshan")
            print(f"Found libdarshan.so at {library}")
        config["DARSHAN_LIB"] = library
        self.env["DARSHAN_LOG_DIR"] = log_dir
        self.env["PBS_JOBID"] = job_id
        Mkdir(log_dir, PsshExecInfo(hostfile=self.hostfile)).run()

    def modify_env(self) -> None:
        """
        Modify the jarvis environment.

        :return: None
        """
        config = cast(dict[str, Any], self.config)
        log_dir = config.get("log_dir")
        job_id = config.get("job_id")
        library = config.get("DARSHAN_LIB")
        if not all(isinstance(value, str) and value for value in (log_dir, job_id)):
            raise ValueError("Darshan runtime configuration is incomplete")
        if not isinstance(library, str) or not library:
            raise ValueError("Darshan library was not resolved during configuration")
        self.setenv("DARSHAN_LOG_DIR", cast(str, log_dir))
        self.setenv("PBS_JOBID", cast(str, job_id))
        self.prepend_env("LD_PRELOAD", library)
