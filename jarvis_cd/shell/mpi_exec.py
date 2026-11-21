"""
MPI execution classes for Jarvis shell execution.
"""
from abc import abstractmethod
from typing import Union, List, Dict, Any
from .core_exec import LocalExec
from .exec_info import ExecInfo, MpiExecInfo, ExecType
from ..util.hostfile import Hostfile


class LocalMpiExec(LocalExec):
    """
    Base class used by all MPI implementations.
    """

    def __init__(self, cmd: Union[str, List[Dict[str, Any]]], exec_info: MpiExecInfo):
        """
        Initialize MPI execution.

        :param cmd: Command to execute with MPI. Can be:
                   - A string: single command
                   - A list of dicts: multiple commands with format:
                     [{'cmd': str, 'nprocs': int, 'disable_preload': bool (optional)}, ...]
                     If 'disable_preload' is True, LD_PRELOAD will be removed for that command
        :param exec_info: MPI execution information
        """
        self.nprocs = exec_info.nprocs
        self.ppn = exec_info.ppn
        self.hostfile = exec_info.hostfile or Hostfile(hosts=['localhost'])
        self.mpi_env = exec_info.env
        self.ssh_port = exec_info.port if exec_info.port else None
        # Pipeline-YAML-level override for the MPI launcher. Defaults
        # to None -> subclass picks "mpiexec".
        self.mpi_cmd = getattr(exec_info, 'mpi_cmd', None)
        # The MPI the Spack installer chose (JARVIS_MPI_PREFIX) supplies
        # the launcher when the pipeline YAML sets no mpi_cmd, so a run
        # never picks up a different mpiexec from PATH.
        self.mpi_prefix = (self.mpi_env or {}).get('JARVIS_MPI_PREFIX') or None
        if not self.mpi_cmd and self.mpi_prefix:
            import os
            launcher = os.path.join(self.mpi_prefix, 'bin', 'mpiexec')
            if os.path.isfile(launcher):
                self.mpi_cmd = launcher
        # Pipeline-YAML-level override for the rsh agent that OpenMPI's
        # prted / MPICH's hydra_pmi_proxy use to spawn remote ranks.
        # Same use case as ssh_cmd (wrap ssh in env -u LD_LIBRARY_PATH
        # so the host openssh links the right libcrypto).
        self.ssh_cmd = getattr(exec_info, 'ssh_cmd', None)

        # Process command format
        if isinstance(cmd, str):
            # Single command format
            self.original_cmd = cmd
            self.cmd_list = None
        else:
            # Multi-command format: list of dicts
            self.cmd_list = self._process_cmd_list(cmd)
            self.original_cmd = None

        # Inside a JARVIS execution, record the launch (launcher and binary
        # realpaths, ldd) and wrap ranks for hostname and /proc maps capture.
        launcher_env = dict(exec_info.basic_env or {})
        self.network = self._apply_launch_network(launcher_env)
        # The site-fact advisories of this launch, on the object, so a
        # package that records its launch (e.g. a service's launch file)
        # carries them top-level, as jarvis's launch.json does.
        site_facts = (self.network or {}).get('site_facts') if isinstance(
            self.network, dict) else None
        self.site_facts = site_facts if isinstance(site_facts, dict) else None
        self.advisories = list((self.network or {}).get('advisories') or []) \
            if isinstance(self.network, dict) else []
        self.launch_json = None
        self.placement = self._resolve_placement()

        from ..core.provenance import capture_launch
        capture_launch(self, cwd=getattr(exec_info, 'cwd', None))

        # Build MPI command
        mpi_cmd = self.mpicmd()

        # Create modified exec_info for LocalExec
        local_info = exec_info.mod(
            env=launcher_env
        )

        super().__init__(mpi_cmd, local_info)

    #: resource-graph network flavour of this launcher (see util.network)
    network_flavour = None

    LAUNCH_RECORD_SCHEMA = 'jarvis.mpi-launch.v1'

    def launch_record(self):
        """A JSON-ready summary of this launch for a package to store.

        Packages that write their own launch file (e.g. a service's
        ``<name>-launch.json``) should dump this rather than pick attributes,
        so the site-fact ``advisories`` stay top-level. jarvis's own record
        is ``launch_json`` (``provenance/launch-NNN/launch.json``).
        """
        hostfile = getattr(self, 'hostfile', None)
        return {
            'schema_version': self.LAUNCH_RECORD_SCHEMA,
            'built_by': f"{type(self).__module__}.{type(self).__name__}",
            'command': getattr(self, 'cmd', None),
            'nprocs': self.nprocs,
            'ppn': self.ppn,
            'hosts': list(getattr(hostfile, 'hosts', []) or []),
            'network': self.network,
            'advisories': list(self.advisories or []),
            'site_facts': self.site_facts,
            'placement': self.placement,
            'launch_json': self.launch_json,
        }

    def _resolve_placement(self):
        """Bound ranks per node by the allocation's task geometry.

        Inside an allocation the scheduler's tasks per node (else the
        physical cores) win over a package ``ppn`` that would not fit or
        would not place every rank; see ``jarvis_cd.util.placement``.
        """
        import os
        import sys
        from ..util.placement import resolve_ppn

        environ = dict(os.environ)
        if (self.mpi_env or {}).get('JARVIS_MPI_PLACEMENT'):
            environ['JARVIS_MPI_PLACEMENT'] = self.mpi_env['JARVIS_MPI_PLACEMENT']
        ppn, record = resolve_ppn(
            self.nprocs, self.ppn, list(self.hostfile.hosts), environ
        )
        if record and record.get('adjusted'):
            print(f"jarvis: ppn {self.ppn} -> {ppn} ({record['reason']})",
                  file=sys.stderr)
        self.ppn = ppn
        return record

    def _apply_launch_network(self, launcher_env: Dict[str, Any]):
        """Apply the resource graph's data/control network to this launch.

        The graph's ``net`` section (or a person's override in it) picks the
        networks; the flavour turns them into its own settings. Settings the
        pipeline already made win, and ``JARVIS_MPI_NETWORK=off`` disables
        it.

        A launch that crosses nodes never goes out blind: when the graph has
        no net section, misses some launch hosts, or predates UCX
        verification, the net facts are collected on the launch hosts first
        (read-only, inside the allocation) and saved as the graph's net
        section. A collection that misses a host with no facts, or a
        ``block`` site fact in the fresh facts, refuses the launch with a
        typed issue.
        """
        import os
        from ..core.issues import JarvisIssueError
        from ..util import network as net_util

        self.network_args = {}
        if not self.network_flavour or net_util.network_disabled(
            self.mpi_env, os.environ
        ):
            return None
        hosts = list(self.hostfile.hosts)
        collected = None
        record = None
        try:
            net = net_util.load_net_section()
            if self.network_flavour != 'cray':
                collected = self._collect_launch_net(net, hosts)
                if collected and collected.get('net'):
                    net = collected['net']
            choice = net_util.resolve_launch_network(net, hosts)
            if choice:
                # Never mutate the package's env dict; ranks get a copy.
                self.mpi_env = dict(self.mpi_env or {})
                record = net_util.apply_launch_network(
                    self.network_flavour,
                    choice,
                    launcher_env=launcher_env,
                    rank_env=self.mpi_env,
                )
            elif collected and collected.get('net'):
                # Fresh facts with no usable network: the gate below decides.
                record = {
                    'schema_version': net_util.LAUNCH_NETWORK_SCHEMA,
                    'data': None, 'control': None,
                    'hosts': sorted({net_util._short(h) for h in hosts}),
                    'hosts_not_in_graph': [],
                    'flavour': self.network_flavour,
                    'applied': {'launcher_env': {}, 'rank_env': {}, 'args': {}},
                    'kept_from_caller': {},
                }
        except JarvisIssueError:
            raise
        except Exception as error:  # never block a launch on graph errors
            failed = {'error': str(error)}
            if collected:
                failed['collected_at_launch'] = collected.get('summary')
            return failed
        if collected:
            record = dict(record or {}, collected_at_launch=collected.get('summary'))
        if not record:
            return None
        self.network_args = dict((record or {}).get('applied', {}).get('args', {}))
        gate = bool(collected and collected.get('net'))
        report = None
        try:
            from ..core.site_facts import launch_site_facts

            # Facts the pre-launch check already saw are advisory here; facts
            # collected by this launch pass the same block rules as a check.
            report = launch_site_facts(self.network_flavour, net, record, collected=gate)
            if report:
                # Recorded even with no issues: the launch's evaluation on its
                # hosts supersedes the check's graph-wide prediction.
                record['site_facts'] = report
            from ..core.site_facts import launch_advisories

            record['advisories'] = launch_advisories(report)
        except Exception as error:
            record['site_facts'] = {'error': str(error)[:500]}
        if gate and report and report.get('blocking'):
            from ..core.site_facts import EXIT_SITE_FACT

            blocking = []
            for issue in report['issues']:
                if issue.get('blocking'):
                    issue = dict(issue)
                    issue['details'] = dict(
                        issue.get('details') or {},
                        collected_at_launch=record.get('collected_at_launch'))
                    blocking.append(issue)
            raise JarvisIssueError(
                blocking,
                exit_code=EXIT_SITE_FACT,
                document={'site_facts': report,
                          'collected_at_launch': record.get('collected_at_launch')},
            )
        return record

    def _collect_launch_net(self, net, hosts):
        """Collect the launch hosts' net facts when the graph lacks them.

        Returns None when the graph covers the launch, else
        ``{"summary": ..., "net": <fresh net section or None>}``.
        """
        import time
        from ..core.issues import JarvisIssueError
        from ..util import network as net_util

        if not net_util.launch_crosses_network(hosts):
            return None
        reasons = net_util.net_collect_reasons(net, hosts=hosts)
        if not reasons:
            return None
        from ..core.rg_collector import collect_failure_issue, collect_net_facts

        unique = list(dict.fromkeys(hosts))
        started = time.time()
        try:
            result = collect_net_facts(unique, ssh_port=self.ssh_port)
        except Exception as error:  # a launch that needs facts never goes blind
            result = {'facts': {}, 'failures': {
                host: {'host': host, 'via': None, 'exit_code': None, 'stderr': '',
                       'reason': f'error running the collector: {error}'}
                for host in unique}}
        facts, failures = result['facts'], result['failures']
        summary = {
            'reasons': reasons,
            'hosts': unique,
            'collected': sorted(facts),
            'failed': sorted(failures),
            'seconds': round(time.time() - started, 3),
        }
        if failures:
            if reasons == ['graph_predates_ucx_verification']:
                # Every host has facts already; only the UCX pin waits.
                summary['fallback'] = ('the stored graph; its UCX device stays '
                                       'withheld (cause unverified_graph)')
                summary['failures'] = [failures[h] for h in sorted(failures)]
                return {'summary': summary, 'net': None}
            raise JarvisIssueError(
                [collect_failure_issue(unique, sorted(facts),
                                       [failures[h] for h in sorted(failures)],
                                       stage='launch')],
                document={'collected_at_launch': summary},
            )
        try:
            fresh = net_util.analyze_net(facts, override=(net or {}).get('override'))
        except Exception as error:
            from ..core.issues import make_jarvis_issue

            raise JarvisIssueError([make_jarvis_issue(
                kind='resource_graph_net_invalid',
                subject='resource graph net',
                message=('The net facts collected on the launch hosts could not be '
                         f'analyzed ({error}); the MPI launch was refused rather '
                         'than sent out with no network.'),
                identity={'hosts': sorted(unique)},
                field='resource_graph.net',
                details={'stage': 'launch', 'error': str(error)[:500],
                         'collected_at_launch': summary},
            )]) from error
        # Merge per host into the stored graph (other hosts' facts and the
        # person's override stay); the launch uses the merged selection.
        persisted = net_util.persist_net_section(fresh, facts=facts)
        merged = persisted.pop('net', None)
        if merged is None:
            try:
                merged, persisted['merge'] = net_util.merge_net_facts(
                    net, facts, override=(net or {}).get('override'))
            except Exception:
                merged = fresh
        summary['persisted'] = persisted
        summary['selected'] = {
            role: (choice or {}).get('cidr')
            for role, choice in (merged.get('selected') or {}).items()
        }
        return {'summary': summary, 'net': merged}

    def _process_cmd_list(self, cmd_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Process list of command dicts, calculating final nprocs for each.

        :param cmd_list: List of dicts with 'cmd', 'nprocs', and optional 'disable_preload' keys
        :return: Processed list with calculated nprocs and environment settings
        """
        if not cmd_list:
            raise ValueError("Command list cannot be empty")

        processed = []
        total_nprocs_allocated = 0

        # Process all but the last command
        for i, cmd_dict in enumerate(cmd_list[:-1]):
            nprocs = cmd_dict.get('nprocs', 0)
            processed.append({
                'cmd': cmd_dict['cmd'],
                'nprocs': nprocs,
                'disable_preload': cmd_dict.get('disable_preload', False)
            })
            total_nprocs_allocated += nprocs

        # Process last command - calculate remaining nprocs
        last_cmd = cmd_list[-1]
        remaining_nprocs = self.nprocs - total_nprocs_allocated

        if remaining_nprocs < 0:
            raise ValueError(f"Total nprocs ({total_nprocs_allocated}) exceeds available ({self.nprocs})")

        processed.append({
            'cmd': last_cmd['cmd'],
            'nprocs': remaining_nprocs,
            'disable_preload': last_cmd.get('disable_preload', False)
        })

        return processed

    @abstractmethod
    def mpicmd(self) -> str:
        """Build MPI command. Must be implemented by subclasses."""
        pass


class OpenMpiExec(LocalMpiExec):
    """
    Execute commands using OpenMPI.
    """

    network_flavour = 'openmpi'

    def mpicmd(self) -> str:
        """Build OpenMPI command"""
        params = self.mpi_cmd.split() if self.mpi_cmd else ['mpiexec']
        params.append('--oversubscribe')
        params.append('--allow-run-as-root')  # For docker
        # Forward the pipeline-level ssh_cmd to OpenMPI's rsh agent
        # so prted spawns remote daemons via the wrapped ssh.
        if self.ssh_cmd:
            params.append(f'--mca plm_rsh_agent "{self.ssh_cmd}"')

        # --prefix lets remote nodes (daemons started over ssh, as with an
        # Open MPI built without Slurm) find prted/orted: the chosen MPI's
        # prefix, else derived from PATH.
        env_path = self.mpi_env.get('PATH', '')
        if self.mpi_prefix:
            params.append(f'--prefix {self.mpi_prefix}')
        elif env_path:
            import os
            for path_dir in env_path.split(os.pathsep):
                if os.path.isfile(os.path.join(path_dir, 'prted')):
                    prefix = os.path.dirname(path_dir)
                    params.append(f'--prefix {prefix}')
                    break

        # Set SSH port if explicitly specified (SSH config will be used otherwise)
        if self.ssh_port and self.ssh_port != 22:
            params.append(f'--mca plm_rsh_args "-p {self.ssh_port}"')


        if self.ppn is not None:
            params.append(f'-npernode {self.ppn}')

        if len(self.hostfile):
            if self.hostfile.path is None:
                params.append(f'--host {",".join(self.hostfile.hosts)}')
            else:
                params.append(f'--hostfile {self.hostfile.path}')

        # Handle multi-command format
        if self.cmd_list:
            cmd_parts = []
            for cmd_dict in self.cmd_list:
                nprocs = cmd_dict['nprocs']
                cmd = cmd_dict['cmd']
                disable_preload = cmd_dict.get('disable_preload', False)

                # Skip commands with 0 nprocs
                if nprocs > 0:
                    # Build per-command environment variables
                    cmd_env = self.mpi_env.copy()
                    if disable_preload and 'LD_PRELOAD' in cmd_env:
                        del cmd_env['LD_PRELOAD']

                    # Add environment variables for this command
                    env_args = ' '.join([f'-x {key}="{val}"' for key, val in cmd_env.items()])
                    cmd_parts.append(f'{env_args} -n {nprocs} {cmd}')
            # Join with ' : ' for multiple commands
            params.append(' : '.join(cmd_parts))
        else:
            # Single command format - add global environment variables
            params.extend([f'-x {key}="{val}"' for key, val in self.mpi_env.items()])
            params.append(f'-n {self.nprocs}')
            params.append(self.original_cmd)

        return ' '.join(params)


class MpichExec(LocalMpiExec):
    """
    Execute commands using MPICH.
    """

    network_flavour = 'mpich'

    def mpicmd(self) -> str:
        """Build MPICH command"""
        params = self.mpi_cmd.split() if self.mpi_cmd else ['mpiexec']

        # Hydra's -iface from the resource graph's control network.
        for flag, value in getattr(self, 'network_args', {}).items():
            params.append(f'{flag} {value}')

        # Forward the pipeline-level ssh_cmd as the bootstrap exec
        # for MPICH/hydra (default is ssh).
        if self.ssh_cmd:
            params.append(f'-bootstrap-exec "{self.ssh_cmd}"')

        # Set SSH port if explicitly specified (SSH config will be used otherwise)
        if self.ssh_port and self.ssh_port != 22:
            params.append(f'-bootstrap-exec-args "-p {self.ssh_port}"')

        if self.ppn is not None:
            params.append(f'-ppn {self.ppn}')

        if len(self.hostfile):
            if self.hostfile.path is None:
                params.append(f'--host {",".join(self.hostfile.hosts)}')
            else:
                params.append(f'--hostfile {self.hostfile.path}')

        # Handle multi-command format
        if self.cmd_list:
            cmd_parts = []
            for cmd_dict in self.cmd_list:
                nprocs = cmd_dict['nprocs']
                cmd = cmd_dict['cmd']
                disable_preload = cmd_dict.get('disable_preload', False)

                # Skip commands with 0 nprocs
                if nprocs > 0:
                    # Build per-command environment variables
                    cmd_env = self.mpi_env.copy()
                    if disable_preload and 'LD_PRELOAD' in cmd_env:
                        del cmd_env['LD_PRELOAD']

                    # Add environment variables for this command
                    env_args = ' '.join([f'-env {key}="{val}"' for key, val in cmd_env.items()])
                    cmd_parts.append(f'{env_args} -n {nprocs} {cmd}')
            # Join with ' : ' for multiple commands
            params.append(' : '.join(cmd_parts))
        else:
            # Single command format - add global environment variables
            params.extend([f'-genv {key}="{val}"' for key, val in self.mpi_env.items()])
            params.append(f'-n {self.nprocs}')
            params.append(self.original_cmd)

        return ' '.join(params)


class IntelMpiExec(MpichExec):
    """
    Execute commands using Intel MPI (similar to MPICH).
    """
    network_flavour = 'intel'


class CrayMpichExec(LocalMpiExec):
    """
    Execute commands using Cray MPICH.
    """

    network_flavour = 'cray'

    def mpicmd(self) -> str:
        """Build Cray MPICH command"""
        params = self.mpi_cmd.split() if self.mpi_cmd else ['mpiexec']

        if self.ppn is not None:
            params.append(f'--ppn {self.ppn}')

        if len(self.hostfile):
            if (self.hostfile.hosts[0] == 'localhost' and
                len(self.hostfile) == 1):
                pass  # Skip hostfile for localhost-only
            elif self.hostfile.path is None:
                params.append(f'--hosts {",".join(self.hostfile.hosts)}')
            else:
                params.append(f'--hostfile {self.hostfile.path}')

        # Handle multi-command format
        if self.cmd_list:
            cmd_parts = []
            for cmd_dict in self.cmd_list:
                nprocs = cmd_dict['nprocs']
                cmd = cmd_dict['cmd']
                disable_preload = cmd_dict.get('disable_preload', False)

                # Skip commands with 0 nprocs
                if nprocs > 0:
                    # Build per-command environment variables
                    cmd_env = self.mpi_env.copy()
                    if disable_preload and 'LD_PRELOAD' in cmd_env:
                        del cmd_env['LD_PRELOAD']

                    # Add environment variables for this command
                    env_args = ' '.join([f'--env {key}="{val}"' for key, val in cmd_env.items()])
                    cmd_parts.append(f'{env_args} -n {nprocs} {cmd}')
            # Join with ' : ' for multiple commands
            params.append(' : '.join(cmd_parts))
        else:
            # Single command format - add global environment variables
            params.extend([f'--env {key}="{val}"' for key, val in self.mpi_env.items()])
            params.append(f'-n {self.nprocs}')
            params.append(self.original_cmd)

        return ' '.join(params)


