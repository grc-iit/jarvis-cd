"""
This module provides classes and methods to launch the LabstorIpcTest application.
LabstorIpcTest is ....
"""
from jarvis_cd.basic.pkg import Application
from jarvis_util import *


class LabstorIpcTest(Application):
    """
    This class provides methods to launch the LabstorIpcTest application.
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
                'name': 'TEST_CASE',
                'msg': 'Destroy previous configuration and rebuild',
                'type': str,
                'default': 'TestIpc'
            }
        ]

    def configure(self, **kwargs):
        """
        Converts the Jarvis configuration to application-specific configuration.
        E.g., OrangeFS produces an orangefs.xml file.

        :param kwargs: Configuration parameters for this pkg.
        :return: None
        """
        self.update_config(kwargs, rebuild=False)

    def start(self):
        """
        Launch an application. E.g., OrangeFS will launch the servers, clients,
        and metadata services on all necessary pkgs.

        :return: None
        """
        test_ipc_execs = ['TestIpc', 'TestIO']
        test_hermes_execs = [
            'TestHermesPut1n', 'TestHermesPut', 'TestHermesPutGet',
            'TestHermesPartialPutGet', 'TestHermesBlobDestroy',
            'TestHermesBucketDestroy', 'TestHermesReorganizeBlob',
            'TestHermesBucketAppend', 'TestHermesBucketAppend1n',
            'TestHermesConnect', 'TestHermesGetContainedBlobIds',
            'TestHermesMultiGetBucket'
        ]
        test_latency_execs = ['TestRoundTripLatency',
                              'TestHshmQueueAllocateEmplacePop',
                              'TestWorkerLatency']
        print(self.config['TEST_CASE'])
        if self.config['TEST_CASE'] in test_ipc_execs:
            Exec(f'test_ipc_exec {self.config["TEST_CASE"]}',
                 MpiExecInfo(hostfile=self.jarvis.hostfile,
                             nprocs=len(self.jarvis.hostfile),
                             ppn=1,
                             env=self.env))
        elif self.config['TEST_CASE'] in test_hermes_execs:
            Exec(f'test_hermes_exec {self.config["TEST_CASE"]}',
                 MpiExecInfo(hostfile=self.jarvis.hostfile,
                             nprocs=len(self.jarvis.hostfile),
                             ppn=1,
                             env=self.env))
        elif self.config['TEST_CASE'] in test_latency_execs:
            Exec(f'test_performance_exec {self.config["TEST_CASE"]}',
                 LocalExecInfo(env=self.env))

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
