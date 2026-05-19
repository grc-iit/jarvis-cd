"""
This module provides classes and methods to launch the PyFLEXTRKR application.
PyFLEXTRKR is an atmospheric feature tracking framework.
"""
from jarvis_cd.core.pkg import Application
from jarvis_cd.shell import Exec, LocalExecInfo, MpiExecInfo, PsshExecInfo
from jarvis_cd.shell.process import Mkdir, Rm
import os
import time
import pathlib
import yaml


class Pyflextrkr(Application):
    """
    PyFLEXTRKR supporting both default (bare-metal) and container deployment.

    Container mode builds a venv at /opt/pyflextrkr-env with PyFLEXTRKR
    installed editable from /opt/PyFLEXTRKR and runs the demo wrappers at
    /opt/run_demo.sh / /opt/run_demo_multinode.sh (matching the apps-repo
    Dockerfile).

    Default (bare-metal) mode still drives a user-installed conda env +
    checkout and writes a run-specific YAML config from the bundled
    _template.yml — useful for Hermes/VFD experiments on Ares.
    """

    def _init(self):
        """
        Initialize paths
        """
        self.pkg_type = 'pyflextrkr'
        self.hermes_env_vars = ['HERMES_ADAPTER_MODE', 'HERMES_CLIENT_CONF',
                                'HERMES_CONF', 'LD_PRELOAD']

    def _configure_menu(self):
        """
        Create a CLI menu for the configurator method.
        For thorough documentation of these parameters, view:
        https://github.com/scs-lab/jarvis-util/wiki/3.-Argument-Parsing

        :return: List(dict)
        """

        return [
            {
                'name': 'base_image',
                'msg': 'Base Docker image for container build',
                'type': str,
                'default': 'sci-hpc-base',
            },
            {
                'name': 'demo',
                'msg': 'Demo to run in container mode',
                'type': str,
                'choices': ['mcs_tbpf', 'mcs_tbpf_multinode',
                            'mcs_imerg', 'mcs_imerg_mcsmip',
                            'mcs_wrf_tbpf', 'mcs_model25km',
                            'mcs_wrf_tbradar', 'mcs_gridrad',
                            'mcs_himawari', 'cell_nexrad', 'cell_csapr',
                            'generic_tracking'],
                'default': 'mcs_tbpf',
            },
            {
                'name': 'out',
                'msg': 'Container path to stage final results into '
                       '(post-run cp from /output/pyflextrkr-data). '
                       'Leave blank to keep results in the container only.',
                'type': str,
                'default': '',
            },
            {
                'name': 'nprocs',
                'msg': 'Number of MPI processes (container multinode mode)',
                'type': int,
                'default': 1,
            },
            {
                'name': 'ppn',
                'msg': 'Processes per node',
                'type': int,
                'default': 1,
            },
            {
                'name': 'data_root',
                'msg': ('Container path where the demo writes its '
                        'tar-extracted inputs, per-stage intermediates '
                        '(features, tracks, MCS tracks), and final '
                        'stats. Default /output/pyflextrkr-data keeps '
                        'everything on the container\'s writable layer; '
                        'point at a FUSE mount (e.g. '
                        '/mnt/dumbwarp/pyflextrkr-data) to route every '
                        'stage R/W through that adapter.'),
                'type': str,
                'default': '/output/pyflextrkr-data',
            },
            {
                'name': 'replicates',
                'msg': ('Run the chosen demo this many times back-to-back '
                        'in one container exec, each into '
                        'rep_NNN-<demo> under the data root. Use this to '
                        'amplify total I/O when one pass through the '
                        'sample dataset is too small.'),
                'type': int,
                'default': 1,
            },
            {
                'name': 'parallel_reps',
                'msg': ('Per-host replicate concurrency for the single-'
                        'node demo path. When > 1, the replicates loop '
                        'fans across hosts via PsshExecInfo and each host '
                        'runs this many in parallel using `wait -n` '
                        'batching. The mcs_tbpf_multinode path uses '
                        'MpiExecInfo and ignores this knob (MPI handles '
                        'cross-rank parallelism).'),
                'type': int,
                'default': 1,
            },
            {
                'name': 'omp_threads',
                'msg': ('OMP_NUM_THREADS for each parallel replicate. '
                        '0 = unset (Dask local-cluster auto-detects).'),
                'type': int,
                'default': 0,
            },
            {
                'name': 'conda_env',
                'msg': 'Name of the conda environment for running Pyflextrkr (bare-metal only)',
                'type': str,
                'default': 'flextrkr',
            },
            {
                'name': 'config',
                'msg': 'The config file for running analysis (bare-metal only)',
                'type': str,
                'default': None,
            },
            {
                'name': 'runscript',
                'msg': 'The name of the Pyflextrkr script to run (bare-metal only)',
                'type': str,
                'default': 'run_mcs_tbpfradar3d_wrf',
                'choices': ['run_mcs_tbpfradar3d_wrf',
                            'run_mcs_tbpf_saag_summer_sam',
                            'run_mcs_tb_summer_sam'],
            },
            {
                'name': 'flush_mem',
                'msg': 'Flushing the memory after each stage (bare-metal only)',
                'type': bool,
                'default': False,
            },
            {
                'name': 'flush_mem_cmd',
                'msg': 'Command to flush the node memory (bare-metal only)',
                'type': str,
                'default': 'ml user-scripts; sudo drop_caches',
            },
            {
                'name': 'pyflextrkr_path',
                'msg': ('Absolute path to the Pyflextrkr source code '
                        '(bare-metal only; container mode uses /opt/PyFLEXTRKR)'),
                'type': str,
                'default': None,
            },
            {
                'name': 'experiment_input_path',
                'msg': 'Absolute path to the experiment run input and output files (bare-metal only)',
                'type': str,
                'default': None,
            },
            {
                'name': 'run_parallel',
                'msg': 'Parallel mode for Pyflextrkr: 0 (serial), 1 (local cluster), 2 (Dask MPI)',
                'type': int,
                'default': 1,
                'choices': [0, 1, 2],
            },
            {
                'name': 'nprocesses',
                'msg': 'Number of processes to run in parallel',
                'type': int,
                'default': 8,
            },
            {
                'name': 'run_cmd',  # internal; populated in _construct_cmd
                'msg': 'Command to run Pyflextrkr',
                'type': str,
                'default': None,
            },
            {
                'name': 'local_exp_dir',
                'msg': 'Local experiment directory (bare-metal only)',
                'type': str,
                'default': None,
            },
            {
                'name': 'with_hermes',
                'msg': 'Whether it is used with Hermes (bare-metal only)',
                'type': bool,
                'default': False,
            },
        ]

    # ------------------------------------------------------------------
    # Container Dockerfile generators
    # ------------------------------------------------------------------

    def _build_phase(self):
        if self.config.get('deploy_mode') != 'container':
            return None
        content = self._read_build_script('build.sh', {
            'BASE_IMAGE': self.config.get('base_image', 'sci-hpc-base'),
        })
        return content, 'default'

    def _build_deploy_phase(self):
        if self.config.get('deploy_mode') != 'container':
            return None
        suffix = getattr(self, '_build_suffix', '')
        content = self._read_dockerfile('Dockerfile.deploy', {
            'BUILD_IMAGE': self.build_image_name(),
            'DEPLOY_BASE': 'ubuntu:24.04',
        })
        return content, suffix

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def _configure(self, **kwargs):
        """
        Converts the Jarvis configuration to application-specific configuration.

        In container mode nothing extra is required — the image already
        contains PyFLEXTRKR at /opt/PyFLEXTRKR and the demo wrappers at
        /opt/run_demo*.sh; `start` just invokes one of them.

        :param kwargs: Configuration parameters for this pkg.
        :return: None
        """

        # Container mode runs the self-contained demo scripts baked into
        # the image — none of the bare-metal conda/scspkg/EXPERIMENT_INPUT_PATH
        # machinery below applies, and requiring it would make container
        # runs fail on host-side path checks.
        if self.config.get('deploy_mode') == 'container':
            return

        # ----- bare-metal path (conda env + source tree on host) -----
        experiment_input_path = os.getenv('EXPERIMENT_INPUT_PATH')
        if experiment_input_path is None:
            raise Exception('Must set the EXPERIMENT_INPUT_PATH environment variable')
        self.config['experiment_input_path'] = experiment_input_path

        # update config file every time
        self.config['config'] = (
            f"{self.pkg_dir}/example_config/{self.config['runscript']}_template.yml")

        if self.config.get('pyflextrkr_path') is None:
            raise Exception('Must set `pyflextrkr_path` to the Pyflextrkr source code')
        if not pathlib.Path(self.config['pyflextrkr_path']).exists():
            raise Exception(
                f"`pyflextrkr_path` {self.config['pyflextrkr_path']} does not exist.")

        if self.config['conda_env'] is None:
            raise Exception('Must set the conda environment for running Pyflextrkr')

        if self.config['runscript'] is None:
            raise Exception('Must set the Pyflextrkr script to run')

        # Check that run script matches config file
        if self.config['runscript'] not in self.config['config']:
            raise Exception(
                f"Run script {self.config['runscript']} does not match "
                f"config file {self.config['config']}")

        # strip any .py extension from runscript
        script_name = self.config['runscript'].split('/')[-1]
        if script_name.endswith('.py'):
            script_name = script_name[:-3]
        self.config['runscript'] = script_name

        if not pathlib.Path(self.config['config']).exists():
            raise Exception(f"File {self.config['config']} does not exist.")

        if self.config['flush_mem']:
            self.env['FLUSH_MEM'] = 'TRUE'
            if self.config['flush_mem_cmd'] is None:
                raise Exception('Must add the command to flush memory using flush_mem_cmd')
        else:
            self.env['FLUSH_MEM'] = 'FALSE'

    def _configure_yaml(self):
        self.env['HDF5_USE_FILE_LOCKING'] = 'FALSE'

        yaml_file = self.config['config']

        if '_template.yml' not in str(yaml_file):
            yaml_file = yaml_file.replace('.yml', '_template.yml')

        self.log(f'Pyflextrkr config from: {yaml_file}')

        with open(yaml_file, 'r') as stream:

            experiment_input_path = self.config['experiment_input_path']
            if self.config['local_exp_dir'] is not None:
                experiment_input_path = self.config['local_exp_dir']

            input_path = f"{experiment_input_path}/{self.config['runscript']}/"
            output_path = f"{experiment_input_path}/output_data/{self.config['runscript']}/"

            if not pathlib.Path(input_path).exists():
                raise Exception(f'Input path {input_path} does not exist.')
            if len(os.listdir(input_path)) == 0:
                raise Exception(f'Input path {input_path} is empty.')

            pathlib.Path(output_path).mkdir(parents=True, exist_ok=True)

            try:
                config_vars = yaml.safe_load(stream)

                config_vars['dask_tmp_dir'] = '/tmp/pyflextrkr_test'
                pathlib.Path(config_vars['dask_tmp_dir']).mkdir(parents=True, exist_ok=True)

                config_vars['clouddata_path'] = str(input_path)
                config_vars['root_path'] = str(output_path)

                config_vars['run_parallel'] = self.config['run_parallel']

                if self.config['run_parallel'] == 0 and self.config['nprocesses'] > 1:
                    self.log('WARNING: run_parallel is 0 (serial) nprocesses is set to 1')
                    self.config['nprocesses'] = 1
                config_vars['nprocesses'] = self.config['nprocesses']

                if self.config['nprocesses'] < config_vars['nprocesses']:
                    self.log(
                        f"WARNING: nprocesses is less than config file, set to {config_vars['nprocesses']}")
                    self.config['nprocesses'] = config_vars['nprocesses']

                if 'landmask_filename' in config_vars:
                    org_path = config_vars['landmask_filename']
                    landmask_path = org_path.replace('INPUT_DIR/', input_path)
                    landmask_path = landmask_path.replace("'", '')
                    if pathlib.Path(landmask_path).exists():
                        config_vars['landmask_filename'] = str(landmask_path)
                    else:
                        raise Exception(f'File {landmask_path} does not exist.')

                new_yaml_file = yaml_file.replace('_template.yml', '.yml')
                yaml.dump(config_vars, open(new_yaml_file, 'w'), default_flow_style=False)
            except yaml.YAMLError as exc:
                self.log(exc)
        self.config['config'] = new_yaml_file

    def _unset_vfd_vars(self, env_vars_toset):
        cmd = ['conda', 'env', 'config', 'vars', 'unset']

        for env_var in env_vars_toset:
            cmd.append(f'{env_var}')
        cmd.append('-n')
        cmd.append(self.config['conda_env'])

        cmd = ' '.join(cmd)
        Exec(cmd, LocalExecInfo(env=self.mod_env)).run()
        self.log(f'Pyflextrkr _unset_vfd_vars: {cmd}')

    def _set_env_vars(self, env_vars_toset):

        self.log('Pyflextrkr _set_env_vars')

        # Unset all env_vars_toset first
        self._unset_vfd_vars(env_vars_toset)

        cmd = ['conda', 'env', 'config', 'vars', 'set']
        for env_var in env_vars_toset:
            env_var_val = self.mod_env[env_var]
            cmd.append(f'{env_var}={env_var_val}')

        cmd.append('-n')
        cmd.append(self.config['conda_env'])
        cmd = ' '.join(cmd)
        self.log(f'Pyflextrkr _set_env_vars: {cmd}')
        Exec(cmd, LocalExecInfo(env=self.mod_env)).run()

    def _construct_cmd(self):
        """
        Construct the command to launch the application (bare-metal only).
        """
        self.clean()

        cmd = []
        if self.config['run_parallel'] == 1:
            cmd = [
                'conda', 'run', '-v', '-n', self.config['conda_env'],
            ]
        elif self.config['run_parallel'] == 2:
            host_list_str = None

            if self.hostfile is None:
                raise Exception('Running with Dask-MPI mode but self.hostfile is None')

            if 'localhost' in self.hostfile:
                host_list_str = '127.0.0.1'
            else:
                for hostname in self.hostfile:
                    if host_list_str is None:
                        host_list_str = hostname.rstrip()
                    else:
                        host_list_str = host_list_str + ',' + hostname.rstrip()

            if host_list_str is None:
                raise Exception('host_list_str is None')
            self.log(f'Pyflextrkr host_list_str: {host_list_str}')

            ppn = self.config['nprocesses'] / len(self.hostfile)
            cmd = [
                'conda', 'run', '-v', '-n', self.config['conda_env'],
                'mpirun',
                '--host', host_list_str,
                '-n', str(self.config['nprocesses']),
                '-ppn', str(int(ppn)),
            ]

        cmd.append('python')
        if self.config['pyflextrkr_path'] and self.config['runscript']:
            cmd.append(
                f'{self.config["pyflextrkr_path"]}/runscripts/{self.config["runscript"]}.py')

        cmd.append(self.config['config'])

        self.config['run_cmd'] = ' '.join(cmd)

    def start(self):
        """
        Launch PyFLEXTRKR.

        Container mode runs /opt/run_demo.sh (single-node) or
        /opt/run_demo_multinode.sh (MPI) inside the deploy image.
        Default mode uses conda + the existing bare-metal workflow.
        """
        if self.config.get('deploy_mode') == 'container':
            demo = self.config.get('demo', 'mcs_tbpf')
            replicates = max(
                int(self.config.get('replicates', 1) or 1), 1)
            if demo == 'mcs_tbpf_multinode':
                cmd = '/opt/run_demo_multinode.sh'
                Exec(cmd, MpiExecInfo(
                    nprocs=self.config.get('nprocs', 1),
                    ppn=self.config.get('ppn', 1),
                    hostfile=self.hostfile,
                    port=self.ssh_port,
                    container=self._container_engine,
                    container_image=self.deploy_image_name(),
                    shared_dir=self.shared_dir,
                    private_dir=self.private_dir,
                    env=self.mod_env,
                )).run()
            else:
                # run_demo.sh writes everything (input download, tar
                # extract, outputs) under /output/pyflextrkr-data inside
                # the container's writable layer. Routing /output to the
                # CTE FUSE mount breaks the tar extract (wrp_cte_fuse
                # returns ENOSYS on rename(2)). Run on the container's
                # native /output, then post-stage to FUSE if `out` is
                # configured -- cp into FUSE is a plain write, supported.
                stage_target = self.config.get('out', '').strip()
                # The PyFLEXTRKR demo runner uses the demo NAME as the
                # input selector. We expose `demo` here so the YAML knob
                # actually flows to the container script -- previously
                # pkg.py ignored it for the single-node path. Map short
                # names to actual run_demo_tests.py demos.
                _DEMO_ALIASES = {
                    'mcs_tbpf': 'demo_mcs_tbpf_idealized',
                    'mcs_tbpf_multinode': 'demo_mcs_tbpf_idealized',
                    'mcs_imerg': 'demo_mcs_imerg',
                    'mcs_imerg_mcsmip': 'demo_mcs_imerg_mcsmip',
                    'mcs_wrf_tbpf': 'demo_mcs_wrf_tbpf',
                    'mcs_model25km': 'demo_mcs_model25km',
                    'mcs_wrf_tbradar': 'demo_mcs_wrf_tbradar',
                    'mcs_gridrad': 'demo_mcs_gridrad',
                    'mcs_himawari': 'demo_mcs_himawari',
                    'cell_nexrad': 'demo_cell_nexrad',
                    'cell_csapr': 'demo_cell_csapr',
                    'generic_tracking': 'demo_generic_tracking',
                }
                demo_arg = _DEMO_ALIASES.get(str(demo), str(demo))
                parallel = max(
                    int(self.config.get('parallel_reps', 1) or 1), 1)
                omp = int(self.config.get('omp_threads', 0) or 0)

                # `data_root` (default /output/pyflextrkr-data) is where
                # the demo runner downloads inputs, extracts tarballs,
                # writes intermediate features/tracks, and the final
                # mcs_stats. Point it at a FUSE mount (e.g.
                # /mnt/dumbwarp/pyflextrkr-data) to route every R/W of
                # the multi-stage pipeline through that adapter.
                data_root = (self.config.get('data_root')
                             or '/output/pyflextrkr-data').rstrip('/')
                if parallel <= 1:
                    # Single-host sequential path (original).
                    if replicates == 1:
                        cmd = (f"/opt/run_demo.sh '{demo_arg}' "
                               f"{data_root}")
                    else:
                        cmd = (
                            "set -e; "
                            f"for i in $(seq 1 {replicates}); do "
                            f"  rep=$(printf 'rep_%03d' \"$i\"); "
                            f"  echo \"=== pyflextrkr replicate $rep ($i/{replicates}) ===\"; "
                            f"  /opt/run_demo.sh '{demo_arg}' "
                            f"    \"{data_root}-$rep\" || exit 1; "
                            f"done"
                        )
                    if stage_target:
                        cmd = (
                            f'{cmd} && '
                            f'mkdir -p {stage_target} && '
                            f'cp -r {data_root}*/. {stage_target}/'
                        )
                    Exec(cmd, LocalExecInfo(
                        container=self._container_engine,
                        container_image=self.deploy_image_name(),
                        shared_dir=self.shared_dir,
                        private_dir=self.private_dir,
                        env=self.mod_env,
                    )).run()
                    return

                # parallel_reps > 1: PsshExecInfo fan-out + per-host
                # wait -n batching. Each rep gets its own
                # /output/pyflextrkr-data-<rep> so concurrent demo
                # invocations on the same host don't collide on extracts
                # or output writes. Per-rep stage to FUSE happens after
                # the per-host all-reps wait.
                nhosts = (max(len(self.hostfile.hosts), 1)
                          if self.hostfile else 1)
                local_reps = (replicates + nhosts - 1) // nhosts
                omp_export = (
                    f"export OMP_NUM_THREADS={omp}; " if omp > 0 else ""
                )
                stage_block = ""
                if stage_target and stage_target != data_root:
                    stage_block = (
                        f"&& mkdir -p {stage_target}/$rep "
                        f"&& cp -r {data_root}-$rep/. "
                        f"{stage_target}/$rep/"
                    )
                cmd = (
                    f"set -e; "
                    f"{omp_export}"
                    f"LOCAL={local_reps}; "
                    f"PAR={parallel}; "
                    f"H=$(hostname -s); "
                    f"echo \"[pyflextrkr-parallel] host=$H reps=$LOCAL parallel=$PAR omp=${{OMP_NUM_THREADS:-default}}\"; "
                    f"PIDS=(); "
                    f"for i in $(seq 1 $LOCAL); do "
                    f"  ( "
                    f"    rep=$(printf 'rep_%03d-%s' \"$i\" \"$H\"); "
                    f"    /opt/run_demo.sh '{demo_arg}' "
                    f"      \"{data_root}-$rep\" "
                    f"      {stage_block} "
                    f"      && echo \"[pyflextrkr-parallel] host=$H $rep DONE\" "
                    f"      || {{ echo \"[pyflextrkr-parallel] host=$H $rep FAILED\" >&2; exit 1; }} "
                    f"  ) & "
                    f"  PIDS+=($!); "
                    f"  if [ \"${{#PIDS[@]}}\" -ge $PAR ]; then "
                    f"    wait -n; "
                    f"    PIDS=(\"${{PIDS[@]:1}}\"); "
                    f"  fi; "
                    f"done; "
                    f"wait"
                )
                Exec(cmd, PsshExecInfo(
                    hostfile=self.hostfile,
                    container=self._container_engine,
                    container_image=self.deploy_image_name(),
                    shared_dir=self.shared_dir,
                    private_dir=self.private_dir,
                    env=self.mod_env,
                )).run()
            return

        if self.config['with_hermes']:
            self._set_env_vars(self.hermes_env_vars)
        else:
            self._unset_vfd_vars(self.hermes_env_vars)

        # Configure yaml file before start
        self._configure_yaml()
        self._construct_cmd()

        self.log(f"Pyflextrkr run_cmd: {self.config['run_cmd']}")

        start = time.time()

        Exec(self.config['run_cmd'],
             LocalExecInfo(env=self.mod_env,
                           pipe_stdout=self.config.get('stdout'),
                           pipe_stderr=self.config.get('stderr'))).run()

        end = time.time()
        diff = end - start
        self.log(f'Pyflextrkr TIME: {diff} seconds')

    def stop(self):
        pass

    def kill(self):
        cmd = ['killall', '-9', 'python']
        Exec(' '.join(cmd), LocalExecInfo(hostfile=self.hostfile)).run()

    def clean(self):
        """
        Destroy output_data directories from prior runs (bare-metal only).
        Container mode writes under the container's own /output volume.
        """
        if self.config.get('deploy_mode') == 'container':
            return
        if not self.config.get('experiment_input_path'):
            return
        output_dir = self.config['experiment_input_path'] + f"/output_data/{self.config['runscript']}"
        if self.config['local_exp_dir'] is not None:
            output_dir = self.config['local_exp_dir'] + f"/output_data/{self.config['runscript']}"

        self.log(f'Removing {output_dir}')
        Rm(output_dir).run()
