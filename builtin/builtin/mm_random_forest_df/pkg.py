"""
This module provides classes and methods to launch the MmRandomForestDf application.
MmRandomForestDf is ....
"""
from jarvis_cd.basic.pkg import Application
from jarvis_util import *


class MmRandomForestDf(Application):
    """
    This class provides methods to launch the MmRandomForestDf application.
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
                'name': 'path',
                'msg': 'The output path',
                'type': str,
                'default': '/tmp/mm.bin',
            },
            {
                'name': 'k',
                'msg': 'The number of centers to create',
                'type': str,
                'default': '8',
            },
            {
                'name': 'df_size',
                'msg': 'The total size of data',
                'type': str,
                'default': '16m',
            },
            {
                'name': 'window_size',
                'msg': 'The size of a window of data',
                'type': str,
                'default': '1m',
            },
            {
                'name': 'type',
                'msg': 'The output file type',
                'type': str,
                'default': 'shared',
                'choices': ['shared', 'parquet', 'hdf5']
            },
            {
                'name': 'nprocs',
                'msg': 'Number of procs',
                'type': int,
                'default': 1,
            },
            {
                'name': 'ppn',
                'msg': 'Processes per node',
                'type': int,
                'default': 16,
            },
        ]

    def _configure(self, **kwargs):
        """
        Converts the Jarvis configuration to application-specific configuration.
        E.g., OrangeFS produces an orangefs.xml file.

        :param kwargs: Configuration parameters for this pkg.
        :return: None
        """
        self.path = self.config['path']
        Mkdir(str(pathlib.Path(self.path).parent),
              LocalExecInfo(env=self.env))

    def start(self):
        """
        Launch an application. E.g., OrangeFS will launch the servers, clients,
        and metadata services on all necessary pkgs.

        :return: None
        """
        cmd = [
            'mm_random_forest_df',
            self.config['k'],
            self.config['path'],
            self.config['df_size'],
            self.config['window_size'],
            self.config['type']
        ]
        print(cmd)
        cmd = ' '.join(cmd)
        Exec(cmd, MpiExecInfo(nprocs=self.config['nprocs'],
                              ppn=self.config['ppn'],
                              do_dbg=self.config['do_dbg'],
                              dbg_port=self.config['dbg_port']))

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
