"""
This module provides classes and methods to launch the PreWrf application.
PreWrf is ....
"""
from jarvis_cd.basic.pkg import Application
from jarvis_util import *
from jarvis_util.shell.filesystem import Chmod


class PreWrf(Application):
    """
    This class provides methods to launch the PreWrf application.
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
                'name': 'wrf_location',
                'msg': 'The location of wrf.exe',
                'type': str,
                'default': None,
            },
            {
                'name': 'dataset_location',
                'msg': 'The location of dataset',
                'type': str,
                'default': None,
            },
            {
                'name': 'dataset_size',
                'msg': 'The size of dataset, small, medium and large',
                'type': str,
                'default': 'small',
            },
        ]

    def configure(self, **kwargs):
        """
        Converts the Jarvis configuration to application-specific configuration.
        E.g., OrangeFS produces an orangefs.xml file.

        :param kwargs: Configuration parameters for this pkg.
        :return: None
        """
        self.update_config(kwargs, rebuild=False)
        replacement = [('dataset_location', self.config['dataset_location']),
                       ('wrf_location', self.config['wrf_location'])]
        self.copy_template_file(f'{self.pkg_dir}/config/download.sh',
                                f'{self.config["dataset_location"]}/download.sh', replacement)

    def start(self):
        """
        Launch an application. E.g., OrangeFS will launch the servers, clients,
        and metadata services on all necessary pkgs.

        :return: None
        """
        bash_path = f'{self.config["dataset_location"]}/download.sh'
        Chmod(bash_path, '+x')
        Exec(bash_path)
        pass

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
