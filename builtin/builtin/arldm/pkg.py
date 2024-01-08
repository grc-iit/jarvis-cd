"""
This module provides classes and methods to launch the Arldm application.
Arldm is ....
"""
from jarvis_cd.basic.pkg import Application
from jarvis_util import *
import pathlib
import yaml

class Arldm(Application):
    """
    This class provides methods to launch the Arldm application.
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
                'msg': 'Name of the conda environment for running ARLDM',
                'type': str,
                'default': "arldm",
            },
            {
                'name': 'config',
                'msg': 'The config file for running analysis',
                'type': str,
                'default': f'{self.pkg_dir}/example_config/config_template.yml',
            },
            {
                'name': 'runscript',
                'msg': 'The name of the ARLDM script to run',
                'type': str,
                'default': 'vistsis', # smallest dataset
                'choices': ['flintstones', 'pororo', 'vistsis', 'vistdii'],
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
                'choice': ['train', 'sample'],
            },
            {
                'name': 'num_workers',
                'msg': 'Number of CPU workers to use for parallel processing',
                'type': int,
                'default': 1,
                'choices': [0, 1, 2],
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
            },
            {
                'name': 'prep_hdf5',
                'msg': 'Prepare the HDF5 file for the ARLDM run',
                'type': bool,
                'default': False,
            }
        ]

    def _configure_yaml(self):
        yaml_file = self.config['config']

        if "_template.yml" not in str(yaml_file):
            yaml_file = yaml_file.replace(".yml", "_template.yml")
        
        self.log(f"ARLDM yaml_file: {yaml_file}")

        with open(yaml_file, "r") as stream:
            try:
                config_vars = yaml.safe_load(stream)
                
                run_test = self.config['runscript']
                
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

    def _configure(self, **kwargs):
        """
        Converts the Jarvis configuration to application-specific configuration.
        E.g., OrangeFS produces an orangefs.xml file.

        :param kwargs: Configuration parameters for this pkg.
        :return: None
        """
        self.env['HDF5_USE_FILE_LOCKING'] = "FALSE" # set HDF5 locking: FALSE, TRUE, BESTEFFORT
        self.env['HYDRA_FULL_ERROR'] = "1" # complete stack trace.
        
        if self.config['experiment_path'] is not None:
            self.config['experiment_path'] = os.path.expandvars(self.config['experiment_path'])
            self.env['EXPERIMENT_PATH'] = self.config['experiment_path']
        
        if self.config['conda_env'] is None:
            raise Exception("Must set the conda environment for running ARLDM")
        if self.config['config'] is None:
            raise Exception("Must set the ARLDM config file")
        if self.config['runscript'] is None:
            raise Exception("Must set the ARLDM script to run")
            
        if self.config['flush_mem'] == False:
            self.env['FLUSH_MEM'] = "FALSE"
        else:
            if self.config['flush_mem_cmd'] is None:
                raise Exception("Must add the command to flush memory using flush_mem_cmd")
        
        if self.config['arldm_path'] is None:
            raise Exception("Must set the path to the ARLDM source code")
        else:
            # check that path exists
            pathlib.Path(self.config['arldm_path']).exists()
            # no default log file
            # self.config['log_file'] = f'{self.config["arldm_path"]}/arldm_run.log'
            # self.config['stdout'] = f'{self.config["arldm_path"]}/arldm_run.log'
        
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

    def prep_hdf5_file(self):
        """
        Prepare the HDF5 file for the ARLDM run
        """
        
        cmd = [
            'python',
        ]
        
        if self.config['runscript'] == 'pororo':
            cmd.append(f'{self.config["arldm_path"]}/data_script/pororo_hdf5.py')
            cmd.append(f'--data_dir {self.config["experiment_path"]}/input_data/pororo_png')
            cmd.append(f'--save_path {self.config["hdf5_file"]}')
        elif self.config['runscript'] == 'flintstones':
            cmd.append(f'{self.config["arldm_path"]}/data_script/flintstones_hdf5.py')
            cmd.append(f'--data_dir {self.config["experiment_path"]}/input_data/flintstones_data')
            cmd.append(f'--save_path {self.config["hdf5_file"]}')
        elif self.config['runscript'] == 'vistsis' or self.config['runscript'] == 'vistdii':
            cmd.append(f'{self.config["arldm_path"]}/data_script/vist_hdf5.py')
            cmd.append(f'--sis_json_dir {self.config["arldm_path"]}/input_data/sis')
            cmd.append(f'--dii_json_dir {self.config["arldm_path"]}/input_data/dii')
            cmd.append(f'--img_dir {self.config["experiment_path"]}/input_data/visit_img')
            cmd.append(f'--save_path {self.config["hdf5_file"]}')
        else:
            raise Exception("Must set the correct ARLDM script to run")
        
        prep_cmd = ' '.join(cmd)
        Exec(prep_cmd,
                LocalExecInfo(env=self.mod_env,))
        
        pass
    
    def train(self):
        """
        Run the ARLDM training run
        """
        # # check which python is being used
        # Exec("which python", LocalExecInfo(env=self.mod_env,))
        print(f"ARLDM training run: {self.config['runscript']}")
        
        cmd = [
            f"cd {self.config['arldm_path']}; echo Executing from directory `pwd`;",
            'conda','run', '-n', self.config['conda_env'], # conda environment
            'python',
        ]

        if self.config['arldm_path'] and self.config['runscript']:
            cmd.append(f'{self.config["arldm_path"]}/main.py')
        
        conda_cmd = ' '.join(cmd)
        
        print(f"Running ARLDM with command: {conda_cmd}")
        
        # Move config file to arldm_path
        Exec(f"cp {self.config['config']} {self.config['arldm_path']}/config.yml",
             LocalExecInfo(env=self.mod_env,))
        
        # # go to arldm_path
        # Exec(f"cd {self.config['arldm_path']}",
        #      LocalExecInfo(env=self.mod_env,))
        
        # Exec(f"cd {self.config['arldm_path']}; echo Executing from directory `pwd`", LocalExecInfo(env=self.mod_env,))
        
        start = time.time()
        
        Exec(conda_cmd,
             LocalExecInfo(env=self.mod_env,))
        
        end = time.time()
        diff = end - start
        self.log(f'TIME: {diff} seconds') # color=Color.GREEN
    
    def sample(self):
        """
        Run the ARLDM sampling run
        
        This step can only be run when training is fully completed. 
        Currently train is set to fast_dev_run.
        """
        print(f"ARLDM sampling run: not implemented yet")

    def start(self):
        """
        Launch an application. E.g., OrangeFS will launch the servers, clients,
        and metadata services on all necessary pkgs.

        :return: None
        """
        if self.config['prep_hdf5']:
            self.prep_hdf5_file()
        
        if self.config['mode'] == 'train':
            self.train()
        
        if self.config['mode'] == 'sample':
            self.sample()
        


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
