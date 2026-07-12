"""
Process utility classes for Jarvis shell execution.
"""

import os
import subprocess
import sys
from typing import Union, List
from .exec_factory import Exec
from .exec_info import ExecInfo, ExecType, LocalExecInfo


def _is_local_windows(exec_info: ExecInfo) -> bool:
    """Return whether a utility should use the native Windows implementation."""
    return os.name == "nt" and exec_info.exec_type == ExecType.LOCAL


def _python_command(source: str, *args: str) -> str:
    """Build a Windows-safe command for one standard-library operation."""
    # Literal newlines inside a ``cmd.exe`` argument are treated as command
    # separators even when ``list2cmdline`` quotes the argument. Keep the
    # command line single-line and let Python decode the source representation.
    return subprocess.list2cmdline([sys.executable, "-c", f"exec({source!r})", *args])


class Kill(Exec):
    """
    Kill all processes whose process name exactly matches an executable.
    """

    def __init__(self, executable: str, exec_info: ExecInfo = None):
        """
        Kill all processes whose process name (comm) exactly equals
        ``executable``. Uses ``pkill -9 -x <executable>``, which matches
        the basename only — pass the executable name, not a command line
        or regex.

        :param executable: Executable name to kill (e.g. ``redis-server``)
        :param exec_info: Info needed to execute the command
        """
        if exec_info is None:
            exec_info = LocalExecInfo()

        kill_cmd = f"pkill -9 -x '{executable}'"
        super().__init__(kill_cmd, exec_info)


class KillAll(Exec):
    """
    Kill all processes owned by the current user.
    """

    def __init__(self, exec_info: ExecInfo = None):
        """
        Kill all processes owned by current user.

        :param exec_info: Info needed to execute the command
        """
        if exec_info is None:
            exec_info = LocalExecInfo()

        kill_cmd = "pkill -9 -u $(whoami)"
        super().__init__(kill_cmd, exec_info)


class Which(Exec):
    """
    Find the location of an executable.
    """

    def __init__(self, executable: str, exec_info: ExecInfo = None):
        """
        Find executable location.

        :param executable: Name of executable to find
        :param exec_info: Execution information
        """
        if exec_info is None:
            exec_info = LocalExecInfo()

        if _is_local_windows(exec_info):
            which_cmd = _python_command(
                "import shutil, sys\n"
                "path = shutil.which(sys.argv[1])\n"
                'print(path or "")\n'
                "raise SystemExit(0 if path else 1)",
                executable,
            )
        else:
            which_cmd = f"which {executable}"
        super().__init__(which_cmd, exec_info)
        self.executable = executable

    def get_path(self) -> str:
        """
        Get the path to the executable.

        :return: Path to executable or empty string if not found
        """
        return self.stdout.get("localhost", "").strip()

    def exists(self) -> bool:
        """
        Check if executable exists.

        :return: True if executable was found
        """
        return self.exit_code.get("localhost", 1) == 0 and bool(self.get_path())


class Mkdir(Exec):
    """
    Create directories.
    """

    def __init__(
        self,
        paths: Union[str, List[str]],
        exec_info: ExecInfo = None,
        parents: bool = True,
        exist_ok: bool = True,
    ):
        """
        Create directories.

        :param paths: Directory path(s) to create
        :param exec_info: Execution information
        :param parents: Create parent directories if needed
        :param exist_ok: Don't error if directory already exists
        """
        if exec_info is None:
            exec_info = LocalExecInfo()

        if isinstance(paths, str):
            paths = [paths]

        if _is_local_windows(exec_info):
            mkdir_cmd = _python_command(
                "from pathlib import Path\n"
                "import sys\n"
                "for value in sys.argv[1:]:\n"
                f"    Path(value).mkdir(parents={parents!r}, exist_ok={exist_ok!r})",
                *paths,
            )
        else:
            flags = []
            if parents:
                flags.append("-p")

            # Build command with properly escaped paths
            flag_str = " ".join(flags)
            path_str = " ".join([f'"{path}"' for path in paths])
            mkdir_cmd = f"mkdir {flag_str} {path_str}".strip()

        super().__init__(mkdir_cmd, exec_info)


class Rm(Exec):
    """
    Remove files and directories.
    """

    def __init__(
        self,
        paths: Union[str, List[str]],
        exec_info: ExecInfo = None,
        recursive: bool = False,
        force: bool = True,
    ):
        """
        Remove files and directories.

        :param paths: File/directory path(s) to remove
        :param exec_info: Execution information
        :param recursive: Remove directories recursively
        :param force: Force removal without prompting
        """
        if exec_info is None:
            exec_info = LocalExecInfo()

        if isinstance(paths, str):
            paths = [paths]

        if _is_local_windows(exec_info):
            rm_cmd = _python_command(
                "from pathlib import Path\n"
                "import shutil, sys\n"
                f"recursive = {recursive!r}\n"
                f"force = {force!r}\n"
                "for value in sys.argv[1:]:\n"
                "    path = Path(value)\n"
                "    if not path.exists() and not path.is_symlink():\n"
                "        if force:\n"
                "            continue\n"
                "        raise FileNotFoundError(value)\n"
                "    if path.is_dir() and not path.is_symlink():\n"
                "        shutil.rmtree(path) if recursive else path.rmdir()\n"
                "    else:\n"
                "        path.unlink()",
                *paths,
            )
        else:
            flags = []
            if recursive:
                flags.append("-r")
            if force:
                flags.append("-f")

            # Build command with properly escaped paths
            flag_str = " ".join(flags)
            path_str = " ".join([f'"{path}"' for path in paths])
            rm_cmd = f"rm {flag_str} {path_str}".strip()

        super().__init__(rm_cmd, exec_info)


class Chmod(Exec):
    """
    Change file permissions.
    """

    def __init__(
        self,
        paths: Union[str, List[str]],
        mode: str,
        exec_info: ExecInfo = None,
        recursive: bool = False,
    ):
        """
        Change file permissions.

        :param paths: File/directory path(s) to modify
        :param mode: Permission mode (e.g., '755', '+x', 'u+w')
        :param exec_info: Execution information
        :param recursive: Apply recursively
        """
        if exec_info is None:
            exec_info = LocalExecInfo()

        if isinstance(paths, str):
            paths = [paths]

        if _is_local_windows(exec_info):
            chmod_cmd = _python_command(
                "from pathlib import Path\n"
                "import os, stat, sys\n"
                "mode = sys.argv[1]\n"
                f"recursive = {recursive!r}\n"
                "def apply(path):\n"
                "    current = stat.S_IMODE(path.stat().st_mode)\n"
                "    target = (int(mode, 8) if mode.isdigit() else "
                "current | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)\n"
                "    os.chmod(path, target)\n"
                "for value in sys.argv[2:]:\n"
                "    root = Path(value)\n"
                "    apply(root)\n"
                "    if recursive and root.is_dir():\n"
                '        for child in root.rglob("*"):\n'
                "            apply(child)",
                mode,
                *paths,
            )
        else:
            flags = []
            if recursive:
                flags.append("-R")

            # Build command with properly escaped paths
            flag_str = " ".join(flags)
            path_str = " ".join([f'"{path}"' for path in paths])
            chmod_cmd = f"chmod {flag_str} {mode} {path_str}".strip()

        super().__init__(chmod_cmd, exec_info)


class Sleep(Exec):
    """
    Sleep for a specified duration.
    """

    def __init__(self, duration: Union[int, float], exec_info: ExecInfo = None):
        """
        Sleep for specified duration.

        :param duration: Sleep duration in seconds
        :param exec_info: Execution information
        """
        if exec_info is None:
            exec_info = LocalExecInfo()

        if _is_local_windows(exec_info):
            sleep_cmd = _python_command(
                "import sys, time; time.sleep(float(sys.argv[1]))",
                str(duration),
            )
        else:
            sleep_cmd = f"sleep {duration}"
        super().__init__(sleep_cmd, exec_info)


class Echo(Exec):
    """
    Echo text to stdout.
    """

    def __init__(self, text: str, exec_info: ExecInfo = None):
        """
        Echo text to stdout.

        :param text: Text to echo
        :param exec_info: Execution information
        """
        if exec_info is None:
            exec_info = LocalExecInfo()

        echo_cmd = f'echo "{text}"'
        super().__init__(echo_cmd, exec_info)


class GdbServer(Exec):
    """
    Launch a gdbserver for remote debugging.
    """

    def __init__(self, cmd: str, port: int, exec_info: ExecInfo = None):
        """
        Launch a gdbserver for remote debugging.

        :param cmd: Command to run under gdbserver
        :param port: Port number for gdbserver to listen on
        :param exec_info: Execution information
        """
        if exec_info is None:
            exec_info = LocalExecInfo()

        gdbserver_cmd = f"gdbserver :{port} {cmd}"
        super().__init__(gdbserver_cmd, exec_info)
        self.port = port
