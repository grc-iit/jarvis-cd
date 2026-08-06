"""Unit tests for OpenMPI launcher MCA injection (distributed pipelines).

OpenMPI 5 (PRRTE) renamed the ssh launch plm and its params: OMPI <=4 uses
plm/rsh + plm_rsh_agent/plm_rsh_args; OMPI 5 uses plm/ssh + plm_ssh_agent/
plm_ssh_args. jarvis assembles the command on the host but it runs inside the
container, so it can't detect the in-container version -- it emits both param
names (each runtime honors the one it knows). The plm component itself is
selected version-agnostically via '--mca plm ^slurm' in exec_factory.
"""
import unittest

from jarvis_cd.shell.mpi_exec import OpenMpiExec
from jarvis_cd.util.hostfile import Hostfile


def _mpicmd(ssh_cmd=None, ssh_port=None, mpi_cmd=None, hosts=('n1', 'n2')):
    """Call OpenMpiExec.mpicmd() without running LocalExec.__init__."""
    obj = object.__new__(OpenMpiExec)
    obj.nprocs = 4
    obj.ppn = 2
    obj.hostfile = Hostfile(hosts=list(hosts), find_ips=False)
    obj.mpi_env = {'PATH': '/nonexistent/bin'}
    obj.ssh_port = ssh_port
    obj.mpi_cmd = mpi_cmd
    obj.ssh_cmd = ssh_cmd
    obj.original_cmd = 'ior -w'
    obj.cmd_list = None
    return obj.mpicmd()


class TestOpenMpiLauncherParams(unittest.TestCase):
    def test_ssh_agent_emits_both_rsh_and_ssh_names(self):
        cmd = _mpicmd(ssh_cmd='env -u LD_LIBRARY_PATH ssh')
        self.assertIn('--mca plm_rsh_agent "env -u LD_LIBRARY_PATH ssh"', cmd)
        self.assertIn('--mca plm_ssh_agent "env -u LD_LIBRARY_PATH ssh"', cmd)

    def test_port_emits_both_rsh_and_ssh_args(self):
        cmd = _mpicmd(ssh_port=2222)
        self.assertIn('--mca plm_rsh_args "-p 2222"', cmd)
        self.assertIn('--mca plm_ssh_args "-p 2222"', cmd)

    def test_port_22_not_injected(self):
        cmd = _mpicmd(ssh_port=22)
        self.assertNotIn('plm_ssh_args', cmd)
        self.assertNotIn('plm_rsh_args', cmd)

    def test_no_ssh_cmd_no_agent(self):
        cmd = _mpicmd(ssh_cmd=None)
        self.assertNotIn('plm_ssh_agent', cmd)
        self.assertNotIn('plm_rsh_agent', cmd)

    def test_inline_host_list_from_pathless_hostfile(self):
        cmd = _mpicmd(ssh_port=2222)
        self.assertIn('--host n1,n2', cmd)


if __name__ == '__main__':
    unittest.main()
