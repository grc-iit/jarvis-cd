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

        # For apptainer, resolve the SIF path from shared_dir (accessible on all nodes)
        if c == 'apptainer' and self.exec_info.shared_dir and self.exec_info.container_image:
            from pathlib import Path
            # The .sif lives in the pipeline shared dir (parent of the
            # per-package shared dir).
            img = str(Path(self.exec_info.shared_dir).parent / f'{self.exec_info.container_image}.sif')
        else:
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
        #
        # Skip vars whose value must come from the container image itself:
        #  - process-level identity: PATH, HOME, USER, SHELL, LOGNAME, ...
        #  - dynamic linker / compiler search paths: host paths like
        #    /opt/aurora/... or /opt/cray/... don't exist inside the
        #    container. Forwarding these clobbers /.singularity.d/env/*.sh
        #    (e.g. Aurora's Intel oneAPI LD_LIBRARY_PATH overriding the
        #    container's /opt/intel/oneapi/compiler/2025.0/lib, which
        #    causes SYCL's Level Zero UR adapter to fail to load and
        #    emit "No device of requested type GPU available").
        #  - MPI wrapper paths: host mpicc/mpicxx aren't in the container.
        #  - PYTHONPATH/CLASSPATH: host interpreter paths won't resolve.
        # Callers that legitimately need to override a container LD path
        # should use an explicit --env flag, not leak host env.
        _HOST_ONLY = {'PATH', 'HOME', 'USER', 'MANPATH', 'TEST_MODE',
                      'PWD', 'OLDPWD', 'SHELL', 'LOGNAME', 'TERM',
                      'LD_LIBRARY_PATH', 'LIBRARY_PATH',
                      'CPATH', 'C_INCLUDE_PATH', 'CPLUS_INCLUDE_PATH',
                      'CMAKE_PREFIX_PATH', 'PKG_CONFIG_PATH',
                      'MPICC', 'MPICXX', 'MPIF77', 'MPIF90',
                      'CC', 'CXX', 'FC', 'F77', 'F90',
                      'PYTHONPATH', 'CLASSPATH'}

        def _shell_quote(value: str) -> str:
            return "'" + str(value).replace("'", "'\\''") + "'"

        mounts = self.exec_info.bind_mounts or []
        if c == 'apptainer':
            if gpu is True or gpu == 'nvidia':
                gpu_flag = '--nv '
            elif gpu == 'intel':
                # Intel GPU passthrough on Aurora-class nodes.
                # Do NOT bind /dev/dri or /sys/class/drm explicitly —
                # apptainer auto-mounts both with the correct permissions
                # for the user namespace. Explicit binds replace those
                # auto-mounts with host bind-mounts that the mapped-to-
                # root user cannot open(O_RDWR), producing EACCES on
                # /dev/dri/renderD*. See apptainer/apptainer#2963.
                # We only forward PBS-allocated GPU affinity env vars
                # so jobs see only their assigned PVC tiles.
                import os as _os
                gpu_flag = ''
                for _k in ('ZE_AFFINITY_MASK', 'ZEX_NUMBER_OF_CCS',
                           'ZE_FLAT_DEVICE_HIERARCHY',
                           'ONEAPI_DEVICE_SELECTOR'):
                    _v = _os.environ.get(_k)
                    if _v:
                        gpu_flag += f'--env {_k}={_v} '
            else:
                gpu_flag = ''
            # Auto-bind the PIPELINE shared dir (parent of the package shared
            # dir) and private_dir so paths outside apptainer's default binds
            # (/home, /tmp, /proc, /sys, /dev) are visible. Required when
            # jarvis shared_dir lives on /lus, /gpfs, etc.
            # Binding the parent (pipeline shared dir) ensures the hostfile
            # at {pipeline_shared_dir}/hostfile is visible to OpenMPI, in
            # addition to the run_dir under the package shared dir.
            from pathlib import Path as _Path
            auto_binds = []
            if self.exec_info.shared_dir:
                auto_binds.append(str(_Path(self.exec_info.shared_dir).parent))
            if self.exec_info.private_dir and str(self.exec_info.private_dir) not in auto_binds:
                auto_binds.append(str(self.exec_info.private_dir))
            auto_bind_flags = ''.join(f'--bind {b} ' for b in auto_binds)
            env_flags_parts = [
                f'--env {k}={_shell_quote(v)}'
                for k, v in env.items() if k not in _HOST_ONLY
            ]
            env_flag = (' '.join(env_flags_parts) + ' ') if env_flags_parts else ''
            mount_flags = ''.join(f'--bind {m} ' for m in mounts)
            wrapped = f'apptainer exec {gpu_flag}{auto_bind_flags}{env_flag}{mount_flags}{img} {shell_cmd}'
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
            wrapped = f'{c} exec {env_flag}{container_name} {shell_cmd}'

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
        Detect the MPI implementation.  MpiVersion runs 'mpiexec --version'
        and may need to reach a container on a remote host — it handles
        that internally via SshExec so there is no circular dependency.
        """
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
                # Apptainer (HPC): mpirun runs INSIDE the container.
                # Clear env so mpirun's env doesn't override the
                # container's PATH/LD_LIBRARY_PATH with host values.
                mpi_type = self._detect_mpi(self.exec_info)
                mpi_executor = self._create_mpi_executor_with_type(
                    self.cmd,
                    self.exec_info.mod(container='none', dry_run=True,
                                       env={}),
                    mpi_type)
                mpi_cmd = mpi_executor.cmd
                # Apptainer runs under an unprivileged user namespace,
                # which breaks OpenMPI's default CMA (process_vm_readv)
                # single-copy intra-node transfer. Every failed CMA
                # syscall prints `Read -1, expected N, errno = 14`
                # (EFAULT); OpenMPI then falls back to plain shmem
                # copy-in/copy-out so the run is correct but the log
                # is flooded. Disable single-copy up front.
                if mpi_type == ExecType.OPENMPI and '--mca btl_vader_single_copy_mechanism' not in mpi_cmd:
                    mpi_cmd = mpi_cmd.replace(
                        'mpiexec ',
                        'mpiexec --mca btl_vader_single_copy_mechanism none ',
                        1)
                # Split single-node vs multi-node launch strategy.
                # On single node, OpenMPI's default fork-local spawn is
                # correct: no rsh/ssh needed, env is inherited directly
                # from the `apptainer exec` context (which sources
                # /.singularity.d/env/*.sh). Forcing `--mca plm rsh` here
                # causes mpiexec to ssh to the host on port 22, producing
                # the "no GPU available" failure on Aurora because the
                # Level Zero / SYCL runtime env does not survive the
                # detour. For multi-node, we do need rsh + explicit -x
                # env forwarding so remote orted picks up container paths.
                hf = self.exec_info.hostfile
                # Multi-node iff hostfile lists more than one host AND
                # at least one host is not this machine. Single-entry
                # hostfiles — whether 'localhost', 127.0.0.1, or the
                # compute node's own hostname — are all single-node
                # launches where mpiexec should fork-spawn locally.
                is_multinode = False
                if hf is not None and len(hf) > 1:
                    import socket as _socket
                    _local_names = {'localhost', '127.0.0.1',
                                    _socket.gethostname()}
                    is_multinode = any(h not in _local_names
                                       for h in hf.hosts)
                if is_multinode:
                    if '--mca plm rsh' not in mpi_cmd:
                        # OpenMPI rsh PLM scaling flags. Without these the
                        # bring-up hangs at >10 nodes:
                        #   plm_rsh_no_tree_spawn=1 — head daemon SSHes ALL
                        #     remote daemons directly in parallel (no
                        #     intermediate-node tree spawn).
                        #   routed=direct — ranks talk directly to peers
                        #     (skip proxy hops), simpler at scale.
                        #   plm_rsh_num_concurrent=32 — allow 32 parallel
                        #     SSH launches; defaults are too low for >10 nodes.
                        mpi_cmd = mpi_cmd.replace(
                            'mpiexec ',
                            'mpiexec --mca plm rsh '
                            '--mca plm_rsh_no_tree_spawn 1 '
                            '--mca routed direct '
                            '--mca plm_rsh_num_concurrent 32 ', 1)
                    # sshd login shell into the apptainer instance on a
                    # remote node does NOT source /.singularity.d/env/*.sh,
                    # so Level Zero / SYCL runtime selectors set there are
                    # empty in child ranks unless forwarded via -x.
                    _forward_env = [
                        'PATH', 'LD_LIBRARY_PATH',
                        'ONEAPI_DEVICE_SELECTOR',
                        'ZE_FLAT_DEVICE_HIERARCHY',
                        'ZE_AFFINITY_MASK',
                        'ZEX_NUMBER_OF_CCS',
                        'ZE_ENABLE_PCI_ID_DEVICE_ORDER',
                        'OCL_ICD_FILENAMES',
                        'SYCL_CACHE_PERSISTENT',
                    ]
                    _x_flags = ' '.join(f'-x {v}' for v in _forward_env)
                    mpi_cmd = mpi_cmd.replace(
                        'mpiexec ', f'mpiexec {_x_flags} ', 1)
                wrapped_cmd, local_info = self._prepare_container(mpi_cmd)
                wrapped_cmd, local_info = self._resolve_exec_info(
                    wrapped_cmd, local_info.mod(exec_type=ExecType.LOCAL))
            else:
                # Docker/Podman: mpirun runs INSIDE the already-running
                # container.  Build the full mpirun command, then wrap it.
                mpi_type = self._detect_mpi(self.exec_info)
                mpi_executor = self._create_mpi_executor_with_type(
                    self.cmd, self.exec_info.mod(container='none', dry_run=True),
                    mpi_type)
                mpi_cmd = mpi_executor.cmd
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
