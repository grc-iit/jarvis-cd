import unittest
import sys
import os
import subprocess
import tempfile
import time
from unittest.mock import patch

# Add the project root to the path so we can import jarvis_cd
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from jarvis_cd.shell.core_exec import LocalExec
from jarvis_cd.shell.exec_info import LocalExecInfo
from jarvis_cd.shell.windows_job import process_start_identity


class TestLocalExec(unittest.TestCase):
    def setUp(self):
        """Set up test fixtures"""
        self.print_env = os.path.join(os.path.dirname(__file__), "print_env.py")

    def env_command(self, *names: str) -> str:
        """Return a cross-platform command for the Python environment probe."""
        return subprocess.list2cmdline([sys.executable, self.print_env, *names])

    @staticmethod
    def python_command(code: str) -> str:
        """Return a cross-platform command for one inline Python program."""
        return subprocess.list2cmdline([sys.executable, "-c", code])

    def test_basic_execution(self):
        """Test basic command execution"""
        exec_info = LocalExecInfo()
        local_exec = LocalExec('echo "hello world"', exec_info)

        self.assertEqual(local_exec.exit_code["localhost"], 0)
        self.assertIn("hello world", local_exec.stdout["localhost"])

    def test_line_callback_observes_complete_stdout_and_stderr(self):
        """Execution callbacks receive live complete lines from both streams."""
        observed = []
        command = self.python_command(
            'import sys; print("out"); print("err", file=sys.stderr)'
        )

        local_exec = LocalExec(
            command,
            LocalExecInfo(
                hide_output=True,
                line_callback=lambda stream, line: observed.append((stream, line)),
            ),
        )

        self.assertEqual(local_exec.exit_code["localhost"], 0)
        self.assertIn(("stdout", "out\n"), observed)
        self.assertIn(("stderr", "err\n"), observed)

    def test_line_callback_failure_is_reported(self):
        """A progress callback failure terminates work and stays bounded."""

        callback_calls = 0

        def fail_callback(stream, line):
            nonlocal callback_calls
            callback_calls += 1
            raise RuntimeError(f"cannot persist {stream}: {line.strip()}")

        started = time.monotonic()
        local_exec = LocalExec(
            self.python_command(
                'import time; print("progress", flush=True); time.sleep(30)'
            ),
            LocalExecInfo(hide_output=True, line_callback=fail_callback),
        )

        self.assertNotEqual(local_exec.exit_code["localhost"], 0)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(callback_calls, 1)
        self.assertIn("Output line callback failed", local_exec.stderr["localhost"])
        self.assertIn("cannot persist stdout: progress", local_exec.stderr["localhost"])

    def test_stateful_line_callback_finalizes_once_at_eof(self):
        """A callback can flush an unterminated final fragment exactly once."""

        class Callback:
            def __init__(self):
                self.lines = []
                self.finalized = 0

            def __call__(self, stream, line):
                self.lines.append((stream, line))

            def finalize(self):
                self.finalized += 1

        callback = Callback()
        local_exec = LocalExec(
            self.python_command('import sys; sys.stdout.write("tail")'),
            LocalExecInfo(
                collect_output=False,
                hide_output=True,
                line_callback=callback,
            ),
        )
        local_exec.wait()

        self.assertEqual(local_exec.exit_code["localhost"], 0)
        self.assertEqual(callback.lines, [("stdout", "tail")])
        self.assertEqual(callback.finalized, 1)

    def test_stateful_callback_receives_owned_process_return_code(self):
        """Exit-aware callbacks receive the real code after output drains."""

        class Callback:
            def __init__(self):
                self.return_codes = []

            def __call__(self, stream, line):
                del stream, line

            def finalize_process(self, return_code):
                self.return_codes.append(return_code)

        success = Callback()
        failed = Callback()
        LocalExec(
            self.python_command("pass"),
            LocalExecInfo(hide_output=True, line_callback=success),
        )
        execution = LocalExec(
            self.python_command("raise SystemExit(7)"),
            LocalExecInfo(hide_output=True, line_callback=failed),
        )

        self.assertEqual(success.return_codes, [0])
        self.assertEqual(failed.return_codes, [7])
        self.assertEqual(execution.exit_code["localhost"], 7)

    def test_line_callback_finalization_failure_is_reported(self):
        """A final provider flush failure makes an otherwise clean process fail."""

        class Callback:
            def __call__(self, stream, line):
                del stream, line

            def finalize(self):
                raise RuntimeError("cannot flush final progress fragment")

        local_exec = LocalExec(
            self.python_command("pass"),
            LocalExecInfo(hide_output=True, line_callback=Callback()),
        )

        self.assertEqual(local_exec.exit_code["localhost"], 1)
        self.assertIn(
            "callback failed for finalization", local_exec.stderr["localhost"]
        )
        self.assertIn(
            "cannot flush final progress fragment", local_exec.stderr["localhost"]
        )

    def test_finalizer_failure_reconciles_with_effective_return_code(self):
        """A failed finalizer receives the effective failure in a correction pass."""

        class Callback:
            def __init__(self):
                self.reconciled = []

            def __call__(self, stream, line):
                del stream, line

            def finalize_process(self, return_code):
                self.reported_return_code = return_code
                raise RuntimeError("terminal metadata write failed")

            def reconcile_process_exit(self, return_code):
                self.reconciled.append(return_code)

        callback = Callback()
        local_exec = LocalExec(
            self.python_command("pass"),
            LocalExecInfo(hide_output=True, line_callback=callback),
        )

        self.assertEqual(callback.reported_return_code, 0)
        self.assertEqual(callback.reconciled, [1])
        self.assertEqual(local_exec.exit_code["localhost"], 1)

    def test_single_env_variable(self):
        """Test execution with a single environment variable"""
        exec_info = LocalExecInfo(env={"TEST_VAR": "test_value"})
        local_exec = LocalExec(self.env_command("TEST_VAR"), exec_info)

        self.assertEqual(local_exec.exit_code["localhost"], 0)
        self.assertIn("TEST_VAR=test_value", local_exec.stdout["localhost"])

    def test_multiple_env_variables(self):
        """Test execution with multiple environment variables"""
        exec_info = LocalExecInfo(
            env={"VAR1": "value1", "VAR2": "value2", "VAR3": "value3"}
        )
        local_exec = LocalExec(self.env_command("VAR1", "VAR2", "VAR3"), exec_info)

        self.assertEqual(local_exec.exit_code["localhost"], 0)
        stdout = local_exec.stdout["localhost"]
        self.assertIn("VAR1=value1", stdout)
        self.assertIn("VAR2=value2", stdout)
        self.assertIn("VAR3=value3", stdout)

    def test_env_variable_with_special_chars(self):
        """Test environment variables with special characters"""
        special_value = "value with spaces and 'quotes' and \"double quotes\""
        exec_info = LocalExecInfo(env={"SPECIAL_VAR": special_value})
        local_exec = LocalExec(self.env_command("SPECIAL_VAR"), exec_info)

        self.assertEqual(local_exec.exit_code["localhost"], 0)
        self.assertIn("SPECIAL_VAR=", local_exec.stdout["localhost"])

    def test_env_variable_override(self):
        """Test that custom env variables override system variables"""
        exec_info = LocalExecInfo(env={"PATH": "/custom/path"})
        local_exec = LocalExec(self.env_command("PATH"), exec_info)

        self.assertEqual(local_exec.exit_code["localhost"], 0)
        self.assertIn("PATH=/custom/path", local_exec.stdout["localhost"])

    def test_empty_env(self):
        """Test execution with empty env dict"""
        exec_info = LocalExecInfo(env={})
        local_exec = LocalExec('echo "test"', exec_info)

        self.assertEqual(local_exec.exit_code["localhost"], 0)

    def test_none_env(self):
        """Test execution with None env (should use system env)"""
        exec_info = LocalExecInfo(env=None)
        local_exec = LocalExec(self.env_command("PATH"), exec_info)

        # PATH should exist in system environment
        self.assertEqual(local_exec.exit_code["localhost"], 0)
        self.assertIn("PATH=", local_exec.stdout["localhost"])

    def test_collect_output(self):
        """Test output collection"""
        exec_info = LocalExecInfo(collect_output=True)
        local_exec = LocalExec('echo "collected output"', exec_info)

        self.assertIn("collected output", local_exec.stdout["localhost"])

    def test_hide_output(self):
        """Test hiding output (should still collect)"""
        exec_info = LocalExecInfo(hide_output=True, collect_output=True)
        local_exec = LocalExec('echo "hidden output"', exec_info)

        self.assertIn("hidden output", local_exec.stdout["localhost"])

    def test_stderr_collection(self):
        """Test stderr collection"""
        exec_info = LocalExecInfo(collect_output=True)
        local_exec = LocalExec('echo "error message" >&2', exec_info)

        self.assertIn("error message", local_exec.stderr["localhost"])

    def test_pipe_stdout_to_file(self):
        """Test piping stdout to a file"""
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
            temp_file = f.name

        try:
            exec_info = LocalExecInfo(pipe_stdout=temp_file)
            LocalExec(self.python_command('print("piped output")'), exec_info)

            with open(temp_file, "r") as f:
                content = f.read()
            self.assertIn("piped output", content)
        finally:
            os.unlink(temp_file)

    def test_pipe_stderr_to_file(self):
        """Test piping stderr to a file"""
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
            temp_file = f.name

        try:
            exec_info = LocalExecInfo(pipe_stderr=temp_file)
            LocalExec(
                self.python_command(
                    'import sys; print("error output", file=sys.stderr)'
                ),
                exec_info,
            )

            with open(temp_file, "r") as f:
                content = f.read()
            self.assertIn("error output", content)
        finally:
            os.unlink(temp_file)

    def test_cwd_change(self):
        """Test changing working directory"""
        with tempfile.TemporaryDirectory() as tmpdir:
            exec_info = LocalExecInfo(cwd=tmpdir)
            local_exec = LocalExec(
                self.python_command("import os; print(os.getcwd())"),
                exec_info,
            )

            self.assertIn(tmpdir, local_exec.stdout["localhost"])

    def test_async_execution(self):
        """Test asynchronous execution"""
        exec_info = LocalExecInfo(exec_async=True)
        local_exec = LocalExec(
            self.python_command('import time; time.sleep(0.1); print("async done")'),
            exec_info,
        )

        # Process should still be running or just finished
        # Wait for it to complete
        exit_code = local_exec.wait("localhost")
        self.assertEqual(exit_code, 0)
        self.assertIn("async done", local_exec.stdout["localhost"])

    def test_sleep_ms(self):
        """Test sleep after execution"""
        exec_info = LocalExecInfo(sleep_ms=100)
        start_time = time.time()
        LocalExec('echo "test"', exec_info)
        elapsed = time.time() - start_time

        # Should have slept for at least 0.1 seconds
        self.assertGreaterEqual(elapsed, 0.1)

    def test_timeout_terminates_shell_process_tree(self):
        """A bounded command returns 124 without leaving its child running."""
        command = subprocess.list2cmdline(
            [sys.executable, "-c", "import time; time.sleep(30)"]
        )
        start_time = time.monotonic()

        local_exec = LocalExec(command, LocalExecInfo(timeout=0.1))

        self.assertEqual(local_exec.exit_code["localhost"], 124)
        self.assertIn(
            "Command timed out after 0.1 seconds", local_exec.stderr["localhost"]
        )
        self.assertLess(time.monotonic() - start_time, 10)
        self.assertIsNotNone(local_exec.processes["localhost"].poll())

    def test_timeout_kills_descendant_that_ignores_graceful_signal(self):
        """Timeout cleanup removes a descendant even when it ignores SIGTERM."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pid_path = os.path.join(tmpdir, "descendant.pid")
            child_code = (
                "import signal,time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "time.sleep(60)"
            )
            parent_code = (
                "import subprocess,time; "
                f'child=subprocess.Popen([{sys.executable!r},"-c",{child_code!r}]); '
                f'open({pid_path!r},"w",encoding="utf-8").write(str(child.pid)); '
                "time.sleep(60)"
            )

            local_exec = LocalExec(
                self.python_command(parent_code),
                LocalExecInfo(timeout=0.5),
            )

            self.assertEqual(local_exec.exit_code["localhost"], 124)
            descendant_pid = int(open(pid_path, encoding="utf-8").read().strip())
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    os.kill(descendant_pid, 0)
                except OSError:
                    break
                time.sleep(0.05)
            else:
                self.fail(f"descendant process survived timeout: {descendant_pid}")

    def test_exited_parent_cannot_leave_pipe_holding_descendant(self):
        """Capture cleanup owns a child even after its immediate parent exits."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pid_path = os.path.join(tmpdir, "pipe-holder.pid")
            child_code = "import time; time.sleep(60)"
            parent_code = (
                "import os,subprocess,sys; "
                'kwargs=({"creationflags":subprocess.CREATE_NEW_PROCESS_GROUP} '
                'if os.name=="nt" else {}); '
                f'child=subprocess.Popen([sys.executable,"-c",{child_code!r}],**kwargs); '
                f'open({pid_path!r},"w",encoding="ascii").write(str(child.pid))'
            )
            started = time.monotonic()

            with patch(
                "jarvis_cd.shell.core_exec.subprocess.run",
                side_effect=AssertionError("taskkill-style PID cleanup is forbidden"),
            ):
                local_exec = LocalExec(
                    self.python_command(parent_code),
                    LocalExecInfo(),
                )

            self.assertEqual(local_exec.exit_code["localhost"], 0)
            self.assertLess(time.monotonic() - started, 12)
            descendant_pid = int(open(pid_path, encoding="ascii").read())
            if os.name == "nt":
                self.assertIsNone(process_start_identity(descendant_pid))
            else:
                with self.assertRaises(OSError):
                    os.kill(descendant_pid, 0)

    def test_stdin_input(self):
        """Test providing stdin to command"""
        exec_info = LocalExecInfo(stdin="test input\n")
        local_exec = LocalExec(
            self.python_command("import sys; print(sys.stdin.read())"),
            exec_info,
        )

        self.assertIn("test input", local_exec.stdout["localhost"])

    def test_exit_code_on_failure(self):
        """Test that exit code is captured on command failure"""
        exec_info = LocalExecInfo()
        local_exec = LocalExec(self.python_command("raise SystemExit(1)"), exec_info)

        self.assertNotEqual(local_exec.exit_code["localhost"], 0)

    def test_env_preserved_across_commands(self):
        """Test that environment variables persist across shell commands"""
        exec_info = LocalExecInfo(env={"CUSTOM_VAR": "custom_value"})
        # Use shell to execute multiple commands
        second_command = self.python_command('print("second command")')
        cmd = f"{self.env_command('CUSTOM_VAR')} && {second_command}"
        local_exec = LocalExec(cmd, exec_info)

        self.assertEqual(local_exec.exit_code["localhost"], 0)
        stdout = local_exec.stdout["localhost"]
        self.assertIn("CUSTOM_VAR=custom_value", stdout)
        self.assertIn("second command", stdout)

    def test_numeric_env_values(self):
        """Test environment variables with numeric values"""
        exec_info = LocalExecInfo(
            env={"INT_VAR": 42, "FLOAT_VAR": 3.14, "BOOL_VAR": True}
        )
        local_exec = LocalExec(
            self.env_command("INT_VAR", "FLOAT_VAR", "BOOL_VAR"),
            exec_info,
        )

        self.assertEqual(local_exec.exit_code["localhost"], 0)
        stdout = local_exec.stdout["localhost"]
        self.assertIn("INT_VAR=42", stdout)
        self.assertIn("FLOAT_VAR=3.14", stdout)
        self.assertIn("BOOL_VAR=True", stdout)

    def test_env_with_equals_in_value(self):
        """Test environment variable with equals sign in value"""
        exec_info = LocalExecInfo(env={"CONFIG": "key=value"})
        local_exec = LocalExec(self.env_command("CONFIG"), exec_info)

        self.assertEqual(local_exec.exit_code["localhost"], 0)
        self.assertIn("CONFIG=key=value", local_exec.stdout["localhost"])

    def test_get_cmd(self):
        """Test get_cmd returns the original command"""
        cmd = 'echo "test command"'
        exec_info = LocalExecInfo()
        local_exec = LocalExec(cmd, exec_info)

        self.assertEqual(local_exec.get_cmd(), cmd)


if __name__ == "__main__":
    unittest.main()
