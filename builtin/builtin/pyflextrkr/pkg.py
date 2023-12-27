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


class Pyflextrkr(Application):
    """
    This class provides methods to launch the Pyflextrkr application.
    """
    def _init(self):
        """
        Initialize paths
        """
        # self.adios2_xml_path = f'{self.shared_dir}/adios2.xml'
        # self.settings_json_path = f'{self.shared_dir}/settings-files.json'
        # self.conda_env = 'pyflextrkr'
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
                'default': f'{self.pkg_dir}/example_config/run_mcs_tbpfradar3d_wrf.yml',
            },
            {
                'name': 'runscript',
                'msg': 'The name of the Pyflextrkr script to run (run_mcs_tbpfradar3d_wrf)',
                'type': str,
                'default': None,
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
                'default': "~/experiment/flextrkr_runs",
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
            self.env['EXPERIMENT_PATH'] = self.config['experiment_path']
        
        if self.config['conda_env'] is None:
            raise Exception("Must set the conda environment for running Pyflextrkr")
        
        if self.config['runscript'] is None:
            raise Exception("Must set the Pyflextrkr script to run")
        else:
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
            # Exec(f"export FLUSH_MEM=FALSE")
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
        
        # for item in self.env:
        #     print(f"{item}={self.env[item]}\n")

    def start(self):
        """
        Launch an application. E.g., Pyflextrkr will launch the servers, clients,
        and metadata services on all necessary pkgs.

        :return: None
        """
        
        # check which python is being used
        # Exec("which python")
        
        cmd = [
            'conda','run', '-n', self.config['conda_env'], # conda environment
            'python',
        ]

        if self.config['pyflextrkr_path'] and self.config['runscript']:
            cmd.append(f'{self.config["pyflextrkr_path"]}/runscripts/{self.config["runscript"]}.py')
        if self.config['config']:
            cmd.append(self.config['config'])
        
        conda_cmd = ' '.join(cmd)
        
        print(f"Running Pyflextrkr with command: {conda_cmd}")
        print(f"STDOUT to : {self.config['log_file']}")
        
        start = time.time()
        
        Exec(conda_cmd,
             LocalExecInfo(env=self.mod_env,
                           pipe_stdout=self.config['log_file'],))
        
        end = time.time()
        diff = end - start
        self.log(f'TIME: {diff} seconds') # color=Color.GREEN
        

    def stop(self):
        """
        Stop a running application. E.g., Pyflextrkr will terminate the servers,
        clients, and metadata services.

        :return: None
        """
        cmd = ['killall', '-9', 'python']
        Exec(' '.join(cmd))
        
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
        print(f"Manual: Please clean up files in {self.config['experiment_path']}")
        pass
        # output_dir = self.config['output'] + "*"
        # print(f'Removing {output_dir}')
        # Rm(output_dir)