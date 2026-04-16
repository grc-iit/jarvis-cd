"""
Container compose execution classes for Docker and Podman.
"""
from pathlib import Path
from typing import Dict, Any, Optional
import hashlib
from .core_exec import CoreExec, LocalExec
from .exec_info import ExecInfo


class PodmanBuildExec(CoreExec):
    """
    Execute podman compose build command.
    """

    def __init__(self, compose_file: str, exec_info: ExecInfo):
        """
        Initialize podman compose build.

        :param compose_file: Path to compose file
        :param exec_info: Execution information
        """
        super().__init__()
        self.compose_file = Path(compose_file)
        self.exec_info = exec_info
        self.local_exec = None

        if not self.compose_file.exists():
            raise FileNotFoundError(f"Compose file not found: {self.compose_file}")

    def get_cmd(self) -> str:
        """Get the podman compose build command string"""
        import shutil
        # Use podman-compose if available, otherwise use podman compose
        if shutil.which('podman-compose'):
            return f"podman-compose -f {self.compose_file} build"
        else:
            # Check if podman has compose subcommand
            from .exec_info import LocalExecInfo
            test_exec = LocalExec('podman compose --help', LocalExecInfo())
            if test_exec.exit_code == 0:
                return f"podman compose -f {self.compose_file} build"

            raise RuntimeError(
                "podman-compose not found and podman compose subcommand not available. "
                "Please install podman-compose: pip install podman-compose"
            )

    def run(self):
        """Execute the podman compose build command"""
        cmd = self.get_cmd()
        self.local_exec = LocalExec(cmd, self.exec_info)

        # Copy state from LocalExec
        self.exit_code = self.local_exec.exit_code
        self.stdout = self.local_exec.stdout
        self.stderr = self.local_exec.stderr
        self.processes = self.local_exec.processes
        self.output_threads = self.local_exec.output_threads


class DockerBuildExec(CoreExec):
    """
    Execute docker compose build command.
    """

    def __init__(self, compose_file: str, exec_info: ExecInfo):
        """
        Initialize docker compose build.

        :param compose_file: Path to compose file
        :param exec_info: Execution information
        """
        super().__init__()
        self.compose_file = Path(compose_file)
        self.exec_info = exec_info
        self.local_exec = None

        if not self.compose_file.exists():
            raise FileNotFoundError(f"Compose file not found: {self.compose_file}")

    def get_cmd(self) -> str:
        """Get the docker compose build command string"""
        return f"docker compose -f {self.compose_file} build"

    def run(self):
        """Execute the docker compose build command"""
        cmd = self.get_cmd()
        self.local_exec = LocalExec(cmd, self.exec_info)

        # Copy state from LocalExec
        self.exit_code = self.local_exec.exit_code
        self.stdout = self.local_exec.stdout
        self.stderr = self.local_exec.stderr
        self.processes = self.local_exec.processes
        self.output_threads = self.local_exec.output_threads


class ContainerBuildExec(CoreExec):
    """
    Router for container build - automatically selects between Docker and Podman.
    """

    def __init__(self, compose_file: str, exec_info: ExecInfo, prefer_podman: bool = False):
        """
        Initialize container build execution.

        :param compose_file: Path to compose file
        :param exec_info: Execution information
        :param prefer_podman: Prefer Podman over Docker if both available
        """
        super().__init__()
        self.compose_file = Path(compose_file)
        self.exec_info = exec_info
        self.prefer_podman = prefer_podman
        self.delegate = None

        # Determine which build implementation to use
        self._select_implementation()

    def _select_implementation(self):
        """Select Docker or Podman based on availability"""
        import shutil

        has_docker = shutil.which('docker') is not None
        has_podman = shutil.which('podman') is not None or shutil.which('podman-compose') is not None

        if self.prefer_podman and has_podman:
            self.delegate = PodmanBuildExec(self.compose_file, self.exec_info)
        elif has_docker:
            self.delegate = DockerBuildExec(self.compose_file, self.exec_info)
        elif has_podman:
            self.delegate = PodmanBuildExec(self.compose_file, self.exec_info)
        else:
            raise RuntimeError("Neither docker nor podman found in PATH")

    def get_cmd(self) -> str:
        """Get the command string from delegate"""
        return self.delegate.get_cmd()

    def run(self):
        """Execute the build command via delegate"""
        self.delegate.run()

        # Copy state from delegate
        self.exit_code = self.delegate.exit_code
        self.stdout = self.delegate.stdout
        self.stderr = self.delegate.stderr
        self.processes = self.delegate.processes
        self.output_threads = self.delegate.output_threads


class PodmanComposeExec(CoreExec):
    """
    Execute podman compose commands.
    """

    def __init__(self, compose_file: str, exec_info: ExecInfo, action: str = 'up'):
        """
        Initialize podman compose execution.

        :param compose_file: Path to compose file
        :param exec_info: Execution information
        :param action: Compose action (up, down, etc.)
        """
        super().__init__()
        self.compose_file = Path(compose_file)
        self.exec_info = exec_info
        self.action = action
        self.local_exec = None

        if not self.compose_file.exists():
            raise FileNotFoundError(f"Compose file not found: {self.compose_file}")

    def get_cmd(self) -> str:
        """Get the podman compose command string"""
        import shutil
        # Use podman-compose if available, otherwise use podman compose
        if shutil.which('podman-compose'):
            cmd = f"podman-compose -f {self.compose_file} {self.action}"
        else:
            # Check if podman has compose subcommand
            from .exec_info import LocalExecInfo
            test_exec = LocalExec('podman compose --help', LocalExecInfo())
            if test_exec.exit_code == 0:
                cmd = f"podman compose -f {self.compose_file} {self.action}"
            else:
                raise RuntimeError(
                    "podman-compose not found and podman compose subcommand not available. "
                    "Please install podman-compose: pip install podman-compose"
                )
        # For 'up', add flags to show output and exit when container stops
        if self.action == 'up':
            cmd += " --abort-on-container-exit"
        return cmd

    def run(self):
        """Execute the podman compose command"""
        cmd = self.get_cmd()
        self.local_exec = LocalExec(cmd, self.exec_info)

        # Copy state from LocalExec
        self.exit_code = self.local_exec.exit_code
        self.stdout = self.local_exec.stdout
        self.stderr = self.local_exec.stderr
        self.processes = self.local_exec.processes
        self.output_threads = self.local_exec.output_threads


class DockerComposeExec(CoreExec):
    """
    Execute docker compose commands.
    """

    def __init__(self, compose_file: str, exec_info: ExecInfo, action: str = 'up'):
        """
        Initialize docker compose execution.

        :param compose_file: Path to compose file
        :param exec_info: Execution information
        :param action: Compose action (up, down, etc.)
        """
        super().__init__()
        self.compose_file = Path(compose_file)
        self.exec_info = exec_info
        self.action = action
        self.local_exec = None

        if not self.compose_file.exists():
            raise FileNotFoundError(f"Compose file not found: {self.compose_file}")

    def get_cmd(self) -> str:
        """Get the docker compose command string"""
        cmd = f"docker compose -f {self.compose_file} {self.action}"
        # For 'up', add flags to show output and exit when container stops
        if self.action == 'up':
            cmd += " --abort-on-container-exit"
        return cmd

    def run(self):
        """Execute the docker compose command"""
        cmd = self.get_cmd()
        self.local_exec = LocalExec(cmd, self.exec_info)

        # Copy state from LocalExec
        self.exit_code = self.local_exec.exit_code
        self.stdout = self.local_exec.stdout
        self.stderr = self.local_exec.stderr
        self.processes = self.local_exec.processes
        self.output_threads = self.local_exec.output_threads


class ContainerComposeExec(CoreExec):
    """
    Router for container compose execution - automatically selects
    between Docker and Podman based on availability or configuration.
    """

    def __init__(self, compose_file: str, exec_info: ExecInfo, action: str = 'up',
                 prefer_podman: bool = False):
        """
        Initialize container compose execution.

        :param compose_file: Path to compose file
        :param exec_info: Execution information
        :param action: Compose action (up, down, etc.)
        :param prefer_podman: Prefer Podman over Docker if both available
        """
        super().__init__()
        self.compose_file = Path(compose_file)
        self.exec_info = exec_info
        self.action = action
        self.prefer_podman = prefer_podman
        self.delegate = None

        # Determine which compose implementation to use
        self._select_implementation()

    def _select_implementation(self):
        """Select Docker or Podman based on availability"""
        import shutil

        has_docker = shutil.which('docker') is not None
        has_podman = shutil.which('podman') is not None or shutil.which('podman-compose') is not None

        if self.prefer_podman and has_podman:
            self.delegate = PodmanComposeExec(self.compose_file, self.exec_info, self.action)
        elif has_docker:
            self.delegate = DockerComposeExec(self.compose_file, self.exec_info, self.action)
        elif has_podman:
            self.delegate = PodmanComposeExec(self.compose_file, self.exec_info, self.action)
        else:
            raise RuntimeError("Neither docker nor podman found in PATH")

    def get_cmd(self) -> str:
        """Get the command string from delegate"""
        return self.delegate.get_cmd()

    def run(self):
        """Execute the compose command via delegate"""
        self.delegate.run()

        # Copy state from delegate
        self.exit_code = self.delegate.exit_code
        self.stdout = self.delegate.stdout
        self.stderr = self.delegate.stderr
        self.processes = self.delegate.processes
        self.output_threads = self.delegate.output_threads


class ApptainerBuildExec(CoreExec):
    """
    Build an Apptainer SIF image from a local Docker/Podman image.
    Converts a locally available container image to an Apptainer SIF file.
    """

    def __init__(self, image_name: str, sif_path: str, exec_info: ExecInfo,
                 source: str = 'docker-daemon'):
        """
        Initialize apptainer build.

        :param image_name: Local docker/podman image name (e.g., 'mypipeline:latest')
        :param sif_path: Output path for the SIF file
        :param exec_info: Execution information
        :param source: Source protocol ('docker-daemon' for docker, 'docker-daemon' for podman too)
        """
        super().__init__()
        self.image_name = image_name
        self.sif_path = sif_path
        self.exec_info = exec_info
        self.source = source
        self.local_exec = None

    def get_cmd(self) -> str:
        """Get the apptainer build command string"""
        tag = self.image_name if ':' in self.image_name else f'{self.image_name}:latest'
        return f"apptainer build --force {self.sif_path} {self.source}://{tag}"

    def run(self):
        """Execute the apptainer build command"""
        cmd = self.get_cmd()
        self.local_exec = LocalExec(cmd, self.exec_info)

        self.exit_code = self.local_exec.exit_code
        self.stdout = self.local_exec.stdout
        self.stderr = self.local_exec.stderr
        self.processes = self.local_exec.processes
        self.output_threads = self.local_exec.output_threads


class ApptainerExec(CoreExec):
    """
    Execute a command inside an Apptainer container.
    """

    def __init__(self, sif_path: str, command: str, exec_info: ExecInfo,
                 gpu: bool = False, bind_paths: list = None):
        """
        Initialize apptainer exec.

        :param sif_path: Path to the SIF file
        :param command: Command to execute inside container
        :param exec_info: Execution information
        :param gpu: Enable GPU support (--nv flag)
        :param bind_paths: List of host:container path pairs to bind
        """
        super().__init__()
        self.sif_path = sif_path
        self.command = command
        self.exec_info = exec_info
        self.gpu = gpu
        self.bind_paths = bind_paths or []
        self.local_exec = None

    def get_cmd(self) -> str:
        """Get the apptainer exec command string.
        Wraps in bash -c so shell metacharacters run inside the container."""
        flags = []
        if self.gpu:
            flags.append('--nv')
        for bind in self.bind_paths:
            flags.append(f'--bind {bind}')
        flags_str = ' '.join(flags)
        escaped = self.command.replace("'", "'\\''")
        return f"apptainer exec {flags_str} {self.sif_path} bash -c '{escaped}'"

    def run(self):
        """Execute the command inside the apptainer container"""
        cmd = self.get_cmd()
        self.local_exec = LocalExec(cmd, self.exec_info)

        self.exit_code = self.local_exec.exit_code
        self.stdout = self.local_exec.stdout
        self.stderr = self.local_exec.stderr
        self.processes = self.local_exec.processes
        self.output_threads = self.local_exec.output_threads
