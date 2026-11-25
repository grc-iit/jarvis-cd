"""
Exec base class for Jarvis shell execution.
"""
from typing import Dict
from .exec_info import ExecInfo, ExecType
from .core_exec import CoreExec, LocalExec, MpiVersion
from .ssh_exec import SshExec, PsshExec
from .mpi_exec import OpenMpiExec, MpichExec, IntelMpiExec, CrayMpichExec
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

    @staticmethod
    def _network_env_prefix(mpi_executor) -> str:
        """``env K=V `` for the resource-graph network launcher settings.

        Inside a container the host-side launcher env does not reach mpiexec,
        so the selected network travels on the command line instead.
        """
        import shlex
        network = getattr(mpi_executor, 'network', None) or {}
        env = (network.get('applied') or {}).get('launcher_env') or {}
        if not env:
            return ''
        return 'env ' + ' '.join(
            f'{key}={shlex.quote(str(value))}' for key, value in sorted(env.items())
        ) + ' '

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

        env = dict(self.exec_info.env) if self.exec_info.env else {}
        img = self.exec_info.container_image or ''

        # Wrap the command in bash -c so that shell metacharacters
        # (&&, |, >, >>, ;, $()) are interpreted inside the container
        # rather than by the host shell.
        escaped = cmd.replace("'", "'\\''")
        shell_cmd = f"bash -c '{escaped}'"

        # Forward every package-set env var into the container. Without
        # this, values prepended on the host shell (in ssh_exec's
        # env_prefix) apply to `docker exec` itself but never reach the
        # inner process — so things like CHI_SERVER_CONF / CHRONOLOG_CONF
        # are silently dropped and the binary falls back to defaults.
        # Host-only vars (PATH, HOME, USER, MANPATH, TEST_MODE) are
        # skipped so the container's own image env wins for them.
        _HOST_ONLY = {'PATH', 'HOME', 'USER', 'MANPATH', 'TEST_MODE',
                      'PWD', 'OLDPWD', 'SHELL', 'LOGNAME', 'TERM'}

        def _shell_quote(value: str) -> str:
            return "'" + str(value).replace("'", "'\\''") + "'"

        if c == 'apptainer':
            # Enter the long-running apptainer instance the pipeline
            # started in _start_containerized_pipeline (apptainer
            # instance start ... <pipeline_name>). The container_image
            # field doubles as the instance name.
            #
            # Bind mounts, --nv, and --add-caps are silently ignored on
            # a running instance, so they must be set at instance-start
            # time (see pipeline.py) — they are deliberately NOT
            # forwarded here.
            env_flags_parts = [
                f'--env {k}={_shell_quote(v)}'
                for k, v in env.items() if k not in _HOST_ONLY
            ]
            env_flag = (' '.join(env_flags_parts) + ' ') if env_flags_parts else ''
            wrapped = f'apptainer exec {env_flag}instance://{img} {shell_cmd}'
        elif c in ('podman', 'docker'):
            # Use 'exec' into the already-running container (started by
            # docker/podman compose).  The container_image doubles as the
            # container name stem; the compose file names the container
            # "{image}_container".
            container_name = f'{img}_container' if img else ''
            env_flags_parts = [
                f'-e {k}={_shell_quote(v)}'
                for k, v in env.items() if k not in _HOST_ONLY
            ]
            env_flag = (' '.join(env_flags_parts) + ' ') if env_flags_parts else ''
            engine = c
            if c == 'podman':
                # the node wrapper (node-local store, an OCI runtime the node
                # has), also when this runs on another node through ssh
                from jarvis_cd.core.method_install import engine_command
                engine = engine_command(c)
            wrapped = f'{engine} exec {env_flag}{container_name} {shell_cmd}'

        # Env vars are now carried via `-e` into the container; don't
        # also emit them as a prefix on the host shell in ssh_exec.
        return wrapped, self.exec_info.mod(env={})

    def _resolve_exec_info(self, cmd: str, exec_info: ExecInfo):
        """
        If exec_type is LOCAL but the hostfile points to remote hosts,
        promote to SSH on the first host.  This keeps container commands
        (docker exec …) running where the containers actually live.
        """
        if exec_info.exec_type == ExecType.LOCAL:
            hostfile = exec_info.hostfile
            if hostfile and not hostfile.is_local():
                return cmd, exec_info.mod(
                    exec_type=ExecType.SSH,
                    hostfile=hostfile.subset(1),
                    port=22,
                )
        return cmd, exec_info

    def _detect_mpi(self, exec_info: ExecInfo):
        """
        Detect the MPI implementation.  ``JARVIS_MPI_TYPE`` in the env
        (exported by the Spack installer for the MPI the pipeline chose)
        wins; otherwise MpiVersion runs 'mpiexec --version' and may need
        to reach a container on a remote host — it handles that internally
        via SshExec so there is no circular dependency.
        """
        chosen = (exec_info.env or {}).get('JARVIS_MPI_TYPE', '').strip().lower()
        if chosen:
            try:
                mpi_type = ExecType(chosen)
            except ValueError:
                mpi_type = None
            if mpi_type in (ExecType.OPENMPI, ExecType.MPICH,
                            ExecType.INTEL_MPI, ExecType.CRAY_MPICH):
                return mpi_type
        detector = MpiVersion(exec_info)
        return detector.version

    def _create_mpi_executor(self, cmd, exec_info):
        """Create the concrete MPI executor after detecting the implementation."""
        mpi_type = self._detect_mpi(exec_info)
        return self._create_mpi_executor_with_type(cmd, exec_info, mpi_type)

    def _create_mpi_executor_with_type(self, cmd, exec_info, mpi_type):
        if mpi_type == ExecType.OPENMPI:
            return OpenMpiExec(cmd, exec_info)
        elif mpi_type == ExecType.MPICH:
            return MpichExec(cmd, exec_info)
        elif mpi_type == ExecType.INTEL_MPI:
            return IntelMpiExec(cmd, exec_info)
        elif mpi_type == ExecType.CRAY_MPICH:
            return CrayMpichExec(cmd, exec_info)
        else:
            print(f"Unknown MPI type {mpi_type}, defaulting to MPICH")
            return MpichExec(cmd, exec_info)

    def run(self):
        """Execute the command using appropriate executor"""
        _MPI_TYPES = (ExecType.MPI, ExecType.OPENMPI, ExecType.MPICH,
                      ExecType.INTEL_MPI, ExecType.CRAY_MPICH)
        is_container = self.exec_info.container and self.exec_info.container != 'none'
        is_mpi = self.exec_info.exec_type in _MPI_TYPES

        if is_mpi and is_container:
            is_apptainer = self.exec_info.container == 'apptainer'
            if is_apptainer:
                # Apptainer (HPC): mpirun runs INSIDE the container and
                # uses SSH to reach apptainer instances on remote nodes.
                # Clear env so mpirun -x flags don't override the
                # container's PATH/LD_LIBRARY_PATH with host values.
                mpi_type = self._detect_mpi(self.exec_info)
                # Open MPI keeps ssh here rather than its srun agent.
                ssh_cmd = self.exec_info.ssh_cmd or (
                    'ssh' if mpi_type == ExecType.OPENMPI else None)
                mpi_executor = self._create_mpi_executor_with_type(
                    self.cmd,
                    self.exec_info.mod(container='none', dry_run=True,
                                       env={}, ssh_cmd=ssh_cmd),
                    mpi_type)
                mpi_cmd = mpi_executor.cmd
                # Force SSH launcher (not Slurm) so mpirun reaches the
                # sshd running inside apptainer instances on remote nodes.
                # Export PATH so remote orted finds application binaries
                # (the remote SSH login shell may override the container PATH).
                if '--mca plm rsh' not in mpi_cmd:
                    mpi_cmd = mpi_cmd.replace(
                        'mpiexec ', 'mpiexec --mca plm rsh ', 1)
                mpi_cmd = mpi_cmd.replace(
                    'mpiexec ', 'mpiexec -x PATH -x LD_LIBRARY_PATH ', 1)
                mpi_cmd = self._network_env_prefix(mpi_executor) + mpi_cmd
                wrapped_cmd, local_info = self._prepare_container(mpi_cmd)
                wrapped_cmd, local_info = self._resolve_exec_info(
                    wrapped_cmd, local_info.mod(exec_type=ExecType.LOCAL))
            else:
                # Docker/Podman: mpirun runs INSIDE the already-running
                # container.  Build the full mpirun command, then wrap it.
                mpi_type = self._detect_mpi(self.exec_info)
                ssh_cmd = self.exec_info.ssh_cmd or (
                    'ssh' if mpi_type == ExecType.OPENMPI else None)
                mpi_executor = self._create_mpi_executor_with_type(
                    self.cmd, self.exec_info.mod(container='none', dry_run=True,
                                                 ssh_cmd=ssh_cmd),
                    mpi_type)
                mpi_cmd = self._network_env_prefix(mpi_executor) + mpi_executor.cmd
                wrapped_cmd, local_info = self._prepare_container(mpi_cmd)
                wrapped_cmd, local_info = self._resolve_exec_info(
                    wrapped_cmd, local_info.mod(exec_type=ExecType.LOCAL))
        elif is_mpi:
            self._delegate = self._create_mpi_executor(self.cmd, self.exec_info)
        else:
            cmd, exec_info = self._prepare_container(self.cmd)
            wrapped_cmd, local_info = self._resolve_exec_info(cmd, exec_info)

        if not self._delegate:
            if local_info.exec_type == ExecType.LOCAL:
                self._delegate = LocalExec(wrapped_cmd, local_info)
            elif local_info.exec_type == ExecType.SSH:
                self._delegate = SshExec(wrapped_cmd, local_info)
            elif local_info.exec_type == ExecType.PSSH:
                self._delegate = PsshExec(wrapped_cmd, local_info)
            else:
                raise ValueError(f"Unsupported execution type: {local_info.exec_type}")

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

    def launch_record(self):
        """The MPI launch's ``launch_record()`` (None before run or for non-MPI)."""
        probe = getattr(self._delegate, 'launch_record', None)
        return probe() if callable(probe) else None

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
