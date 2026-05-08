"""
Unit tests for container compose execution classes in
jarvis_cd.shell.container_compose_exec.

Tests verify command construction only — no actual container commands are run.
A temporary compose file is created in setUp so the exists() guard in __init__
is satisfied; no real compose operations take place.
"""
import sys
import os
import unittest
import tempfile
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

from jarvis_cd.shell.container_compose_exec import (
    PodmanBuildExec,
    DockerBuildExec,
    ContainerBuildExec,
    PodmanComposeExec,
    DockerComposeExec,
    ContainerComposeExec,
    ApptainerBuildExec,
    ApptainerExec,
)
from jarvis_cd.shell.exec_info import LocalExecInfo


def _dry_run_info():
    return LocalExecInfo(dry_run=True)


class _TempComposeFileMixin:
    """Mixin that creates a throwaway compose file before each test."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix='jarvis_compose_test_')
        self.compose_file = os.path.join(self._tmpdir, 'docker-compose.yml')
        with open(self.compose_file, 'w') as fh:
            fh.write('version: "3"\nservices: {}\n')

    def tearDown(self):
        import shutil as _shutil
        _shutil.rmtree(self._tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# PodmanBuildExec
# ---------------------------------------------------------------------------

class TestPodmanBuildExec(_TempComposeFileMixin, unittest.TestCase):

    def _make(self):
        # Patch shutil.which so podman-compose appears available (avoids
        # the live 'podman compose --help' subprocess inside get_cmd).
        with patch('shutil.which', side_effect=lambda n: '/usr/bin/podman-compose' if n == 'podman-compose' else None):
            return PodmanBuildExec(self.compose_file, _dry_run_info())

    def test_get_cmd_contains_podman(self):
        """get_cmd() contains 'podman' and the compose file path."""
        pb = self._make()
        with patch('shutil.which', side_effect=lambda n: '/usr/bin/podman-compose' if n == 'podman-compose' else None):
            cmd = pb.get_cmd()
        self.assertIn('podman', cmd)
        self.assertIn(self.compose_file, cmd)

    def test_get_cmd_contains_build(self):
        """get_cmd() contains 'build'."""
        pb = self._make()
        with patch('shutil.which', side_effect=lambda n: '/usr/bin/podman-compose' if n == 'podman-compose' else None):
            cmd = pb.get_cmd()
        self.assertIn('build', cmd)


# ---------------------------------------------------------------------------
# DockerBuildExec
# ---------------------------------------------------------------------------

class TestDockerBuildExec(_TempComposeFileMixin, unittest.TestCase):

    def test_get_cmd_contains_docker_compose_build(self):
        """get_cmd() contains 'docker', the compose file path, and 'build'."""
        db = DockerBuildExec(self.compose_file, _dry_run_info())
        cmd = db.get_cmd()
        self.assertIn('docker', cmd)
        self.assertIn(self.compose_file, cmd)
        self.assertIn('build', cmd)


# ---------------------------------------------------------------------------
# ContainerBuildExec (router)
# ---------------------------------------------------------------------------

class TestContainerBuildExec(_TempComposeFileMixin, unittest.TestCase):

    def test_selects_docker_when_available(self):
        """When docker is in PATH and prefer_podman is False, DockerBuildExec is selected."""
        def fake_which(name):
            return '/usr/bin/docker' if name == 'docker' else None

        with patch('shutil.which', side_effect=fake_which):
            cb = ContainerBuildExec(self.compose_file, _dry_run_info())
        self.assertIsInstance(cb.delegate, DockerBuildExec)

    def test_selects_podman_when_prefer_podman(self):
        """With prefer_podman=True and both runtimes available, PodmanBuildExec is selected."""
        def fake_which(name):
            if name in ('docker', 'podman', 'podman-compose'):
                return f'/usr/bin/{name}'
            return None

        with patch('shutil.which', side_effect=fake_which):
            cb = ContainerBuildExec(self.compose_file, _dry_run_info(),
                                    prefer_podman=True)
        self.assertIsInstance(cb.delegate, PodmanBuildExec)

    def test_get_cmd_delegates(self):
        """After routing, get_cmd() returns a non-empty string."""
        def fake_which(name):
            return '/usr/bin/docker' if name == 'docker' else None

        with patch('shutil.which', side_effect=fake_which):
            cb = ContainerBuildExec(self.compose_file, _dry_run_info())
        cmd = cb.get_cmd()
        self.assertIsInstance(cmd, str)
        self.assertTrue(len(cmd) > 0)


# ---------------------------------------------------------------------------
# PodmanComposeExec
# ---------------------------------------------------------------------------

class TestPodmanComposeExec(_TempComposeFileMixin, unittest.TestCase):

    def _make(self, action='up'):
        with patch('shutil.which', side_effect=lambda n: '/usr/bin/podman-compose' if n == 'podman-compose' else None):
            return PodmanComposeExec(self.compose_file, _dry_run_info(), action=action)

    def test_get_cmd_up_action(self):
        """action='up' produces a command containing 'up' and '--abort-on-container-exit'."""
        pc = self._make(action='up')
        with patch('shutil.which', side_effect=lambda n: '/usr/bin/podman-compose' if n == 'podman-compose' else None):
            cmd = pc.get_cmd()
        self.assertIn('up', cmd)
        self.assertIn('--abort-on-container-exit', cmd)

    def test_get_cmd_down_action(self):
        """action='down' produces a command containing 'down' but not '--abort-on-container-exit'."""
        pc = self._make(action='down')
        with patch('shutil.which', side_effect=lambda n: '/usr/bin/podman-compose' if n == 'podman-compose' else None):
            cmd = pc.get_cmd()
        self.assertIn('down', cmd)
        self.assertNotIn('--abort-on-container-exit', cmd)


# ---------------------------------------------------------------------------
# DockerComposeExec
# ---------------------------------------------------------------------------

class TestDockerComposeExec(_TempComposeFileMixin, unittest.TestCase):

    def test_get_cmd_up_action(self):
        """action='up' produces a command containing 'up' and '--abort-on-container-exit'."""
        dc = DockerComposeExec(self.compose_file, _dry_run_info(), action='up')
        cmd = dc.get_cmd()
        self.assertIn('up', cmd)
        self.assertIn('--abort-on-container-exit', cmd)

    def test_get_cmd_down_action(self):
        """action='down' produces a command containing 'down' but not '--abort-on-container-exit'."""
        dc = DockerComposeExec(self.compose_file, _dry_run_info(), action='down')
        cmd = dc.get_cmd()
        self.assertIn('down', cmd)
        self.assertNotIn('--abort-on-container-exit', cmd)


# ---------------------------------------------------------------------------
# ContainerComposeExec (router)
# ---------------------------------------------------------------------------

class TestContainerComposeExec(_TempComposeFileMixin, unittest.TestCase):

    def test_selects_docker(self):
        """When docker is in PATH and prefer_podman is False, DockerComposeExec is selected."""
        def fake_which(name):
            return '/usr/bin/docker' if name == 'docker' else None

        with patch('shutil.which', side_effect=fake_which):
            cc = ContainerComposeExec(self.compose_file, _dry_run_info())
        self.assertIsInstance(cc.delegate, DockerComposeExec)

    def test_get_cmd_delegates(self):
        """After routing, get_cmd() returns a non-empty string."""
        def fake_which(name):
            return '/usr/bin/docker' if name == 'docker' else None

        with patch('shutil.which', side_effect=fake_which):
            cc = ContainerComposeExec(self.compose_file, _dry_run_info())
        cmd = cc.get_cmd()
        self.assertIsInstance(cmd, str)
        self.assertTrue(len(cmd) > 0)


# ---------------------------------------------------------------------------
# ApptainerBuildExec
# ---------------------------------------------------------------------------

class TestApptainerBuildExec(unittest.TestCase):

    def test_get_cmd_contains_apptainer_build(self):
        """get_cmd() contains 'apptainer' and 'build'."""
        ab = ApptainerBuildExec('myimage:latest', '/tmp/my.sif', _dry_run_info())
        cmd = ab.get_cmd()
        self.assertIn('apptainer', cmd)
        self.assertIn('build', cmd)
        self.assertIn('/tmp/my.sif', cmd)

    def test_get_cmd_uses_docker_daemon_uri(self):
        """source='docker-daemon' produces a cmd containing 'docker-daemon://'."""
        ab = ApptainerBuildExec('myimage:latest', '/tmp/my.sif', _dry_run_info(),
                                source='docker-daemon')
        cmd = ab.get_cmd()
        self.assertIn('docker-daemon://', cmd)

    def test_get_cmd_appends_latest_tag_when_missing(self):
        """Image name without a tag gets ':latest' appended automatically."""
        ab = ApptainerBuildExec('myimage', '/tmp/my.sif', _dry_run_info())
        cmd = ab.get_cmd()
        self.assertIn('myimage:latest', cmd)

    def test_get_cmd_preserves_existing_tag(self):
        """Image name that already has a tag is not modified."""
        ab = ApptainerBuildExec('myimage:v1.0', '/tmp/my.sif', _dry_run_info())
        cmd = ab.get_cmd()
        self.assertIn('myimage:v1.0', cmd)
        self.assertNotIn('myimage:v1.0:latest', cmd)


# ---------------------------------------------------------------------------
# ApptainerExec
# ---------------------------------------------------------------------------

class TestApptainerExec(unittest.TestCase):

    def test_get_cmd_basic(self):
        """get_cmd() contains 'apptainer exec', the sif_path, and the command."""
        ae = ApptainerExec('/tmp/my.sif', 'echo hello', _dry_run_info())
        cmd = ae.get_cmd()
        self.assertIn('apptainer exec', cmd)
        self.assertIn('/tmp/my.sif', cmd)
        self.assertIn('echo hello', cmd)

    def test_get_cmd_gpu_flag(self):
        """gpu=True adds '--nv' to the command."""
        ae = ApptainerExec('/tmp/my.sif', 'echo hello', _dry_run_info(), gpu=True)
        cmd = ae.get_cmd()
        self.assertIn('--nv', cmd)

    def test_get_cmd_no_gpu_flag_by_default(self):
        """gpu=False (default) does NOT add '--nv'."""
        ae = ApptainerExec('/tmp/my.sif', 'echo hello', _dry_run_info())
        cmd = ae.get_cmd()
        self.assertNotIn('--nv', cmd)

    def test_get_cmd_bind_paths(self):
        """bind_paths list adds '--bind' flags to the command."""
        ae = ApptainerExec('/tmp/my.sif', 'echo hello', _dry_run_info(),
                           bind_paths=['/host/path1:/container/path1',
                                       '/host/path2:/container/path2'])
        cmd = ae.get_cmd()
        self.assertIn('--bind', cmd)
        self.assertIn('/host/path1:/container/path1', cmd)
        self.assertIn('/host/path2:/container/path2', cmd)


if __name__ == '__main__':
    unittest.main()
