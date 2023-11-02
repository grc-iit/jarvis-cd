"""
This module provides classes and methods to launch the HermesPosixTests application.
HermesPosixTests is ....
"""
from jarvis_cd.basic.pkg import Application
from jarvis_util import *


class HermesPosixTests(Application):
    """
    This class provides methods to launch the HermesPosixTests application.
    """
    def _init(self):
        """
        Initialize paths
        """
        self.choices = [
            'posix_basic',
            'hermes_posix_basic_small',
            'hermes_posix_basic_large',
            'posix_basic_mpi',
            'hermes_posix_basic_mpi_small',
            'hermes_posix_basic_mpi_large',
            'posix_simple_io_omp',
            'hermes_posix_simple_io_omp',
        ]

    def _configure_menu(self):
        """
        Create a CLI menu for the configurator method.
        For thorough documentation of these parameters, view:
        https://github.com/scs-lab/jarvis-util/wiki/3.-Argument-Parsing

        :return: List(dict)
        """
        return [
            {
                'name': 'TEST_CASE',
                'msg': '',
                'type': str,
                'default': None,
                'choices': self.choices,
            }
        ]

    def _configure(self, **kwargs):
        """
        Converts the Jarvis configuration to application-specific configuration.
        E.g., OrangeFS produces an orangefs.xml file.

        :param kwargs: Configuration parameters for this pkg.
        :return: None
        """
        pass

    def start(self):
        """
        Launch an application. E.g., OrangeFS will launch the servers, clients,
        and metadata services on all necessary pkgs.

        :return: None
        """
        test = getattr(self, f'test_{self.config["TEST_CASE"]}')
        code = 1
        if test:
            code = test()
        self.exit_code = code

    def test_posix_basic(self):
        node = Exec('posix_adapter_test',
                    LocalExecInfo(env=self.mod_env))
        return node.exit_code

    def test_hermes_posix_basic_small(self):
        posix_cmd = [
            'hermes_posix_adapter_test',
            '~[request_size=range-small]',
            '--reporter compact -d yes'
        ]
        posix_cmd = ''.join(posix_cmd)
        node = Exec(posix_cmd,
                    LocalExecInfo(env=self.mod_env,
                                  do_dbg=self.config['do_dbg'],
                                  dbg_port=self.config['dbg_port']))
        return node.exit_code

    def test_hermes_posix_basic_large(self):
        posix_cmd = [
            'hermes_posix_adapter_test',
            '~[request_size=range-large]',
            '--reporter compact -d yes'
        ]
        posix_cmd = ''.join(posix_cmd)
        node = Exec(posix_cmd,
                    LocalExecInfo(env=self.mod_env,
                                  do_dbg=self.config['do_dbg'],
                                  dbg_port=self.config['dbg_port']))
        return node.exit_code

    def test_posix_basic_mpi(self):
        node = Exec('posix_adapter_test',
                    MpiExecInfo(nprocs=2,
                                hostfile=self.jarvis.hostfile,
                                env=self.mod_env,
                                do_dbg=self.config['do_dbg'],
                                dbg_port=self.config['dbg_port']))
        return node.exit_code

    def test_hermes_posix_basic_mpi_small(self):
        posix_cmd = [
            'hermes_posix_adapter_mpi_test',
            '~[request_size=range-small]',
            '--reporter compact -d yes'
        ]
        posix_cmd = ''.join(posix_cmd)
        node = Exec(posix_cmd,
                    MpiExecInfo(nprocs=2,
                                hostfile=self.jarvis.hostfile,
                                env=self.mod_env,
                                do_dbg=self.config['do_dbg'],
                                dbg_port=self.config['dbg_port']))
        return node.exit_code

    def test_hermes_posix_basic_mpi_large(self):
        posix_cmd = [
            'hermes_posix_adapter_mpi_test',
            '~[request_size=range-large]',
            '--reporter compact -d yes'
        ]
        posix_cmd = ''.join(posix_cmd)
        node = Exec(posix_cmd,
                    MpiExecInfo(nprocs=2,
                                hostfile=self.jarvis.hostfile,
                                env=self.mod_env,
                                do_dbg=self.config['do_dbg'],
                                dbg_port=self.config['dbg_port']))
        return node.exit_code

    def test_posix_simple_io_omp(self):
        posix_cmd = [
            'posix_simple_io_omp',
            '/tmp/test_hermes/hi.txt 0 1024 8 0'
        ]
        posix_cmd = ''.join(posix_cmd)
        node = Exec(posix_cmd,
                    MpiExecInfo(nprocs=2,
                                hostfile=self.jarvis.hostfile,
                                env=self.mod_env,
                                do_dbg=self.config['do_dbg'],
                                dbg_port=self.config['dbg_port']))
        return node.exit_code

    def test_hermes_posix_simple_io_omp(self):
        posix_cmd = [
            'hermes_posix_simple_io_omp',
            '/tmp/test_hermes/hi.txt 0 1024 8 0'
        ]
        posix_cmd = ''.join(posix_cmd)
        node = Exec(posix_cmd,
                    MpiExecInfo(nprocs=2,
                                hostfile=self.jarvis.hostfile,
                                env=self.mod_env,
                                do_dbg=self.config['do_dbg'],
                                dbg_port=self.config['dbg_port']))
        return node.exit_code

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
