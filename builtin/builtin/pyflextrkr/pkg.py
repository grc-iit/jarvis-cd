"""
This module provides classes and methods to launch the Gray Scott application.
Pyflextrkr is ....
"""
from jarvis_cd.basic.pkg import Application, Color
from jarvis_util import *
import time
import pathlib

import yaml


class Pyflextrkr(Application):
    """
    This class provides methods to launch the Pyflextrkr application.
    """
    def _init(self):
        """
        Initialize paths
        """
        pass

    def _configure_menu(self):
        """
        Create a CLI menu for the configurator method.
        For thorough documentation of these parameters, view:
        https://github.com/scs-lab/jarvis-util/wiki/3.-Argument-Parsing

        :return: List(dict)
        """
        
        return [
            {
                'name': 'conda_env',
                'msg': 'Name of the conda environment for running Pyflextrkr',
                'type': str,
                'default': "flextrkr",
            },
            {
                'name': 'config',
                'msg': 'The config file for running analysis',
                'type': str,
                'default': f'{self.pkg_dir}/example_config/run_mcs_tbpfradar3d_wrf_template.yml',
            },
            {
                'name': 'update_envar',
                'msg': 'Update the conda environment variables',
                'type': str,
                'default': f'{self.pkg_dir}/example_config/update_envar.yml',
            },
            {
                'name': 'runscript',
                'msg': 'The name of the Pyflextrkr script to run (run_mcs_tbpfradar3d_wrf)',
                'type': str,
                'default': 'run_mcs_tbpfradar3d_wrf',
                'choices': ['run_mcs_tbpfradar3d_wrf']
            },
            {
                'name': 'flush_mem',
                'msg': 'Flushing the memory after each stage',
                'type': bool,
                'default': False,
            },
            {
                'name': 'flush_mem_cmd',
                'msg': 'Command to flush the node memory',
                'type': str,
                'default': None,
            },
            {
                'name': 'pyflextrkr_path',
                'msg': 'Absolute path to the Pyflextrkr source code',
                'type': str,
                'default': None,
            },
            {
                'name': 'log_file',
                'msg': 'File path to log stdout',
                'type': str,
                'default': None,
            },
            {
                'name': 'experiment_path',
                'msg': 'Absolute path to the experiment run input and output files',
                'type': str,
                'default': '${HOME}/experiments/flextrkr_runs',
            },
            {
                'name': 'run_parallel',
                'msg': 'Parallel mode for Pyflextrkr: 0 (serial), 1 (local cluster), 2 (Dask MPI)',
                'type': int,
                'default': 1,
            },
            {
                'name': 'nprocesses',
                'msg': 'Number of processes to run in parallel',
                'type': int,
                'default': 8,
            },
            {
                'name': 'run_cmd', # This is a internal variable
                'msg': 'Command to run Pyflextrkr',
                'type': str,
                'default': None,
            }
        ]

    def _configure(self, **kwargs):
        """
        Converts the Jarvis configuration to application-specific configuration.

        :param kwargs: Configuration parameters for this pkg.
        :return: None
        """
        
        self.env['HDF5_USE_FILE_LOCKING'] = "BESTEFFORT" # set HDF5 locking: FALSE, TRUE, BESTEFFORT
        
        if self.config['experiment_path'] is not None:
            self.config['experiment_path'] = os.path.expandvars(self.config['experiment_path'])
            self.env['EXPERIMENT_PATH'] = self.config['experiment_path']
        
        # Check if run_parallel is 0, 1, or 2
        if self.config['run_parallel'] not in [0,1,2]:
            raise Exception("run_parallel must be 0 (serial), 1 (local cluster), or 2 (Dask MPI)")
        
        if self.config['conda_env'] is None:
            raise Exception("Must set the conda environment for running Pyflextrkr")
        
        if self.config['runscript'] is None:
            raise Exception("Must set the Pyflextrkr script to run")
        else:
            
            # check if run script matches config file
            if self.config['runscript'] not in self.config['config']:
                raise Exception(f"Run script {self.config['runscript']} does not match config file {self.config['config']}")
            
            # get base file name without extension
            pass_in_path = self.config['runscript']
            script_name = pass_in_path.split("/")[-1]
            # remove ".py" from file name
            if ".py" in script_name:
                script_name = script_name[:-3]
            self.config['runscript'] = script_name

        # Check if config file exists
        if pathlib.Path(self.config['config']).exists():
            pass
        else:
            raise Exception(f"File {self.config['config']} does not exist.")

        if self.config['flush_mem'] == False:
            self.env['FLUSH_MEM'] = "FALSE"
        else:
            self.env['FLUSH_MEM'] = "TRUE"
            if self.config['flush_mem_cmd'] is None:
                raise Exception("Must add the command to flush memory using flush_mem_cmd")
        
        if self.config['pyflextrkr_path'] is None:
            raise Exception("Must set the path to the Pyflextrkr source code")
        else:
            # check that path exists
            pathlib.Path(self.config['pyflextrkr_path']).exists()
            self.config['log_file'] = f'{self.config["pyflextrkr_path"]}/pyflextrkr_run.log'
            self.config['stdout'] = f'{self.config["pyflextrkr_path"]}/pyflextrkr_run.log'
        
        ## Configure yaml file
        self._configure_yaml()
        

    def _configure_yaml(self):
        self.env['HDF5_USE_FILE_LOCKING'] = "TRUE" # set HDF5 locking: FALSE, TRUE, BESTEFFORT

        yaml_file = self.config['config']
        
        if "_template.yml" not in str(yaml_file):
            yaml_file = yaml_file.replace(".yml", "_template.yml")
        
        self.log(f"Pyflextrkr yaml_file: {yaml_file}")
            
        paths_to_mkdir = []
        
        with open(yaml_file, "r") as stream:
            try:
                config_vars = yaml.safe_load(stream)
                config_vars['dask_tmp_dir'] = f"/tmp/pyflextrkr_test"
                config_vars['clouddata_path'] = f"{self.config['experiment_path']}/input_data/{self.config['runscript']}/"
                config_vars['root_path'] = f"{self.config['experiment_path']}/output_data/{self.config['runscript']}/"
                
                paths_to_mkdir.append(config_vars['dask_tmp_dir'])
                paths_to_mkdir.append(config_vars['clouddata_path'])
                paths_to_mkdir.append(config_vars['root_path'])
                
                # Set run mode
                config_vars['run_parallel'] = self.config['run_parallel']
                if self.config['run_parallel'] == 0 and self.config['nprocesses'] > 1:
                    self.log(f"WARNING: run_parallel is 0 (serial) nprocesses is set to 1")
                    self.config['nprocesses'] = 1
                config_vars['nprocesses'] = self.config['nprocesses']
                
                # check if landmask_filename is a key in config_vars
                if 'landmask_filename' in config_vars:
                    # check if landmask_filename exists
                    landmask_filename = f"{self.config['experiment_path']}/input_data/{self.config['runscript']}/wrf_landmask.nc"
                    config_vars['landmask_filename'] = landmask_filename
                    
                    if pathlib.Path(landmask_filename).exists():
                        config_vars['landmask_filename'] = landmask_filename
                    else:
                        raise Exception(f"File {config_vars['landmask_filename']} does not exist.")
                
                # save config_vars back to yaml file
                new_yaml_file = yaml_file.replace("_template.yml", ".yml")
                yaml.dump(config_vars, open(new_yaml_file, 'w'), default_flow_style=False)
            except yaml.YAMLError as exc:
                self.log(exc)
        self.config['config'] = new_yaml_file
        
        for new_path in paths_to_mkdir:
            pathlib.Path(new_path).mkdir(parents=True, exist_ok=True)
            # Exec(f"mkdir -p {new_path}")

    def _construct_cmd(self):
        """
        Construct the command to launch the application. E.g., Pyflextrkr will
        launch with expected environment and number of srun processes.

        :return: None
        """
        cmd = []
        if self.config['run_parallel'] == 1:
            cmd = [
            'conda','run', '-n', self.config['conda_env'],
            ]
        elif self.config['run_parallel'] == 2:
            host_list_str = None
            
            # Check if self.jarvis.hostfile is set
            if self.jarvis.hostfile is None:
                raise Exception("Running with Dask-MPI mode but self.jarvis.hostfile is None")
            
            # open self.jarvis.hostfile to get all lines of hosts into a string deliminated by ,
            # self.log(f"Pyflextrkr hostfile: {self.jarvis.hostfile}")
            if 'localhost' in self.jarvis.hostfile:
                host_list_str = "127.0.0.1"
            else:
                for hostname in self.jarvis.hostfile:
                    if host_list_str is None:
                        host_list_str = hostname.rstrip()
                    else:
                        host_list_str = host_list_str + "," + hostname.rstrip()
            
            if host_list_str is None:
                raise Exception("host_list_str is None")
            self.log(f"Pyflextrkr host_list_str: {host_list_str}")
            
            # mpirun --host $hostlist --npernode 2
            ppn = self.config['nprocesses']/len(self.jarvis.hostfile)
            cmd = [
                'conda','run', '-n', self.config['conda_env'],
                'mpirun',
                '--host', host_list_str,
                '-n', str(self.config['nprocesses']),
                '-ppn', str(int(ppn)),
            ]
            
        cmd.append('python')
        # Convert runscript to full .py file path
        if self.config['pyflextrkr_path'] and self.config['runscript']:
            cmd.append(f'{self.config["pyflextrkr_path"]}/runscripts/{self.config["runscript"]}.py')
        
        
        cmd.append(self.config['config'])

        self.config['run_cmd'] = ' '.join(cmd)

    def _update_conda_env(self):
        """ YAML file format
        variables:
            HDF5_USE_FILE_LOCKING: FALSE
            HDF5_DRIVER: "hdf5_tracker_vfd"
            HDF5_PLUGIN_PATH: "/home/mtang11/install/tracker/lib"
        """

        yaml_file = self.config['update_envar']
                
        # conda env update --file ares_tracker_envar.yaml --prune --name arldm # need internet
        cmd = [
            'conda','run', '-n', self.config['conda_env'],
            'conda','env','update',
            '--file', yaml_file,
            '--prune',
            '--name', self.config['conda_env'],
        ]
        conda_cmd = ' '.join(cmd)
        print(f"Updating conda environment with command: {conda_cmd}")
        Exec(conda_cmd, LocalExecInfo(env=self.mod_env,))
        
        # check if environment variables are updated
        with open(yaml_file, "r") as stream:
            try:
                config_vars = yaml.safe_load(stream)
                for key, val in config_vars['variables'].items():
                    # print(f"YAML file environment variable: {key} = {val}")
                    cmd = [
                        'conda','run', '-n', self.config['conda_env'],
                        'echo', f"${key}",
                    ]
                    conda_cmd = ' '.join(cmd)
                    Exec(conda_cmd, LocalExecInfo(env=self.mod_env,))
            except yaml.YAMLError as exc:
                print(exc)

    def start(self):
        """
        Launch an application. E.g., Pyflextrkr will launch the servers, clients,
        and metadata services on all necessary pkgs.

        :return: None
        """
        
        ## Configure yaml file before start
        self._configure_yaml()
        self._update_conda_env()
        
        self._construct_cmd()
        
        self.log(f"Pyflextrkr run_cmd: {self.config['run_cmd']}")
        self.log(f"Pyflextrkr log_file : {self.config['log_file']}")
        
        start = time.time()
        
        Exec(self.config['run_cmd'],
             LocalExecInfo(env=self.mod_env,
                           pipe_stdout=self.config['log_file'],))
        
        end = time.time()
        diff = end - start
        self.log(f'Pyflextrkr TIME: {diff} seconds') # color=Color.GREEN
        

    def stop(self):
        """
        Stop a running application. E.g., Pyflextrkr will terminate the servers,
        clients, and metadata services.

        :return: None
        """
        pass
        
    def kill(self):
        """
        Stop a running application. E.g., Pyflextrkr will terminate the servers,
        clients, and metadata services.

        :return: None
        """
        cmd = ['killall', '-9', 'python']
        Exec(' '.join(cmd))

    def clean(self):
        """
        Destroy all data for an application. E.g., Pyflextrkr will delete all
        metadata and data directories in addition to the orangefs.xml file.

        :return: None
        """
        self.log(f"Manual Exec Required: Please clean up files in {self.config['experiment_path']}")
        pass
        # output_dir = self.config['output'] + "*"
        # self.log(f'Removing {output_dir}')
        # Rm(output_dir)