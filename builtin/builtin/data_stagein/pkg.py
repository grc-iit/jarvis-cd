"""
This module provides classes and methods to launch the DataStagein application.
DataStagein is ....
"""
from jarvis_cd.basic.pkg import Application
from jarvis_util import *
import os
import pathlib


class DataStagein(Application):
    """
    This class provides methods to launch the DataStagein application.
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
                'name': 'user_data_paths',
                'msg': 'List of paths of user datas to stage in, delimitated by comma',
                'type': str,
                'default': None,
            },
            {
                'name': 'dest_data_path',
                'msg': 'The destination path for all user datas to be staged in',
                'type': str,
                'default': None,
            },
            {
                'name': 'user_data_list',
                'msg': 'List of paths of user datas to stage in',
                'type': list,
                'default': None,
            },
            {
                'name': 'mkdir_datapaths',
                'msg': 'List of paths tp create if it does not exist, delimitated by comma',
                'type': str,
                'default': None,
            },
            {
                'name': 'mkdir_datapaths_list',
                'msg': 'List of paths tp create if it does not exist',
                'type': list,
                'default': None,
            }
        ]

    def _configure(self, **kwargs):
        """
        Converts the Jarvis configuration to application-specific configuration.
        E.g., OrangeFS produces an orangefs.xml file.

        :param kwargs: Configuration parameters for this pkg.
        :return: None
        """
        user_data_paths = self.config['user_data_paths']
        
        # Convert user_data_paths to list
        if user_data_paths is not None:
            self.config['user_data_list'] = user_data_paths.split(',')
        
        # Convert mkdir_datapaths to list
        mkdir_datapaths = self.config['mkdir_datapaths']
        if mkdir_datapaths is not None:
            self.config['mkdir_datapaths_list'] = mkdir_datapaths.split(',')
        
        self.config['dest_data_path'] = self.config['dest_data_path']
    
    def start(self):
        """
        Launch an application. E.g., OrangeFS will launch the servers, clients,
        and metadata services on all necessary pkgs.

        :return: None
        """
        print("Data stagein starting")
        
        user_data_list = self.config['user_data_list']
        dest_data_path = self.config['dest_data_path']
        mkdir_datapaths_list = self.config['mkdir_datapaths_list']
        
        # create paths for user
        for datapath in mkdir_datapaths_list:
            if not pathlib.Path(datapath).exists():
                pathlib.Path(datapath).mkdir(parents=True, exist_ok=True)
            else:
                print(f"Path {datapath} already exists")
        
        # create destination path if it does not exist
        if not pathlib.Path(dest_data_path).exists():
            pathlib.Path(dest_data_path).mkdir(parents=True, exist_ok=True)
        
        for data_path in user_data_list:
            if not os.path.exists(data_path):
                raise FileNotFoundError(f"Data path {data_path} does not exist")
            else:
                # Check if the path is empty (e.g. does not contain any files)
                if len(os.listdir(data_path)) == 0:
                    raise ValueError(f"Data path {data_path} is empty")
            
            # Move data to destination path
            cmd = f"cp -r {data_path} {dest_data_path}"
            print(f"Copying data from {data_path} to {dest_data_path}")
            Exec(cmd,LocalExecInfo(env=self.mod_env,))
            
            Exec(f"ls -l {dest_data_path}",LocalExecInfo(env=self.mod_env,))
            
        print("Data stagein complete")

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
