"""
Unit tests for container execution classes in jarvis_cd.shell.container_exec.

Tests verify command construction only — no actual container commands are run.
"""
import sys
import os
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

from jarvis_cd.shell.container_exec import (
    PodmanContainerExec,
    DockerContainerExec,
    ContainerExec,
)
from jarvis_cd.shell.exec_info import LocalExecInfo


def _dry_run_info():
    return LocalExecInfo(dry_run=True)


class TestPodmanContainerExec(unittest.TestCase):
    """Tests for PodmanContainerExec command construction."""

    def test_get_cmd_basic(self):
        """get_cmd() contains 'podman exec', the container name, and the command."""
        exec_info = _dry_run_info()
        pod = PodmanContainerExec('mycontainer', 'echo hello', exec_info)
        cmd = pod.get_cmd()
        self.assertIn('podman exec', cmd)
        self.assertIn('mycontainer', cmd)
        self.assertIn('echo hello', cmd)

    def test_get_cmd_escapes_single_quotes(self):
        """Single quotes in the command are escaped so the shell won't break."""
        exec_info = _dry_run_info()
        pod = PodmanContainerExec('mycontainer', "echo it's alive", exec_info)
        cmd = pod.get_cmd()
        # The raw single-quote should not appear unescaped inside the bash -c '…' wrapper
        # After escaping, the original "it's" becomes "it'\\''s"
        self.assertIn("it'\\''s", cmd)

    def test_get_cmd_uses_bash(self):
        """get_cmd() wraps the command with bash -c."""
        exec_info = _dry_run_info()
        pod = PodmanContainerExec('mycontainer', 'ls -la', exec_info)
        cmd = pod.get_cmd()
        self.assertIn('bash -c', cmd)


class TestDockerContainerExec(unittest.TestCase):
    """Tests for DockerContainerExec command construction."""

    def test_get_cmd_basic(self):
        """get_cmd() contains 'docker exec', the container name, and the command."""
        exec_info = _dry_run_info()
        dock = DockerContainerExec('mycontainer', 'echo hello', exec_info)
        cmd = dock.get_cmd()
        self.assertIn('docker exec', cmd)
        self.assertIn('mycontainer', cmd)
        self.assertIn('echo hello', cmd)

    def test_get_cmd_uses_bash(self):
        """get_cmd() wraps the command with bash -c."""
        exec_info = _dry_run_info()
        dock = DockerContainerExec('mycontainer', 'ls -la', exec_info)
        cmd = dock.get_cmd()
        self.assertIn('bash -c', cmd)

    def test_get_cmd_escapes_single_quotes(self):
        """Single quotes in the command are escaped."""
        exec_info = _dry_run_info()
        dock = DockerContainerExec('mycontainer', "echo it's alive", exec_info)
        cmd = dock.get_cmd()
        self.assertIn("it'\\''s", cmd)


class TestContainerExecRouter(unittest.TestCase):
    """Tests for ContainerExec routing logic."""

    def test_selects_podman_when_available(self):
        """When only podman is in PATH, delegate is PodmanContainerExec."""
        def fake_which(name):
            return '/usr/bin/podman' if name == 'podman' else None

        exec_info = _dry_run_info()
        with patch('shutil.which', side_effect=fake_which):
            ce = ContainerExec('mycontainer', 'echo hello', exec_info)
        self.assertIsInstance(ce.delegate, PodmanContainerExec)

    def test_selects_docker_when_podman_unavailable(self):
        """When only docker is in PATH, delegate is DockerContainerExec."""
        def fake_which(name):
            return '/usr/bin/docker' if name == 'docker' else None

        exec_info = _dry_run_info()
        with patch('shutil.which', side_effect=fake_which):
            ce = ContainerExec('mycontainer', 'echo hello', exec_info)
        self.assertIsInstance(ce.delegate, DockerContainerExec)

    def test_prefers_podman_when_flag_set(self):
        """With prefer_podman=True and both runtimes available, podman is selected."""
        def fake_which(name):
            return f'/usr/bin/{name}' if name in ('docker', 'podman') else None

        exec_info = _dry_run_info()
        with patch('shutil.which', side_effect=fake_which):
            ce = ContainerExec('mycontainer', 'echo hello', exec_info,
                               prefer_podman=True)
        self.assertIsInstance(ce.delegate, PodmanContainerExec)

    def test_docker_preferred_over_podman_by_default(self):
        """Without prefer_podman, docker is selected when both are available."""
        def fake_which(name):
            return f'/usr/bin/{name}' if name in ('docker', 'podman') else None

        exec_info = _dry_run_info()
        with patch('shutil.which', side_effect=fake_which):
            ce = ContainerExec('mycontainer', 'echo hello', exec_info)
        self.assertIsInstance(ce.delegate, DockerContainerExec)

    def test_raises_when_neither_available(self):
        """RuntimeError is raised when neither docker nor podman is found."""
        exec_info = _dry_run_info()
        with patch('shutil.which', return_value=None):
            with self.assertRaises(RuntimeError):
                ContainerExec('mycontainer', 'echo hello', exec_info)

    def test_get_cmd_delegates_to_implementation(self):
        """After routing, get_cmd() returns a non-empty string."""
        def fake_which(name):
            return '/usr/bin/docker' if name == 'docker' else None

        exec_info = _dry_run_info()
        with patch('shutil.which', side_effect=fake_which):
            ce = ContainerExec('mycontainer', 'echo hello', exec_info)
        cmd = ce.get_cmd()
        self.assertIsInstance(cmd, str)
        self.assertTrue(len(cmd) > 0)


if __name__ == '__main__':
    unittest.main()
