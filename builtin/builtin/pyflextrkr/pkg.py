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
                'default': None,
            },
            {
                'name': 'runscript',
                'msg': 'The name of the Pyflextrkr script to run',
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
                'default': None, #PYFLEXTRKR_PATH,
            },
            {
                'name': 'log_file',
                'msg': 'File path to log stdout',
                'type': str,
                'default': None, #f'{PYFLEXTRKR_PATH}/pyflextrkr_run.log',
            },
        ]

    def _configure(self, **kwargs):
        """
        Converts the Jarvis configuration to application-specific configuration.

        :param kwargs: Configuration parameters for this pkg.
        :return: None
        """
        if self.config['conda_env'] is None:
            raise Exception("Must set the conda environment for running Pyflextrkr")
        if self.config['config'] is None:
            raise Exception("Must set the Pyflextrkr config file")
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
        
        if self.config['flush_mem'] == False:
            Exec(f"export FLUSH_MEM=FALSE")
        else:
            if self.config['flush_mem_cmd'] is None:
                raise Exception("Must add the command to flush memory using flush_mem_cmd")
        
        if self.config['pyflextrkr_path'] is None:
            raise Exception("Must set the path to the Pyflextrkr source code")
        else:
            # check that path exists
            pathlib.Path(self.config['pyflextrkr_path']).exists()
            self.config['log_file'] = f'{self.config["pyflextrkr_path"]}/pyflextrkr_run.log'
            self.config['stdout'] = f'{self.config["pyflextrkr_path"]}/pyflextrkr_run.log'
            

    def start(self):
        """
        Launch an application. E.g., OrangeFS will launch the servers, clients,
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