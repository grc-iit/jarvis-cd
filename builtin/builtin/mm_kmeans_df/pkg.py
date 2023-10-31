"""
This module provides classes and methods to launch the MmKmeansDf application.
MmKmeansDf is ....
"""
from jarvis_cd.basic.pkg import Application
from jarvis_util import *


class MmKmeansDf(Application):
    """
    This class provides methods to launch the MmKmeansDf application.
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
                'default': None,
            },
            {
                'name': 'df_size',
                'msg': 'The output path',
                'type': str,
                'default': '16g',
            },
            {
                'name': 'window_size',
                'msg': 'The output path',
                'type': str,
                'default': '256m',
            },
            {
                'name': 'nprocs',
                'msg': 'The output path',
                'type': str,
                'default': '16',
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
            'kmeans_df',
            self.config['path'],
            self.config['df_size'],
            self.config['window_size']
        ]
        cmd = ' '.join(cmd)
        Exec(cmd, MpiExecInfo(nprocs=self.config['nprocs']))

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
        Rm(f'{self.config["path"]}*')
