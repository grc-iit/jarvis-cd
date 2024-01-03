"""
This module provides classes and methods to launch the Gray Scott application.
Gray Scott is a 3D 7-point stencil code for modeling the diffusion of two
substances.
"""
from jarvis_cd.basic.pkg import Application, Color
from jarvis_util import *
import time
import pathlib

import subprocess


class ARLDM(Application):
    """
    This class provides methods to launch the ARLDM application.
    """
    def _init(self):
        """
        Initialize paths
        """
        # self.adios2_xml_path = f'{self.shared_dir}/adios2.xml'
        # self.settings_json_path = f'{self.shared_dir}/settings-files.json'
        # self.conda_env = 'arldm'
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
                'msg': 'Name of the conda environment for running ARLDM',
                'type': str,
                'default': "arldm",
            },
            {
                'name': 'config',
                'msg': 'The config file for running analysis',
                'type': str,
                'default': f'{self.pkg_dir}/example_config/config_template.yaml',
            },
            {
                'name': 'runscript',
                'msg': 'The name of the ARLDM script to run',
                'type': str,
                'default': 'vistsis', # smallest dataset
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
                'name': 'arldm_path',
                'msg': 'Absolute path to the ARLDM source code (can set to `scspkg pkg src arldm`/ARLDM)',
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
                'name': 'mode',
                'msg': 'Mode of running ARLDM: train(D) or sample',
                'type': str,
                'default': 'train',
            },
            {
                'name': 'supported_runscripts',
                'msg': 'List of supported running scripts',
                'type': list,
                'default': ['flintstones', 'pororo', 'vistsis', 'vistdii'],
            },
            {
                'name': 'num_workers',
                'msg': 'Number of CPU workers to use for parallel processing',
                'type': int,
                'default': 1,
            },
            {
                'name': 'experiment_path',
                'msg': 'Absolute path to the experiment run input and output files',
                'type': str,
                'default': '${HOME}/experiments/ARLDM',
            },
            {
                'name': 'ckpt_dir',
                'msg': 'Directory to save checkpoints',
                'type': str,
                'default': None, #'${HOME}/experiments/ARLDM/save_ckpt',
            },
            {
                'name': 'sample_output_dir',
                'msg': 'Directory to save samples',
                'type': str,
                'default': None, #'${HOME}/experiments/ARLDM/output_data/sample_out_{runscript}_{mode}',
            },
            {
                'name': 'hdf5_file',
                'msg': 'HDF5 file to save samples',
                'type': str,
                'default': None, #'${HOME}/experiments/ARLDM/output_data/{runscript}.h5',
            }
        ]

    def _configure(self, **kwargs):
        """
        Converts the Jarvis configuration to application-specific configuration.

        :param kwargs: Configuration parameters for this pkg.
        :return: None
        """
        
        self.env['HDF5_USE_FILE_LOCKING'] = "FALSE" # set HDF5 locking: FALSE, TRUE, BESTEFFORT
        
        if self.config['experiment_path'] is not None:
            self.config['experiment_path'] = os.path.expandvars(self.config['experiment_path'])
            self.env['EXPERIMENT_PATH'] = self.config['experiment_path']
        
        if self.config['conda_env'] is None:
            raise Exception("Must set the conda environment for running ARLDM")
        if self.config['config'] is None:
            raise Exception("Must set the ARLDM config file")
        if self.config['runscript'] is None:
            raise Exception("Must set the ARLDM script to run")
        else:
            # Check if runscript is supported
            if self.config['runscript'] not in self.config['supported_runscripts']:
                raise Exception(f"Runscript {self.config['runscript']} is not supported, 
                                must be one of {self.config['supported_runscripts']}")
            
            
        if self.config['flush_mem'] == False:
            Exec(f"export FLUSH_MEM=FALSE")
        else:
            if self.config['flush_mem_cmd'] is None:
                raise Exception("Must add the command to flush memory using flush_mem_cmd")
        
        if self.config['arldm_path'] is None:
            raise Exception("Must set the path to the ARLDM source code")
        else:
            # check that path exists
            pathlib.Path(self.config['arldm_path']).exists()
            self.config['log_file'] = f'{self.config["arldm_path"]}/arldm_run.log'
            self.config['stdout'] = f'{self.config["arldm_path"]}/arldm_run.log'
        
        # check and make -p experiment_path
        pathlib.Path(self.config['experiment_path']).mkdir(parents=True, exist_ok=True)
        
        # set ckpt_dir
        self.config['ckpt_dir'] = f'{self.config["experiment_path"]}/save_ckpt'
        pathlib.Path(self.config['ckpt_dir']).mkdir(parents=True, exist_ok=True)
        
        # set sample_output_dir
        self.config['sample_output_dir'] = f'{self.config["experiment_path"]}/output_data/sample_out_{self.config["runscript"]}_{self.config["mode"]}'
        pathlib.Path(self.config['sample_output_dir']).mkdir(parents=True, exist_ok=True)
        
        # set sample_output_dir
        self.config['hdf5_file'] = f'{self.config["experiment_path"]}/output_data/{self.config["runscript"]}.h5'
        
        
        self._configure_yaml()
        

    def _configure_yaml(self):
        yaml_file = self.config['config']
        
        with open(yaml_file, "r") as stream:
            try:
                config_vars = yaml.safe_load(stream)
                
                run_test = config_vars['runscript']
                
                config_vars['mode'] = self.config['mode']
                config_vars['num_workers'] = self.config['num_workers']
                config_vars['ckpt_dir'] = self.config['ckpt_dir']
                config_vars['run_name'] = f"{self.config['runscript']}_{self.config['mode']}"
                config_vars['dataset'] = self.config['runscript']
                config_vars['sample_output_dir'] = self.config['sample_output_dir']
                
                config_vars[run_test]['hdf5_file'] = self.config['hdf5_file']
                
                # save config_vars back to yaml file
                new_yaml_file = yaml_file.replace("_template.yml", ".yml")
                yaml.dump(config_vars, open(new_yaml_file, 'w'), default_flow_style=False)
            except yaml.YAMLError as exc:
                print(exc)
        self.config['config'] = new_yaml_file

    def prep_hdf5_file(self):
        """
        Prepare the HDF5 file for the ARLDM run
        """
        pass


    def start(self):
        """
        Launch an application. E.g., OrangeFS will launch the servers, clients,
        and metadata services on all necessary pkgs.

        :return: None
        """
        pass
        
        # check which python is being used
        Exec("which python")
        
        # cmd = [
        #     'conda','run', '-n', self.config['conda_env'], # conda environment
        #     'python',
        # ]

        # if self.config['arldm_path'] and self.config['runscript']:
        #     cmd.append(f'{self.config["arldm_path"]}/main.py')
        
        # conda_cmd = ' '.join(cmd)
        
        # print(f"Running ARLDM with command: {conda_cmd}")
        # print(f"STDOUT to : {self.config['log_file']}")
        
        # start = time.time()
        
        # Exec(conda_cmd,
        #      LocalExecInfo(env=self.mod_env,
        #                    pipe_stdout=self.config['log_file'],))
        
        # end = time.time()
        # diff = end - start
        # self.log(f'TIME: {diff} seconds') # color=Color.GREEN
        

    def stop(self):
        """
        Stop a running application. E.g., OrangeFS will terminate the servers,
        clients, and metadata services.

        :return: None
        """
        pass

    def clean(self):
        """
        Destroy all data for an application. E.g., OrangeFS will delete all
        metadata and data directories in addition to the orangefs.xml file.

        :return: None
        """
        pass
        # output_dir = self.config['output'] + "*"
        # print(f'Removing {output_dir}')
        # Rm(output_dir)