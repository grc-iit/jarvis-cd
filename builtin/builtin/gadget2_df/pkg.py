"""
This module provides classes and methods to launch the Gadget2Df application.
Gadget2Df is ....
"""
from jarvis_cd.basic.pkg import Application
from jarvis_util import *


class Gadget2Df(Application):
    """
    This class provides methods to launch the Gadget2Df application.
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
                'name': 'nprocs',
                'msg': 'Number of processes to spawn',
                'type': int,
                'default': 1,
            },
            {
                'name': 'ppn',
                'msg': 'Processes per node',
                'type': int,
                'default': None,
            },
            {
                'name': 'j',
                'msg': 'Number of threads to use for building gadget',
                'type': int,
                'default': 8,
            },
            {
                'name': 'tile_fac',
                'msg': 'The tiling factor used to replicate the initial boundary'
                       'conditions',
                'type': int,
                'default': 4,
            },
        ]

    def _configure(self, **kwargs):
        """
        Converts the Jarvis configuration to application-specific configuration.
        E.g., OrangeFS produces an orangefs.xml file.

        :param kwargs: Configuration parameters for this pkg.
        :return: None
        """
        paramfile = f'{self.config_dir}/ics.param'
        self.copy_template_file(f'{self.pkg_dir}/paramfiles/ics.param',
                                paramfile,
                                replacements={
                                    'REPO_DIR': self.env['GADGET2_PATH'],
                                    'TILE_FAC': self.config['tile_fac']
                                })
        build_dir = f'{self.shared_dir}/build'
        cmake_opts = {}
        if 'FFTW_PATH' in self.env:
            cmake_opts['FFTW_PATH'] = self.env['FFTW_PATH']
        Cmake(self.env['GADGET2_PATH'],
              build_dir,
              opts=cmake_opts,
              exec_info=LocalExecInfo(env=self.env))
        Make(build_dir, nthreads=self.config['j'],
             exec_info=LocalExecInfo(env=self.env))

    def start(self):
        """
        Launch an application. E.g., OrangeFS will launch the servers, clients,
        and metadata services on all necessary pkgs.

        :return: None
        """
        build_dir = f'{self.shared_dir}/build'
        paramfile = f'{self.config_dir}/ics.param'
        exec_path = f'{build_dir}/bin/NGenIC'
        ngenic_root = f'{self.env["GADGET2_PATH"]}/N-GenIC'
        Exec(f'{exec_path} {paramfile}',
             MpiExecInfo(nprocs=self.config['nprocs'],
                         ppn=self.config['ppn'],
                         hostfile=self.jarvis.hostfile,
                         env=self.mod_env,
                         cwd=ngenic_root))

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
