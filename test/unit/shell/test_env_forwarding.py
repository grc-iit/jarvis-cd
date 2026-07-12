import unittest
import sys
import os
import subprocess
from typing import Optional

# Add the project root to the path so we can import jarvis_cd
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from jarvis_cd.shell.core_exec import LocalExec
from jarvis_cd.shell.ssh_exec import SshExec, PsshExec
from jarvis_cd.shell.exec_factory import Exec as MpiExec
from jarvis_cd.shell.exec_info import (
    LocalExecInfo,
    SshExecInfo,
    PsshExecInfo,
    MpiExecInfo,
)
from jarvis_cd.shell.mpi_exec import MpichExec
from jarvis_cd.util.hostfile import Hostfile


_SSH_TIMEOUT_SECONDS = 5
_SSH_LAUNCHER = (
    "ssh -o BatchMode=yes -o ConnectionAttempts=1 "
    "-o ServerAliveInterval=2 -o ServerAliveCountMax=1"
)


def _probe_localhost_ssh() -> tuple[bool, str]:
    """Return whether non-interactive localhost SSH is ready for live tests."""
    command = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectionAttempts=1",
        "-o",
        f"ConnectTimeout={_SSH_TIMEOUT_SECONDS}",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "LogLevel=ERROR",
        "localhost",
        "echo jarvis-ssh-probe",
    ]
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=_SSH_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError:
        return False, "ssh executable is unavailable"
    except subprocess.TimeoutExpired:
        return False, "localhost SSH capability probe timed out"
    if result.returncode != 0:
        diagnostic = (result.stderr or result.stdout).strip()
        return False, diagnostic or f"ssh exited {result.returncode}"
    if "jarvis-ssh-probe" not in result.stdout:
        return False, "localhost SSH probe omitted its response token"
    return True, ""


class TestLocalExecEnvForwarding(unittest.TestCase):
    def setUp(self):
        """Set up test fixtures"""
        self.print_env = os.path.join(os.path.dirname(__file__), "print_env.py")

    def test_single_custom_env_var(self):
        """Test LocalExec forwards single custom environment variable"""
        exec_info = LocalExecInfo(env={"CUSTOM_VAR": "HELLO"})
        local_exec = LocalExec(f"python3 {self.print_env} CUSTOM_VAR", exec_info)

        self.assertEqual(local_exec.exit_code["localhost"], 0)
        self.assertIn("CUSTOM_VAR=HELLO", local_exec.stdout["localhost"])

    def test_multiple_custom_env_vars(self):
        """Test LocalExec forwards multiple custom environment variables"""
        exec_info = LocalExecInfo(
            env={"CUSTOM_VAR": "HELLO", "ANOTHER_VAR": "WORLD", "THIRD_VAR": "TEST"}
        )
        local_exec = LocalExec(
            f"python3 {self.print_env} CUSTOM_VAR ANOTHER_VAR THIRD_VAR", exec_info
        )

        self.assertEqual(local_exec.exit_code["localhost"], 0)
        stdout = local_exec.stdout["localhost"]
        self.assertIn("CUSTOM_VAR=HELLO", stdout)
        self.assertIn("ANOTHER_VAR=WORLD", stdout)
        self.assertIn("THIRD_VAR=TEST", stdout)

    def test_env_var_with_spaces(self):
        """Test LocalExec forwards environment variable with spaces"""
        exec_info = LocalExecInfo(env={"CUSTOM_VAR": "HELLO WORLD"})
        local_exec = LocalExec(f"python3 {self.print_env} CUSTOM_VAR", exec_info)

        self.assertEqual(local_exec.exit_code["localhost"], 0)
        self.assertIn("CUSTOM_VAR=HELLO WORLD", local_exec.stdout["localhost"])

    def test_env_var_with_special_chars(self):
        """Test LocalExec forwards environment variable with special characters"""
        exec_info = LocalExecInfo(env={"CUSTOM_VAR": "HELLO@#$%"})
        local_exec = LocalExec(f"python3 {self.print_env} CUSTOM_VAR", exec_info)

        self.assertEqual(local_exec.exit_code["localhost"], 0)
        self.assertIn("CUSTOM_VAR=HELLO@#$%", local_exec.stdout["localhost"])

    def test_numeric_env_var(self):
        """Test LocalExec forwards numeric environment variable"""
        exec_info = LocalExecInfo(env={"CUSTOM_VAR": 12345})
        local_exec = LocalExec(f"python3 {self.print_env} CUSTOM_VAR", exec_info)

        self.assertEqual(local_exec.exit_code["localhost"], 0)
        self.assertIn("CUSTOM_VAR=12345", local_exec.stdout["localhost"])

    def test_env_not_forwarded_without_setting(self):
        """Test that custom env var is not available without setting it"""
        exec_info = LocalExecInfo(env={})
        local_exec = LocalExec(f"python3 {self.print_env} CUSTOM_VAR", exec_info)

        # Should fail because CUSTOM_VAR is not set
        self.assertNotEqual(local_exec.exit_code["localhost"], 0)
        self.assertIn("CUSTOM_VAR not found", local_exec.stderr["localhost"])


class TestSshExecEnvForwarding(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        """Probe the exact non-interactive SSH capability once per class."""
        cls.ssh_available, cls.ssh_unavailable_reason = _probe_localhost_ssh()

    def setUp(self) -> None:
        """Set up test fixtures"""
        self.print_env = os.path.join(os.path.dirname(__file__), "print_env.py")
        # Use localhost for SSH tests to avoid needing remote hosts
        self.hostfile = Hostfile(hosts=["localhost"], find_ips=False)

    def assert_ssh_forwarding(
        self,
        command: str,
        environment: dict[str, object],
        expected_output: list[str],
        expected_command: list[str],
    ) -> Optional[SshExec]:
        """Exercise live SSH or its deterministic command contract."""
        exec_info = SshExecInfo(
            hostfile=self.hostfile,
            env=environment,
            timeout=_SSH_TIMEOUT_SECONDS,
            ssh_cmd=_SSH_LAUNCHER,
        )
        if not self.ssh_available:
            self.assertTrue(self.ssh_unavailable_reason)
            dry_run = SshExec(command, exec_info.mod(dry_run=True))
            rendered = dry_run.get_cmd()
            self.assertIn("BatchMode=yes", rendered)
            self.assertIn(
                f"ConnectTimeout={_SSH_TIMEOUT_SECONDS}",
                rendered,
            )
            for value in expected_command:
                self.assertIn(value, rendered)
            return None

        ssh_exec = SshExec(command, exec_info)
        self.assertEqual(
            ssh_exec.exit_code["localhost"],
            0,
            ssh_exec.stderr["localhost"],
        )
        for value in expected_output:
            self.assertIn(value, ssh_exec.stdout["localhost"])
        return ssh_exec

    def test_single_custom_env_var(self):
        """Test SshExec forwards single custom environment variable"""
        self.assert_ssh_forwarding(
            f"python3 {self.print_env} CUSTOM_VAR",
            {"CUSTOM_VAR": "HELLO"},
            ["CUSTOM_VAR=HELLO"],
            ['CUSTOM_VAR="HELLO"'],
        )

    def test_multiple_custom_env_vars(self):
        """Test SshExec forwards multiple custom environment variables"""
        self.assert_ssh_forwarding(
            f"python3 {self.print_env} CUSTOM_VAR ANOTHER_VAR THIRD_VAR",
            {"CUSTOM_VAR": "HELLO", "ANOTHER_VAR": "WORLD", "THIRD_VAR": "TEST"},
            [
                "CUSTOM_VAR=HELLO",
                "ANOTHER_VAR=WORLD",
                "THIRD_VAR=TEST",
            ],
            [
                'CUSTOM_VAR="HELLO"',
                'ANOTHER_VAR="WORLD"',
                'THIRD_VAR="TEST"',
            ],
        )

    def test_env_var_with_spaces(self):
        """Test SshExec forwards environment variable with spaces"""
        self.assert_ssh_forwarding(
            f"python3 {self.print_env} CUSTOM_VAR",
            {"CUSTOM_VAR": "HELLO WORLD"},
            ["CUSTOM_VAR=HELLO WORLD"],
            ['CUSTOM_VAR="HELLO WORLD"'],
        )

    def test_env_var_with_quotes(self):
        """Test SshExec forwards environment variable with quotes"""
        self.assert_ssh_forwarding(
            f"python3 {self.print_env} CUSTOM_VAR",
            {"CUSTOM_VAR": 'HELLO "QUOTED" WORLD'},
            ["CUSTOM_VAR=", "HELLO", "WORLD"],
            ['CUSTOM_VAR="HELLO \\"QUOTED\\" WORLD"'],
        )

    def test_numeric_env_var(self):
        """Test SshExec forwards numeric environment variable"""
        self.assert_ssh_forwarding(
            f"python3 {self.print_env} CUSTOM_VAR",
            {"CUSTOM_VAR": 12345},
            ["CUSTOM_VAR=12345"],
            ['CUSTOM_VAR="12345"'],
        )


class TestPsshExecEnvForwarding(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        """Probe the exact non-interactive SSH capability once per class."""
        cls.ssh_available, cls.ssh_unavailable_reason = _probe_localhost_ssh()

    def setUp(self) -> None:
        """Set up test fixtures"""
        self.print_env = os.path.join(os.path.dirname(__file__), "print_env.py")
        # Use localhost for PSSH tests
        self.hostfile = Hostfile(hosts=["localhost"], find_ips=False)

    def assert_pssh_forwarding(
        self,
        command: str,
        environment: dict[str, object],
        expected_output: list[str],
        expected_command: list[str],
    ) -> PsshExec:
        """Exercise live PSSH or its deterministic per-host command contract."""
        exec_info = PsshExecInfo(
            hostfile=self.hostfile,
            env=environment,
            timeout=_SSH_TIMEOUT_SECONDS,
            ssh_cmd=_SSH_LAUNCHER,
            dry_run=not self.ssh_available,
        )
        if not self.ssh_available:
            self.assertTrue(self.ssh_unavailable_reason)
        pssh_exec = PsshExec(command, exec_info)
        if not self.ssh_available:
            rendered = pssh_exec.ssh_executors["localhost"].get_cmd()
            self.assertIn("BatchMode=yes", rendered)
            self.assertIn(
                f"ConnectTimeout={_SSH_TIMEOUT_SECONDS}",
                rendered,
            )
            for value in expected_command:
                self.assertIn(value, rendered)
            return pssh_exec

        self.assertEqual(
            pssh_exec.exit_code["localhost"],
            0,
            pssh_exec.stderr["localhost"],
        )
        for value in expected_output:
            self.assertIn(value, pssh_exec.stdout["localhost"])
        return pssh_exec

    def test_single_custom_env_var(self):
        """Test PsshExec forwards single custom environment variable"""
        self.assert_pssh_forwarding(
            f"python3 {self.print_env} CUSTOM_VAR",
            {"CUSTOM_VAR": "HELLO"},
            ["CUSTOM_VAR=HELLO"],
            ['CUSTOM_VAR="HELLO"'],
        )

    def test_multiple_custom_env_vars(self):
        """Test PsshExec forwards multiple custom environment variables"""
        self.assert_pssh_forwarding(
            f"python3 {self.print_env} CUSTOM_VAR ANOTHER_VAR",
            {"CUSTOM_VAR": "HELLO", "ANOTHER_VAR": "WORLD"},
            ["CUSTOM_VAR=HELLO", "ANOTHER_VAR=WORLD"],
            ['CUSTOM_VAR="HELLO"', 'ANOTHER_VAR="WORLD"'],
        )


class TestMpiExecEnvForwarding(unittest.TestCase):
    def setUp(self):
        """Set up test fixtures"""
        self.print_env = os.path.join(os.path.dirname(__file__), "print_env.py")
        self.hostfile = Hostfile(hosts=["localhost"], find_ips=False)

        # Check if mpiexec is available
        try:
            subprocess.run(
                ["mpiexec", "--version"],
                capture_output=True,
                check=True,
                timeout=5,
            )
            self.mpi_available = True
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            FileNotFoundError,
        ):
            self.mpi_available = False

    def assert_dry_run_forwarding(self, command, exec_info, expected):
        """Assert the capability-independent MPICH command construction path."""
        dry_run = MpichExec(command, exec_info.mod(dry_run=True))
        for value in expected:
            self.assertIn(value, dry_run.cmd)

    def test_single_custom_env_var(self):
        """Test MpiExec forwards single custom environment variable"""
        exec_info = MpiExecInfo(
            nprocs=1, hostfile=self.hostfile, timeout=15, env={"CUSTOM_VAR": "HELLO"}
        )
        if not self.mpi_available:
            self.assert_dry_run_forwarding(
                f"python3 {self.print_env} CUSTOM_VAR",
                exec_info,
                ['-genv CUSTOM_VAR="HELLO"', "-n 1"],
            )
            return
        mpi_exec = MpiExec(f"python3 {self.print_env} CUSTOM_VAR", exec_info)
        delegate = mpi_exec.run()

        self.assertIs(mpi_exec._delegate, delegate)
        self.assertIn("localhost", mpi_exec.processes)
        self.assertIsNotNone(mpi_exec.processes["localhost"].poll())
        self.assertEqual(mpi_exec.exit_code["localhost"], 0)
        self.assertIn("CUSTOM_VAR=HELLO", mpi_exec.stdout["localhost"])

    def test_multiple_custom_env_vars(self):
        """Test MpiExec forwards multiple custom environment variables"""
        exec_info = MpiExecInfo(
            nprocs=1,
            hostfile=self.hostfile,
            timeout=15,
            env={"CUSTOM_VAR": "HELLO", "ANOTHER_VAR": "WORLD", "THIRD_VAR": "TEST"},
        )
        if not self.mpi_available:
            self.assert_dry_run_forwarding(
                f"python3 {self.print_env} CUSTOM_VAR ANOTHER_VAR THIRD_VAR",
                exec_info,
                [
                    '-genv CUSTOM_VAR="HELLO"',
                    '-genv ANOTHER_VAR="WORLD"',
                    '-genv THIRD_VAR="TEST"',
                ],
            )
            return
        mpi_exec = MpiExec(
            f"python3 {self.print_env} CUSTOM_VAR ANOTHER_VAR THIRD_VAR", exec_info
        )
        mpi_exec.run()

        self.assertEqual(mpi_exec.exit_code["localhost"], 0)
        stdout = mpi_exec.stdout["localhost"]
        self.assertIn("CUSTOM_VAR=HELLO", stdout)
        self.assertIn("ANOTHER_VAR=WORLD", stdout)
        self.assertIn("THIRD_VAR=TEST", stdout)

    def test_env_var_with_spaces(self):
        """Test MpiExec forwards environment variable with spaces"""
        exec_info = MpiExecInfo(
            nprocs=1,
            hostfile=self.hostfile,
            timeout=15,
            env={"CUSTOM_VAR": "HELLO WORLD"},
        )
        if not self.mpi_available:
            self.assert_dry_run_forwarding(
                f"python3 {self.print_env} CUSTOM_VAR",
                exec_info,
                ['-genv CUSTOM_VAR="HELLO WORLD"'],
            )
            return
        mpi_exec = MpiExec(f"python3 {self.print_env} CUSTOM_VAR", exec_info)
        mpi_exec.run()

        self.assertEqual(mpi_exec.exit_code["localhost"], 0)
        self.assertIn("CUSTOM_VAR=HELLO WORLD", mpi_exec.stdout["localhost"])

    def test_numeric_env_var(self):
        """Test MpiExec forwards numeric environment variable"""
        exec_info = MpiExecInfo(
            nprocs=1, hostfile=self.hostfile, timeout=15, env={"CUSTOM_VAR": 54321}
        )
        if not self.mpi_available:
            self.assert_dry_run_forwarding(
                f"python3 {self.print_env} CUSTOM_VAR",
                exec_info,
                ['-genv CUSTOM_VAR="54321"'],
            )
            return
        mpi_exec = MpiExec(f"python3 {self.print_env} CUSTOM_VAR", exec_info)
        mpi_exec.run()

        self.assertEqual(mpi_exec.exit_code["localhost"], 0)
        self.assertIn("CUSTOM_VAR=54321", mpi_exec.stdout["localhost"])

    def test_env_forwarding_with_multiple_procs(self):
        """Test MpiExec forwards env vars with multiple processes"""
        exec_info = MpiExecInfo(
            nprocs=2, hostfile=self.hostfile, timeout=15, env={"CUSTOM_VAR": "HELLO"}
        )
        if not self.mpi_available:
            self.assert_dry_run_forwarding(
                f"python3 {self.print_env} CUSTOM_VAR",
                exec_info,
                ['-genv CUSTOM_VAR="HELLO"', "-n 2"],
            )
            return
        mpi_exec = MpiExec(f"python3 {self.print_env} CUSTOM_VAR", exec_info)
        mpi_exec.run()

        self.assertEqual(mpi_exec.exit_code["localhost"], 0)
        # With 2 processes, we should see the output twice (or at least once)
        self.assertIn("CUSTOM_VAR=HELLO", mpi_exec.stdout["localhost"])


if __name__ == "__main__":
    unittest.main()
