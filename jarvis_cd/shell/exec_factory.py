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
        
    def _prepare_container(self, cmd: str):
        """
        Wrap a command for the configured container engine and return a cleaned ExecInfo.

        Handles LD_PRELOAD specially: the path exists only inside the container, so
        passing it raw in the subprocess env causes the host linker to fail. Instead:
          - apptainer: passes LD_PRELOAD via --env (host linker never sees it)
          - docker/podman: passes LD_PRELOAD via -e flag

        :param cmd: Command to execute inside the container
        :return: (wrapped_cmd, exec_info) — exec_info has LD_PRELOAD removed from env
        """
        c = self.exec_info.container
        if not c or c == 'none':
            return cmd, self.exec_info

        gpu = self.exec_info.gpu
        env = dict(self.exec_info.env) if self.exec_info.env else {}
        ld_preload = env.pop('LD_PRELOAD', None)

        # For apptainer, resolve the SIF path from shared_dir (accessible on all nodes)
        if c == 'apptainer' and self.exec_info.shared_dir and self.exec_info.container_image:
            from pathlib import Path
            img = str(Path(self.exec_info.shared_dir) / f'{self.exec_info.container_image}.sif')
        else:
            img = self.exec_info.container_image or ''

        mounts = self.exec_info.bind_mounts or []
        if c == 'apptainer':
            gpu_flag = '--nv ' if gpu else ''
            env_flag = f'--env LD_PRELOAD={ld_preload} ' if ld_preload else ''
            mount_flags = ''.join(f'--bind {m} ' for m in mounts)
            wrapped = f'apptainer exec {gpu_flag}{env_flag}{mount_flags}{img} {cmd}'
        elif c == 'podman':
            gpu_flag = '--gpus all ' if gpu else ''
            env_flag = f'-e LD_PRELOAD={ld_preload} ' if ld_preload else ''
            mount_flags = ''.join(f'-v {m} ' for m in mounts)
            wrapped = f'podman run --rm --network host {gpu_flag}{env_flag}{mount_flags}{img} {cmd}'
        else:  # docker
            gpu_flag = '--gpus all ' if gpu else ''
            env_flag = f'-e LD_PRELOAD={ld_preload} ' if ld_preload else ''
            mount_flags = ''.join(f'-v {m} ' for m in mounts)
            wrapped = f'docker run --rm --network host {gpu_flag}{env_flag}{mount_flags}{img} {cmd}'

        return wrapped, self.exec_info.mod(env=env)

    def run(self):
        """Execute the command using appropriate executor"""
        _MPI_TYPES = (ExecType.MPI, ExecType.OPENMPI, ExecType.MPICH,
                      ExecType.INTEL_MPI, ExecType.CRAY_MPICH)
        is_container = self.exec_info.container and self.exec_info.container != 'none'
        is_mpi = self.exec_info.exec_type in _MPI_TYPES

        if is_mpi and is_container:
            # Build the full mpirun command first (without running), then wrap
            # with the container so the container executes the entire mpirun
            # invocation rather than just the application binary.
            mpi_executor = MpiExec(self.cmd, self.exec_info.mod(container='none'))
            mpi_cmd = mpi_executor.cmd
            wrapped_cmd, local_info = self._prepare_container(mpi_cmd)
            self._delegate = LocalExec(wrapped_cmd, local_info.mod(exec_type=ExecType.LOCAL))
        elif is_mpi:
            self._delegate = MpiExec(self.cmd, self.exec_info)
        else:
            cmd, exec_info = self._prepare_container(self.cmd)
            if exec_info.exec_type == ExecType.LOCAL:
                self._delegate = LocalExec(cmd, exec_info)
            elif exec_info.exec_type == ExecType.SSH:
                self._delegate = SshExec(cmd, exec_info)
            elif exec_info.exec_type == ExecType.PSSH:
                self._delegate = PsshExec(cmd, exec_info)
            else:
                raise ValueError(f"Unsupported execution type: {exec_info.exec_type}")
            
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