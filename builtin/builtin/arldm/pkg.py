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
        # print(f"ARLDM _init")
        pass

    def _configure_menu(self):
        """
        Create a CLI menu for the configurator method.
        For thorough documentation of these parameters, view:
        https://github.com/scs-lab/jarvis-util/wiki/3.-Argument-Parsing

        :return: List(dict)
        """
        print(f"ARLDM _configure_menu")
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
            },
            {
                'name': 'storage_device',
                'msg': 'Storage device to use for ARLDM run',
                'type': str,
                'default': 'nfs',
                # 'choices': ['nvme', 'ssd', 'hdd', 'nfs', 'pfs'], # this should be per systems
            },{
                'name': 'local_exp_dir',
                'msg': 'Local experiment directory',
                'type': str,
                'default': None,
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
                config_vars['run_name'] = f"{self.config['runscript']}_{self.config['mode']}"
                
                config_vars['ckpt_dir'] = self.config['ckpt_dir']
                config_vars['sample_output_dir'] = self.config['sample_output_dir']
                config_vars[run_test]['hdf5_file'] = self.config['hdf5_file']
                
                if self.config['local_exp_dir'] is not None:
                    replace_dir = self.config['experiment_path']
                    new_dir = self.config['local_exp_dir']
                    config_vars['ckpt_dir'] = config_vars['ckpt_dir'].replace(replace_dir, new_dir)
                    config_vars['sample_output_dir'] = config_vars['sample_output_dir'].replace(replace_dir, new_dir)
                    self.config['hdf5_file'] = f"{new_dir}/output_data/{self.config['runscript']}/{self.config['runscript']}_out.h5"
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
        # self.env['HDF5_USE_FILE_LOCKING'] = "FALSE" 
        # self.env['HYDRA_FULL_ERROR'] = "1" # complete stack trace.
        
        print(f"ARLDM _configure")
        
        self.setenv('HDF5_USE_FILE_LOCKING', "FALSE") # set HDF5 locking: FALSE, TRUE, BESTEFFORT
        self.setenv('HYDRA_FULL_ERROR', "1")
        
        if self.config['experiment_path'] is not None:
            self.config['experiment_path'] = os.path.expandvars(self.config['experiment_path'])
            # self.env['EXPERIMENT_PATH'] = self.config['experiment_path']
            self.setenv('EXPERIMENT_PATH', self.config['experiment_path'])
        
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
            if not pathlib.Path(self.config['arldm_path']).exists():
                raise Exception("Must set the correct path to the ARLDM source code")
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
        self.config['hdf5_file'] = f'{self.config["experiment_path"]}/output_data/{self.config["runscript"]}_out.h5'
        
        
        self._configure_yaml()
        

    def _prep_hdf5_file(self):
        """
        Prepare the HDF5 file for the ARLDM run
        """
        
        print(f"ARLDM _prep_hdf5_file to {self.config['hdf5_file']}")
        
        cmd = [
            'python',
        ]
        
        if self.config['runscript'] == 'pororo':
            cmd.append(f'{self.config["arldm_path"]}/data_script/pororo_hdf5.py')
            cmd.append(f'--data_dir {self.config["experiment_path"]}/input_data/pororo')
            cmd.append(f'--save_path {self.config["hdf5_file"]}')
        elif self.config['runscript'] == 'flintstones':
            cmd.append(f'{self.config["arldm_path"]}/data_script/flintstones_hdf5.py')
            cmd.append(f'--data_dir {self.config["experiment_path"]}/input_data/flintstones')
            cmd.append(f'--save_path {self.config["hdf5_file"]}')
        elif self.config['runscript'] == 'vistsis' or self.config['runscript'] == 'vistdii':
            cmd.append(f'{self.config["arldm_path"]}/data_script/vist_hdf5.py')
            cmd.append(f'--sis_json_dir {self.config["experiment_path"]}/input_data/vistsis')
            cmd.append(f'--dii_json_dir {self.config["experiment_path"]}/input_data/vistdii')
            cmd.append(f'--img_dir {self.config["experiment_path"]}/input_data/visit_img')
            cmd.append(f'--save_path {self.config["hdf5_file"]}')
        else:
            raise Exception("Must set the correct ARLDM script to run")
        
        prep_cmd = ' '.join(cmd)
        
        start = time.time()
        Exec(prep_cmd, LocalExecInfo(env=self.mod_env,))
        
        end = time.time()
        diff = end - start
        self.log(f'TIME: {diff} seconds') # color=Color.GREEN
        
        # check if hdf5_file exists
        if pathlib.Path(self.config['hdf5_file']).exists():
           print(f"HDF5 file created: {self.config['hdf5_file']}")
        else:
            raise Exception(f"HDF5 file not created: {self.config['hdf5_file']}") 
        
    
    def _stagein_h5_data(self):
        """
        Move the data to the storage device
        """
        print(f"ARLDM _stagein_h5_data")
        
        orig_path = self.config['experiment_path']
        rg = self.jarvis.resource_graph
        dev_type = self.config['storage_device']
        

        if self.config['storage_device'] == 'nfs':
            # Default is NFS, no need to move data
            pass
        elif self.config['storage_device'] == 'pfs':
            # Move data to PFS
            # TODO: do nothing for now
            pass
        else:
            # Find storage path
            dev_df = rg.find_storage(dev_types=[dev_type],shared=False)       
            if dev_df is None:
                raise Exception(f"Could not find storage device of type {dev_type}")     

            new_exp_dir = os.path.expandvars(dev_df.rows[0]['mount']) + "/ARLDM"
            new_exp_dir_input_dir = new_exp_dir + "/input_data" + f"/{self.config['runscript']}"
            new_exp_dir_output_dir = new_exp_dir + "/output_data" + f"/{self.config['runscript']}"
            
            # check if new_exp_dir_input_dir exist, if exists, no need to copy
            if (pathlib.Path(new_exp_dir_input_dir).exists() and 
                len(os.listdir(new_exp_dir_input_dir)) != 0):
                    if pathlib.Path(f"{new_exp_dir_output_dir}/{self.config['runscript']}_out.h5").exists():
                        # check if new_exp_dir_input_dir has files in it
                        print(f"Input data already exists on {dev_type}: {new_exp_dir_input_dir}")
                    else:
                        if self.config['prep_hdf5'] == False:
                            cmd = f"cp {orig_path}/output_data/{self.config['runscript']}_out.h5 {new_exp_dir_output_dir}"
                            print(f"Copying data to {dev_type}: {cmd}")
                            Exec(cmd,LocalExecInfo(env=self.mod_env,))
                        else:
                            pass
            else:
            
                # Make experiment_path on NVME
                print(f"Making experiment input path on NVME: {new_exp_dir_input_dir}")
                print(f"Making experiment output path on NVME: {new_exp_dir_output_dir}")
                pathlib.Path(new_exp_dir_input_dir).mkdir(parents=True, exist_ok=True)
                pathlib.Path(new_exp_dir_output_dir).mkdir(parents=True, exist_ok=True)
                
                # Move data to NVME
                cmd = f"cp -r {orig_path}/input_data/{self.config['runscript']}/* {new_exp_dir_input_dir}"
                print(f"Copying data to {dev_type}: {cmd}")
                Exec(cmd,LocalExecInfo(env=self.mod_env,))
                
                if self.config['prep_hdf5'] == False:
                    cmd = f"cp {orig_path}/output_data/{self.config['runscript']}_out.h5 {new_exp_dir_output_dir}"
                    print(f"Copying data to {dev_type}: {cmd}")
                    Exec(cmd,LocalExecInfo(env=self.mod_env,))
                
            Exec(f"ls -l {new_exp_dir_input_dir}",LocalExecInfo(env=self.mod_env,))
            Exec(f"ls -l {new_exp_dir_output_dir}",LocalExecInfo(env=self.mod_env,))
            self.config['local_exp_dir'] = new_exp_dir
    
    def _train(self):
        """
        Run the ARLDM training run
        """
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
    
    def _sample(self):
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
        
        self._stagein_h5_data()
        self._configure_yaml()
        
        print(f"ARLDM start")
        
        start = time.time()
        
        if self.config['prep_hdf5']:
            self._prep_hdf5_file()
        
        if self.config['mode'] == 'train':
            self._train()
        
        if self.config['mode'] == 'sample':
            self._sample()
        
        end = time.time()
        diff = end - start
        self.log(f'TOTAL RUN TIME: {diff} seconds')


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
