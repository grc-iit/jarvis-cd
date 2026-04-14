"""
Exec base class for Jarvis shell execution.
"""
from typing import Dict
from .exec_info import ExecInfo, ExecType
from .core_exec import CoreExec, LocalExec
from .ssh_exec import SshExec, PsshExec
from .mpi_exec import MpiExec
from .scp_exec import ScpExec, PscpExec


class Exec(CoreExec):
    """
    Base execution class that delegates to appropriate executor based on ExecInfo type.
    """
    
    def __init__(self, cmd: str, exec_info: ExecInfo):
        """
        Initialize executor with command and execution info.
        
        :param cmd: Command to execute
        :param exec_info: Execution information
        """
        super().__init__()
        self.cmd = cmd
        self.exec_info = exec_info
        self._delegate = None
        
    def _wrap_container_cmd(self, cmd: str) -> str:
        """
        Wrap a shell command to run inside the configured container engine.

        Reads container settings from self.exec_info:
          - container: engine name ('none', 'docker', 'podman', 'apptainer')
          - gpu: whether to enable GPU passthrough
          - container_image: image name (docker/podman) or SIF path (apptainer)

        Returns the original command unchanged when container is 'none' or unset.

        :param cmd: Command to execute
        :return: Wrapped command string, or the original command if no container
        """
        c = self.exec_info.container
        if not c or c == 'none':
            return cmd
        gpu = self.exec_info.gpu
        img = self.exec_info.container_image or ''
        if c == 'apptainer':
            gpu_flag = '--nv ' if gpu else ''
            return f'apptainer exec {gpu_flag}{img} {cmd}'
        elif c == 'podman':
            gpu_flag = '--gpus all ' if gpu else ''
            return f'podman run --rm {gpu_flag}{img} {cmd}'
        else:  # docker
            gpu_flag = '--gpus all ' if gpu else ''
            return f'docker run --rm {gpu_flag}{img} {cmd}'

    def run(self):
        """Execute the command using appropriate executor"""
        cmd = self._wrap_container_cmd(self.cmd)

        # Create the appropriate executor based on exec_info type
        if self.exec_info.exec_type == ExecType.LOCAL:
            self._delegate = LocalExec(cmd, self.exec_info)
        elif self.exec_info.exec_type == ExecType.SSH:
            self._delegate = SshExec(cmd, self.exec_info)
        elif self.exec_info.exec_type == ExecType.PSSH:
            self._delegate = PsshExec(cmd, self.exec_info)
        elif self.exec_info.exec_type in [ExecType.MPI, ExecType.OPENMPI,
                                         ExecType.MPICH, ExecType.INTEL_MPI,
                                         ExecType.CRAY_MPICH]:
            self._delegate = MpiExec(cmd, self.exec_info)
        else:
            raise ValueError(f"Unsupported execution type: {self.exec_info.exec_type}")
            
        # Copy delegate attributes to self
        self.exit_code = self._delegate.exit_code
        self.stdout = self._delegate.stdout
        self.stderr = self._delegate.stderr
        self.processes = self._delegate.processes
        self.output_threads = self._delegate.output_threads
        
        return self._delegate
        
    def get_cmd(self) -> str:
        """Get the command string"""
        return self.cmd
        
    def wait(self, hostname: str = 'localhost') -> int:
        """Wait for execution to complete"""
        if self._delegate:
            return self._delegate.wait(hostname)
        return 0
        
    def wait_all(self) -> Dict[str, int]:
        """Wait for all executions to complete"""
        if self._delegate:
            return self._delegate.wait_all()
        return {}