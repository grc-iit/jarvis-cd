"""
Unit tests for Exec._prepare_container() and Exec._resolve_exec_info()
from jarvis_cd.shell.exec_factory.
"""
import unittest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

from jarvis_cd.shell.exec_factory import Exec
from jarvis_cd.shell.exec_info import ExecInfo, ExecType
from jarvis_cd.util.hostfile import Hostfile


def make_exec(exec_info: ExecInfo) -> Exec:
    """Create an Exec instance without running it."""
    obj = Exec.__new__(Exec)
    # Initialise the minimal CoreExec state that __init__ would set
    obj.cmd = 'echo hello'
    obj.exec_info = exec_info
    obj._delegate = None
    return obj


class TestPrepareContainerNone(unittest.TestCase):
    """Tests for _prepare_container with container='none' or no container."""

    def test_no_container_returns_unchanged(self):
        """container='none' → returns cmd unchanged and same exec_info."""
        info = ExecInfo(exec_type=ExecType.LOCAL, container='none')
        exec_obj = make_exec(info)
        cmd = 'echo hello'
        returned_cmd, returned_info = exec_obj._prepare_container(cmd)
        self.assertEqual(returned_cmd, cmd)
        self.assertIs(returned_info, info)


class TestPrepareContainerApptainer(unittest.TestCase):
    """Tests for _prepare_container with container='apptainer'."""

    def _make_apptainer_exec(self, container_image='myimg', env=None,
                              shared_dir=None, bind_mounts=None):
        info = ExecInfo(
            exec_type=ExecType.LOCAL,
            container='apptainer',
            container_image=container_image,
            env=env or {},
            shared_dir=shared_dir,
            bind_mounts=bind_mounts or [],
        )
        return make_exec(info)

    def test_apptainer_wraps_with_apptainer_exec(self):
        """container='apptainer', container_image='myimg', shared_dir set →
        wrapped cmd contains 'apptainer exec' and 'myimg.sif'."""
        exec_obj = self._make_apptainer_exec(
            container_image='myimg', shared_dir='/shared')
        wrapped_cmd, _ = exec_obj._prepare_container('echo hello')
        self.assertIn('apptainer exec', wrapped_cmd)
        self.assertIn('myimg.sif', wrapped_cmd)

    def test_apptainer_wraps_with_shared_dir_path(self):
        """When shared_dir is set, SIF path is resolved from the parent of shared_dir."""
        exec_obj = self._make_apptainer_exec(
            container_image='myimg', shared_dir='/shared/mypkg')
        wrapped_cmd, _ = exec_obj._prepare_container('echo hello')
        self.assertIn('/shared/myimg.sif', wrapped_cmd)

    def test_apptainer_passes_env_vars(self):
        """container='apptainer', env={'MY_VAR': 'val'} → wrapped cmd contains
        '--env MY_VAR='."""
        exec_obj = self._make_apptainer_exec(env={'MY_VAR': 'val'})
        wrapped_cmd, _ = exec_obj._prepare_container('echo hello')
        self.assertIn('--env MY_VAR=', wrapped_cmd)

    def test_apptainer_skips_host_only_vars(self):
        """env={'PATH': '/foo', 'MY_VAR': 'val'} → wrapped cmd does NOT contain
        '--env PATH='."""
        exec_obj = self._make_apptainer_exec(
            env={'PATH': '/foo', 'MY_VAR': 'val'})
        wrapped_cmd, _ = exec_obj._prepare_container('echo hello')
        self.assertNotIn('--env PATH=', wrapped_cmd)
        self.assertIn('--env MY_VAR=', wrapped_cmd)

    def test_apptainer_removes_env_from_returned_info(self):
        """Returned exec_info should have an empty env dict."""
        exec_obj = self._make_apptainer_exec(env={'MY_VAR': 'val'})
        _, returned_info = exec_obj._prepare_container('echo hello')
        self.assertEqual(returned_info.env, {})

    def test_apptainer_gpu_flag(self):
        """When gpu=True, '--nv' appears in the wrapped command."""
        info = ExecInfo(
            exec_type=ExecType.LOCAL,
            container='apptainer',
            container_image='gpuimg',
            gpu=True,
        )
        exec_obj = make_exec(info)
        wrapped_cmd, _ = exec_obj._prepare_container('echo hello')
        self.assertIn('--nv', wrapped_cmd)

    def test_apptainer_no_gpu_flag_when_false(self):
        """When gpu=False, '--nv' does NOT appear."""
        exec_obj = self._make_apptainer_exec()
        wrapped_cmd, _ = exec_obj._prepare_container('echo hello')
        self.assertNotIn('--nv', wrapped_cmd)

    def test_apptainer_bind_mounts(self):
        """bind_mounts entries appear as '--bind ...' in the wrapped command."""
        exec_obj = self._make_apptainer_exec(bind_mounts=['/host/path:/container/path'])
        wrapped_cmd, _ = exec_obj._prepare_container('echo hello')
        self.assertIn('--bind /host/path:/container/path', wrapped_cmd)


class TestPrepareContainerDocker(unittest.TestCase):
    """Tests for _prepare_container with container='docker'."""

    def _make_docker_exec(self, container_image='myimg', env=None):
        info = ExecInfo(
            exec_type=ExecType.LOCAL,
            container='docker',
            container_image=container_image,
            env=env or {},
        )
        return make_exec(info)

    def test_docker_wraps_with_docker_exec(self):
        """container='docker', container_image='myimg' → wrapped cmd contains
        'docker exec' and 'myimg_container'."""
        exec_obj = self._make_docker_exec(container_image='myimg')
        wrapped_cmd, _ = exec_obj._prepare_container('echo hello')
        self.assertIn('docker exec', wrapped_cmd)
        self.assertIn('myimg_container', wrapped_cmd)

    def test_docker_passes_env_with_e_flag(self):
        """container='docker', env={'X': 'y'} → wrapped cmd contains '-e X='."""
        exec_obj = self._make_docker_exec(env={'X': 'y'})
        wrapped_cmd, _ = exec_obj._prepare_container('echo hello')
        self.assertIn('-e X=', wrapped_cmd)

    def test_docker_skips_host_only_vars(self):
        """HOST-only vars like PATH should not be forwarded via -e."""
        exec_obj = self._make_docker_exec(env={'PATH': '/usr/bin', 'APP_VAR': 'v'})
        wrapped_cmd, _ = exec_obj._prepare_container('echo hello')
        self.assertNotIn('-e PATH=', wrapped_cmd)
        self.assertIn('-e APP_VAR=', wrapped_cmd)

    def test_docker_removes_env_from_returned_info(self):
        """Returned exec_info should have empty env."""
        exec_obj = self._make_docker_exec(env={'X': 'y'})
        _, returned_info = exec_obj._prepare_container('echo hello')
        self.assertEqual(returned_info.env, {})


class TestPrepareContainerPodman(unittest.TestCase):
    """Tests for _prepare_container with container='podman'."""

    def _make_podman_exec(self, container_image='myimg', env=None):
        info = ExecInfo(
            exec_type=ExecType.LOCAL,
            container='podman',
            container_image=container_image,
            env=env or {},
        )
        return make_exec(info)

    def test_podman_wraps_with_podman_exec(self):
        """container='podman', container_image='myimg' → wrapped cmd contains
        'podman exec'."""
        exec_obj = self._make_podman_exec(container_image='myimg')
        wrapped_cmd, _ = exec_obj._prepare_container('echo hello')
        self.assertIn('podman exec', wrapped_cmd)

    def test_podman_passes_env_with_e_flag(self):
        """container='podman', env={'X': 'y'} → wrapped cmd contains '-e X='."""
        exec_obj = self._make_podman_exec(env={'X': 'y'})
        wrapped_cmd, _ = exec_obj._prepare_container('echo hello')
        self.assertIn('-e X=', wrapped_cmd)

    def test_podman_removes_env_from_returned_info(self):
        """Returned exec_info should have empty env."""
        exec_obj = self._make_podman_exec(env={'X': 'y'})
        _, returned_info = exec_obj._prepare_container('echo hello')
        self.assertEqual(returned_info.env, {})


class TestResolveExecInfo(unittest.TestCase):
    """Tests for Exec._resolve_exec_info()."""

    def test_local_localhost_stays_local(self):
        """exec_type=LOCAL, hostfile with only 'localhost' → exec_type unchanged."""
        hostfile = Hostfile(hosts=['localhost'], find_ips=False)
        info = ExecInfo(exec_type=ExecType.LOCAL, hostfile=hostfile)
        exec_obj = make_exec(info)
        _, returned_info = exec_obj._resolve_exec_info('echo hello', info)
        self.assertEqual(returned_info.exec_type, ExecType.LOCAL)

    def test_local_remote_host_promotes_to_ssh(self):
        """exec_type=LOCAL, hostfile with a non-localhost host → returned
        exec_info has exec_type=SSH."""
        hostfile = Hostfile(hosts=['remotehost'], find_ips=False)
        info = ExecInfo(exec_type=ExecType.LOCAL, hostfile=hostfile)
        exec_obj = make_exec(info)
        _, returned_info = exec_obj._resolve_exec_info('echo hello', info)
        self.assertEqual(returned_info.exec_type, ExecType.SSH)

    def test_non_local_exec_type_not_changed(self):
        """exec_type=SSH stays SSH regardless of hostfile content."""
        hostfile = Hostfile(hosts=['localhost'], find_ips=False)
        info = ExecInfo(exec_type=ExecType.SSH, hostfile=hostfile)
        exec_obj = make_exec(info)
        _, returned_info = exec_obj._resolve_exec_info('echo hello', info)
        self.assertEqual(returned_info.exec_type, ExecType.SSH)

    def test_local_no_hostfile_stays_local(self):
        """exec_type=LOCAL with no hostfile → stays LOCAL (no crash)."""
        info = ExecInfo(exec_type=ExecType.LOCAL, hostfile=None)
        exec_obj = make_exec(info)
        _, returned_info = exec_obj._resolve_exec_info('echo hello', info)
        self.assertEqual(returned_info.exec_type, ExecType.LOCAL)

    def test_promoted_ssh_uses_subset_of_one(self):
        """When promoted to SSH, the returned hostfile is a subset of 1 host."""
        hostfile = Hostfile(hosts=['node1', 'node2'], find_ips=False)
        info = ExecInfo(exec_type=ExecType.LOCAL, hostfile=hostfile)
        exec_obj = make_exec(info)
        _, returned_info = exec_obj._resolve_exec_info('echo hello', info)
        self.assertEqual(len(returned_info.hostfile), 1)
        self.assertEqual(returned_info.hostfile[0], 'node1')


if __name__ == '__main__':
    unittest.main()
