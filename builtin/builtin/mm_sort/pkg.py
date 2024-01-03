"""
This module provides classes and methods to launch the MmSort application.
MmSort is ....
"""
from jarvis_cd.basic.pkg import Application
from jarvis_util import *


class MmSort(Application):
    """
    This class provides methods to launch the MmSort application.
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
                'msg': 'The input data path',
                'type': str,
                'default': None,
            },
            {
                'name': 'memory',
                'msg': 'The max amount of memory to consume',
                'type': str,
                'default': '1g',
            },
            {
                'name': 'nprocs',
                'msg': 'Number of processes to spawn',
                'type': int,
                'default': 16,
            },
            {
                'name': 'ppn',
                'msg': 'Number of processes per node',
                'type': int,
                'default': 16,
            },
            {
                'name': 'api',
                'msg': 'The implementation to use',
                'type': str,
                'default': 'spark',
                'choices': ['spark', 'mmap', 'mega']
            },
            {
                'name': 'scratch',
                'msg': 'Where spark buffers data',
                'type': str,
                'default': '${HOME}/sparktmp/',
            },
        ]

    def _configure(self, **kwargs):
        """
        Converts the Jarvis configuration to application-specific configuration.
        E.g., OrangeFS produces an orangefs.xml file.

        :param kwargs: Configuration parameters for this pkg.
        :return: None
        """
        self.config['scratch'] = os.path.expandvars(self.config['scratch'])
        Mkdir(self.config['scratch'],
              PsshExecInfo(hosts=self.jarvis.hostfile))

    def start(self):
        """
        Launch an application. E.g., OrangeFS will launch the servers, clients,
        and metadata services on all necessary pkgs.

        :return: None
        """
        mm_sort = ['mmap', 'mega']
        if self.config['api'] == 'spark':
            cmd = [
                'spark-submit',
                f'--driver-memory 2g',
                f'--executor-memory {self.config["memory"]}',
                f'--conf spark.speculation=false',
                f'--conf spark.storage.replication=1',
                f'--conf spark.local.dir={self.config["scratch"]}',
                f'{self.env["MM_PATH"]}/benchmark/mm_sort.py',
                self.config['path']
            ]
            cmd = ' '.join(cmd)
            Exec(cmd, LocalExecInfo(env=self.env))
        elif self.config['api'] in mm_sort:
            cmd = [
                'mm_sort',
                self.config['api'],
                self.config['path'],
                self.config['memory'],
            ]
            cmd = ' '.join(cmd)
            Exec(cmd, MpiExecInfo(env=self.env,
                                  nprocs=self.config['nprocs'],
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
