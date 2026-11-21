"""
Shell execution class for resource graph collection.
"""
import sys
from pathlib import Path
from .core_exec import CoreExec
from .exec_info import ExecInfo


class ResourceGraphExec(CoreExec):
    """
    Execute resource graph collection script on remote or local machines.
    """
    
    def __init__(self, exec_info: ExecInfo, benchmark: bool = True, duration: int = 25):
        """
        Initialize resource graph execution.
        
        :param exec_info: Execution information (LocalExecInfo, SshExecInfo, etc.)
        :param benchmark: Whether to run performance benchmarks
        :param duration: Benchmark duration in seconds
        """
        super().__init__()
        self.exec_info = exec_info
        self.benchmark = benchmark
        self.duration = duration
        # Build command
        self._build_command()
        
    def _build_command(self):
        """Build the command to execute."""
        from jarvis_cd.core.rg_collector import collector_command

        self.cmd = collector_command(benchmark=self.benchmark, duration=self.duration)
        
    def get_cmd(self) -> str:
        """Get the command string."""
        return self.cmd
        
    def run(self):
        """Execute the resource graph collection."""
        from .exec_factory import Exec
        
        # Use the Exec factory to create appropriate executor
        self._executor = Exec(self.cmd, self.exec_info)
        
        # Wait for completion if not async
        if not self.exec_info.exec_async:
            self._executor.wait_all()
        
        # Copy results from executor
        self.exit_code = self._executor.exit_code.copy()
        self.stdout = self._executor.stdout.copy()
        self.stderr = self._executor.stderr.copy()
        self.processes = self._executor.processes.copy()
        
        return self._executor