"""
SSH execution classes for Jarvis shell execution.

A command for another host reaches it in one of two ways:

- Inside a Slurm allocation, on a node of that allocation, it runs as a job
  step: ``srun --jobid --overlap -w <node> -N1 -n1`` (Slurm 20.11 or later).
  It stays in the job's
  cgroup, is accounted to the job, and ends when the job is cancelled.
- Anywhere else (a dev host, a plain ssh cluster, cloud VMs, or a host outside
  the allocation) it runs over ssh with a stdin lifeline: the remote side
  starts the command in its own session and, when the ssh connection's stdin
  closes (the caller was killed or stopped it), sends the whole session
  SIGTERM, waits the grace period, then SIGKILL. Launched programs therefore
  never read the caller's stdin; a ``stdin`` payload is passed as data.

PBS and LSF allocations use the ssh path (not yet verified on those systems).
The command, its working directory and its environment are passed as data
(``shlex``-quoted), never spliced into shell text.
"""

import itertools
import os
import shlex
import shutil
import signal
import subprocess
import threading
from typing import Dict, Optional

from .core_exec import CoreExec, LocalExec
from .exec_info import SshExecInfo, PsshExecInfo

KILL_GRACE_ENV = "JARVIS_KILL_GRACE"
_DEFAULT_KILL_GRACE = 10

# Run by the remote login shell as
#   bash -c '<this>' jarvis-lifeline '<command>' <grace seconds>
# fd 3 is ssh's stdin. The command runs in its own session ($p is its process
# group). The watcher waits for EOF on fd 3, then TERM, grace, KILL. If the
# command ends first, the watcher is dropped, unless EOF already arrived (the
# caller is gone), in which case it finishes the teardown.
LIFELINE = (
    'exec 3<&0 0</dev/null; '
    'setsid bash -c "$1" & p=$!; '
    '( while read -r _ <&3; do :; done; kill -TERM -- -$p; sleep "$2"; '
    'kill -KILL -- -$p ) >/dev/null 2>&1 & w=$!; '
    'wait $p; rc=$?; '
    'if read -t 0 <&3; then wait $w; else kill $w 2>/dev/null; fi; '
    'exit $rc'
)

_allocation_lock = threading.Lock()
_allocation_cache: Dict[tuple, Dict[str, str]] = {}
_step_counter = itertools.count()


def kill_grace() -> int:
    """Seconds between SIGTERM and SIGKILL when a launch is torn down."""
    try:
        return max(0, int(float(os.environ.get(KILL_GRACE_ENV, _DEFAULT_KILL_GRACE))))
    except ValueError:
        return _DEFAULT_KILL_GRACE


def allocation_nodes() -> Dict[str, str]:
    """Short host name to node name for the Slurm allocation this process is in.

    Empty outside an allocation, or when ``srun``/``scontrol`` are missing.
    """
    job = os.environ.get("SLURM_JOB_ID")
    nodelist = os.environ.get("SLURM_JOB_NODELIST") or os.environ.get("SLURM_NODELIST")
    if os.name == "nt" or not job or not nodelist:
        return {}
    key = (job, nodelist)
    with _allocation_lock:
        if key in _allocation_cache:
            return _allocation_cache[key]
        nodes: Dict[str, str] = {}
        if shutil.which("srun") and shutil.which("scontrol"):
            try:
                result = subprocess.run(
                    ["scontrol", "show", "hostnames", nodelist],
                    capture_output=True, text=True, timeout=30,
                    stdin=subprocess.DEVNULL,
                )
                if result.returncode == 0:
                    for name in result.stdout.split():
                        nodes[name.split(".")[0]] = name
            except (OSError, subprocess.SubprocessError):
                pass
        _allocation_cache[key] = nodes
        return nodes


def step_node(hostname: str, exec_info) -> Optional[str]:
    """The allocation node to run on as a job step, or None for ssh.

    Another ssh port or another user names a different sshd or account
    (for example a container's sshd), so those keep ssh.
    """
    if exec_info.port not in (None, 22):
        return None
    if exec_info.user and exec_info.user != _current_user():
        return None
    return allocation_nodes().get(str(hostname).split(".")[0])


def _current_user() -> Optional[str]:
    try:
        import getpass
        return getpass.getuser()
    except Exception:
        return None


def remote_command(cmd: str, exec_info, marker: str = "") -> str:
    """The bash text run on the target host: cwd, env and stdin as data."""
    parts = [marker] if marker else []
    if exec_info.cwd:
        parts.append(f"cd -- {shlex.quote(str(exec_info.cwd))} &&")
    if exec_info.stdin:
        parts.append(f"printf %s {shlex.quote(str(exec_info.stdin))} |")
    if exec_info.env:
        parts.append("env " + " ".join(
            shlex.quote(f"{key}={value}") for key, value in exec_info.env.items()
        ))
    if exec_info.sudo:
        parts.append("sudo -E" if exec_info.sudoenv and exec_info.env else "sudo")
    parts.append(f"bash -c {shlex.quote(cmd)}")
    return " ".join(parts)


class SshExec(LocalExec):
    """
    Execute a command on one host: a Slurm step inside an allocation, else ssh.
    Inherits from LocalExec to reuse subprocess management.
    """

    def __init__(self, cmd: str, exec_info: SshExecInfo, hostname: Optional[str] = None,
                 launch_dir=None, host_index: int = 0, record: bool = True):
        """
        Initialize remote execution.

        :param cmd: Command to execute remotely
        :param exec_info: SSH execution information
        :param hostname: Target hostname (if None, uses first host from hostfile)
        :param launch_dir: Provenance launch folder shared by a PsshExec
        :param host_index: This host's index within that launch
        :param record: Record this launch's provenance when no folder is given
        """
        self.original_cmd = cmd
        self.package_exec_info = exec_info
        self.target_hostname = hostname or (
            exec_info.hostfile.hosts[0] if exec_info.hostfile else "localhost"
        )
        self.step_node = step_node(self.target_hostname, exec_info)
        self.transport = "srun-step" if self.step_node else "ssh"

        if launch_dir is None and record and not exec_info.dry_run:
            from jarvis_cd.core.provenance import capture_host_launch
            launch_dir = capture_host_launch(
                "SshExec", cmd, [self.target_hostname], exec_info.env,
                {self.target_hostname: self.transport},
            )
        self.launch_dir = launch_dir
        marker = ""
        if launch_dir is not None:
            from jarvis_cd.core.provenance import host_marker
            marker = host_marker(launch_dir, host_index)

        inner = remote_command(cmd, exec_info, marker)
        if self.step_node:
            transport_cmd = self._build_step_command(inner)
        else:
            transport_cmd = self._build_ssh_command(inner, exec_info)
            self._hold_stdin = True

        # The transport (ssh or srun) runs in JARVIS's own environment; the
        # package's env reaches only the command, through ``env``.
        super().__init__(transport_cmd, exec_info.mod(env={}, stdin=None))

        # Override hostname for output tracking
        self.hostname = self.target_hostname
        for table in (self.stdout, self.stderr, self.exit_code, self.processes,
                      self.process_groups, self.windows_jobs, self.output_threads):
            if "localhost" in table and self.hostname != "localhost":
                table[self.hostname] = table.pop("localhost")

    def _build_step_command(self, inner: str) -> str:
        """``srun`` step on the target node of the current allocation."""
        self.job_id = os.environ["SLURM_JOB_ID"]
        self.step_name = f"jarvis-{os.getpid()}-{next(_step_counter)}"
        parts = ["srun", f"--jobid={self.job_id}", f"--job-name={self.step_name}",
                 "--overlap"]
        # No -c: the step uses the CPUs the job requested on that node, and
        # binding a one-task step would pin everything it starts.
        parts.extend(["-w", str(self.step_node), "-N1", "-n1", "--export=ALL",
                      "--input=none", "--quiet", "--mpi=none", "--cpu-bind=none",
                      "bash", "-c", inner])
        return "exec " + " ".join(shlex.quote(part) for part in parts)

    def _build_ssh_command(self, inner: str, exec_info: SshExecInfo) -> str:
        """
        Build the ssh command that runs ``inner`` under the stdin lifeline.

        :param inner: Remote bash text from :func:`remote_command`
        :param exec_info: SSH execution information
        :return: Complete SSH command string
        """
        # The base SSH launcher can be overridden at the pipeline-YAML
        # level via the top-level ``ssh_cmd`` key — useful e.g. to wrap
        # ssh in ``env -u LD_LIBRARY_PATH`` so a conda env's libcrypto
        # doesn't get loaded into the host's openssh.
        ssh_parts = exec_info.ssh_cmd.split() if exec_info.ssh_cmd else ["ssh"]

        # Agent and scheduler launches are non-interactive. Never let a
        # password or keyboard-interactive prompt retain the process tree, and
        # avoid multiplying connection retries behind the caller's timeout.
        ssh_parts.extend(["-o", "BatchMode=yes", "-o", "ConnectionAttempts=1"])
        if not exec_info.strict_ssh:
            ssh_parts.extend(
                ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null"]
            )
        if exec_info.port and exec_info.port != 22:
            ssh_parts.extend(["-p", str(exec_info.port)])
        if exec_info.pkey:
            ssh_parts.extend(["-i", shlex.quote(str(exec_info.pkey))])
        if exec_info.timeout:
            ssh_parts.extend(["-o", f"ConnectTimeout={exec_info.timeout}"])
        if exec_info.user:
            ssh_parts.append(f"{exec_info.user}@{self.target_hostname}")
        else:
            ssh_parts.append(str(self.target_hostname))

        remote = (f"bash -c {shlex.quote(LIFELINE)} jarvis-lifeline "
                  f"{shlex.quote(inner)} {kill_grace()}")
        ssh_parts.append(shlex.quote(remote))
        return "exec " + " ".join(ssh_parts)

    def kill(self, hostname: str = "localhost"):
        """Stop the launch: TERM, the grace period, then KILL.

        A signal to ``srun`` itself makes Slurm kill the step at once, so the
        step's processes get SIGTERM through ``scancel --signal`` first (every
        process in the step's cgroup), and ``srun`` is stopped only after the
        grace period. Over ssh the lifeline does the same on the remote host
        once ssh dies.
        """
        if hostname not in self.processes and hostname == "localhost":
            hostname = self.hostname
        process = self.processes.get(hostname)
        if (self.transport == "srun-step" and process is not None
                and os.name != "nt" and process.poll() is None):
            step_id = self._step_id()
            if step_id:
                subprocess.run(["scancel", "--signal=TERM", step_id],
                               capture_output=True, timeout=30, check=False)
                try:
                    process.wait(timeout=kill_grace())
                except subprocess.TimeoutExpired:
                    pass
            try:
                if process.poll() is None:
                    process.send_signal(signal.SIGTERM)
                    process.wait(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass
        super().kill(hostname)

    def _step_id(self) -> Optional[str]:
        """``<job>.<step>`` of this launch's step, found by its name."""
        try:
            listed = subprocess.run(
                ["squeue", "-h", "-s", "-j", self.job_id, "-o", "%i %j"],
                capture_output=True, text=True, timeout=30, check=False,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        for line in listed.splitlines():
            fields = line.split()
            if len(fields) == 2 and fields[1] == self.step_name:
                return fields[0]
        return None


class PsshExec(CoreExec):
    """
    Execute commands on multiple hosts in parallel (one SshExec per host).
    """

    def __init__(self, cmd: str, exec_info: PsshExecInfo):
        """
        Initialize parallel execution.

        :param cmd: Command to execute on all hosts
        :param exec_info: PSSH execution information
        """
        super().__init__()
        self.cmd = cmd
        self.exec_info = exec_info
        self.ssh_executors = {}

        if not exec_info.hostfile or len(exec_info.hostfile) == 0:
            raise ValueError("PSSH requires a hostfile with at least one host")

        # Start execution on each host
        self.run()

        # If not async, wait for all to complete
        if not exec_info.exec_async:
            self.wait_all()

    def _ssh_info(self) -> SshExecInfo:
        info = self.exec_info
        return SshExecInfo(
            user=info.user,
            pkey=info.pkey,
            port=info.port,
            env=info.env,
            sudo=info.sudo,
            sudoenv=info.sudoenv,
            cwd=info.cwd,
            stdin=info.stdin,
            collect_output=info.collect_output,
            pipe_stdout=info.pipe_stdout,
            pipe_stderr=info.pipe_stderr,
            hide_output=info.hide_output,
            exec_async=True,  # Always async for parallel execution
            strict_ssh=info.strict_ssh,
            timeout=info.timeout,
            dry_run=info.dry_run,
            # Forward the pipeline-level ssh launcher override down
            # to each per-host SshExec.
            ssh_cmd=info.ssh_cmd,
        )

    def run(self):
        """Execute command on all hosts in parallel"""
        hosts = list(self.exec_info.hostfile.hosts) if self.exec_info.hostfile else []
        ssh_info = self._ssh_info()
        launch_dir = None
        if not self.exec_info.dry_run:
            from jarvis_cd.core.provenance import capture_host_launch
            launch_dir = capture_host_launch(
                "PsshExec", self.cmd, hosts, self.exec_info.env,
                {host: "srun-step" if step_node(host, ssh_info) else "ssh"
                 for host in hosts},
            )

        threads = []
        for index, hostname in enumerate(hosts):
            thread = threading.Thread(
                target=self._execute_on_host,
                args=(hostname, ssh_info, launch_dir, index),
            )
            thread.daemon = True
            thread.start()
            threads.append(thread)

        # Wait for all threads to start their processes
        for thread in threads:
            thread.join()

    def _execute_on_host(self, hostname: str, ssh_info: SshExecInfo,
                         launch_dir=None, index: int = 0):
        """
        Execute command on a specific host.

        :param hostname: Target hostname
        :param ssh_info: SSH execution information
        :param launch_dir: The launch's provenance folder, if recorded
        :param index: The host's index within the launch
        """
        try:
            ssh_exec = SshExec(self.cmd, ssh_info, hostname, launch_dir=launch_dir,
                               host_index=index, record=False)
            self.ssh_executors[hostname] = ssh_exec

            # Copy process reference for management
            if hostname in ssh_exec.processes:
                self.processes[hostname] = ssh_exec.processes[hostname]
            if hostname in ssh_exec.process_groups:
                self.process_groups[hostname] = ssh_exec.process_groups[hostname]
            if hostname in ssh_exec.windows_jobs:
                self.windows_jobs[hostname] = ssh_exec.windows_jobs[hostname]

        except Exception as e:
            print(f"Error executing on {hostname}: {e}")
            self.exit_code[hostname] = 1
            self.stdout[hostname] = ""
            self.stderr[hostname] = str(e)

    def wait(self, hostname: str) -> int:
        """
        Wait for execution on a specific host to complete.

        :param hostname: Hostname to wait for
        :return: Exit code
        """
        if hostname in self.ssh_executors:
            ssh_exec = self.ssh_executors[hostname]
            exit_code = ssh_exec.wait(hostname)

            # Copy outputs
            self.stdout[hostname] = ssh_exec.stdout.get(hostname, "")
            self.stderr[hostname] = ssh_exec.stderr.get(hostname, "")
            self.exit_code[hostname] = exit_code
            self.windows_jobs.pop(hostname, None)

            return exit_code
        return 0

    def kill(self, hostname: str = "localhost"):
        """Stop one host's launch through its SshExec (TERM, grace, KILL)."""
        if hostname in self.ssh_executors:
            self.ssh_executors[hostname].kill(hostname)
        else:
            super().kill(hostname)

    def kill_all(self):
        """Stop every host's launch in parallel, so the grace periods overlap."""
        threads = [threading.Thread(target=self.kill, args=(host,), daemon=True)
                   for host in list(self.ssh_executors)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    def get_cmd(self) -> str:
        """Get the original command"""
        return self.cmd
