"""
This module provides classes and methods to launch the Ior application.
Ior is a benchmark tool for measuring the performance of I/O systems.
It is a simple tool that can be used to measure the performance of a file system.
It is mainly targeted for HPC systems and parallel I/O.
"""

import os
import pathlib
import re
from jarvis_cd.core.pkg import Application
from jarvis_cd.shell import Exec, MpiExecInfo, PsshExecInfo, Rm, Mkdir
from jarvis_cd.shell.process import GdbServer


class Ior(Application):
    """
    Merged IOR class supporting both default (bare-metal) and container deployment.

    Set deploy_mode='container' to build and run IOR inside a Docker/Podman/Apptainer
    container.  Set deploy_mode='default' (the default) to use a system-installed ior
    binary via MPI.
    """

    def _configure_menu(self):
        """
        Create a CLI menu for the configurator method.

        :return: List(dict)
        """
        return [
            {
                "name": "write",
                "msg": "Perform a write workload",
                "type": bool,
                "default": True,
                "choices": [],
                "args": [],
            },
            {
                "name": "read",
                "msg": "Perform a read workload",
                "type": bool,
                "default": False,
            },
            {
                "name": "xfer",
                "msg": "The size of data transfer",
                "type": str,
                "default": "1m",
            },
            {
                "name": "block",
                "msg": "Amount of data to generate per-process",
                "type": str,
                "default": "32m",
                "aliases": ["block_size"],
            },
            {
                "name": "api",
                "msg": "The I/O api to use",
                "type": str,
                "choices": ["posix", "mpiio", "hdf5"],
                "default": "posix",
            },
            {
                "name": "fpp",
                "msg": "Use file-per-process",
                "type": bool,
                "default": False,
            },
            {
                "name": "reps",
                "msg": "Number of times to repeat",
                "type": int,
                "default": 1,
            },
            {
                "name": "nprocs",
                "msg": "Number of processes",
                "type": int,
                "default": 1,
            },
            {
                "name": "ppn",
                "msg": "The number of processes per node",
                "type": int,
                "default": 16,
            },
            {
                "name": "out",
                "msg": "Path to the output file",
                "type": str,
                "default": "/tmp/ior.bin",
                "aliases": ["output"],
            },
            {
                "name": "log",
                "msg": "Path to IOR output log",
                "type": str,
                "default": "",
            },
            {
                "name": "direct",
                "msg": "Use direct I/O (O_DIRECT) for POSIX API, bypassing I/O buffers",
                "type": bool,
                "default": False,
            },
        ]

    # ------------------------------------------------------------------
    # Container Dockerfile generators
    # ------------------------------------------------------------------

    def _build_phase(self):
        if self.config.get("deploy_mode") != "container":
            return None
        base = getattr(self.pipeline, "container_base", "ubuntu:24.04")
        content = self._read_build_script(
            "build.sh",
            {
                "BASE_IMAGE": base,
            },
        )
        return content, "mpi"

    def _build_deploy_phase(self):
        if self.config.get("deploy_mode") != "container":
            return None
        suffix = getattr(self, "_build_suffix", "")
        content = self._read_dockerfile(
            "Dockerfile.deploy",
            {
                "BUILD_IMAGE": self.build_image_name(),
                "DEPLOY_BASE": "ubuntu:24.04",
            },
        )
        return content, suffix

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def _configure(self, **kwargs):
        """
        Configure IOR.

        Calls super()._configure() which updates self.config and (when
        deploy_mode == 'container') triggers build_phase / build_deploy_phase.

        In default mode, also uppercases the API name and creates the output
        directory on all nodes.
        """
        super()._configure(**kwargs)

        # Default the log path to <shared_dir>/ior.log so _get_stat has
        # something to parse even when the YAML omits `log:`. Users who
        # set `log:` explicitly keep their override.
        if not self.config.get("log"):
            self.config["log"] = str(pathlib.Path(self.shared_dir) / "ior.log")

        if self.config.get("deploy_mode") == "default":
            self.config["api"] = self.config["api"].upper()

            # Create parent directory of output file on all nodes
            out = os.path.expandvars(self.config["out"])
            parent_dir = str(pathlib.Path(out).parent)
            hostfile = self.hostfile
            if hostfile is None or hostfile.is_local():
                pathlib.Path(parent_dir).mkdir(parents=True, exist_ok=True)
            else:
                Mkdir(
                    parent_dir,
                    PsshExecInfo(
                        env=self.mod_env,
                        hostfile=hostfile,
                        timeout=30,
                    ),
                ).run()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """Launch IOR via MpiExecInfo; Exec handles container wrapping transparently."""
        cfg = self.config

        cmd = [
            "ior",
            "-k",
            f"-b {cfg['block']}",
            f"-t {cfg['xfer']}",
            f"-a {cfg['api'].upper()}",
            f"-o {cfg['out']}",
        ]
        if cfg.get("write", True):
            cmd.append("-w")
        if cfg.get("read"):
            cmd.append("-r")
        if cfg.get("fpp"):
            cmd.append("-F")
        if cfg.get("reps", 1) > 1:
            cmd.append(f"-i {cfg['reps']}")
        if cfg.get("direct"):
            cmd.append("-O useO_DIRECT=1")

        ior_cmd = " ".join(cmd)
        if cfg.get("log"):
            ior_cmd += f" 2>&1 | tee {cfg['log']}"

        gdb_server = GdbServer(ior_cmd, cfg.get("dbg_port", 4000))
        cmd_list = [
            {
                "cmd": gdb_server.get_cmd(),
                "nprocs": 1 if cfg.get("do_dbg") else 0,
                "disable_preload": True,
            },
            {"cmd": ior_cmd, "nprocs": None},
        ]
        Exec(
            cmd_list,
            MpiExecInfo(
                nprocs=cfg["nprocs"],
                ppn=cfg["ppn"],
                hostfile=self.hostfile,
                port=self.ssh_port,
                container=self._container_engine,
                container_image=self.deploy_image_name(),
                shared_dir=self.shared_dir,
                private_dir=self.private_dir,
                env=self.mod_env,
            ),
        ).run()

    def stop(self):
        """Stop IOR (no-op — IOR runs to completion)."""
        pass

    def clean(self):
        """Remove IOR output files."""
        Rm(
            self.config["out"] + "*", PsshExecInfo(env=self.env, hostfile=self.hostfile)
        ).run()

    # ------------------------------------------------------------------
    # Output parsing
    # ------------------------------------------------------------------

    # Captures "Max Write: 269.97 MiB/sec (283.08 MB/sec)" — the trailing
    # MB/sec figure in parens is decimal-megabytes (1e6 bytes/sec); IOR
    # also prints binary MiB/sec (2^20 bytes/sec). We expose both.
    _MAX_RE = re.compile(
        r"^Max\s+(?P<op>Write|Read):\s+"
        r"(?P<mib>[0-9.]+)\s+MiB/sec\s+"
        r"\((?P<mb>[0-9.]+)\s+MB/sec\)",
        re.MULTILINE,
    )

    # Captures the per-operation summary row at the end of an IOR run.
    # The columns in IOR 3.3.0 are: Operation Max(MiB) Min(MiB) Mean(MiB)
    # StdDev Max(OPs) Min(OPs) Mean(OPs) StdDev Mean(s) ... — we keep
    # the MiB stats (first four numeric columns after the op name).
    _SUMMARY_RE = re.compile(
        r"^(?P<op>write|read)\s+"
        r"(?P<max>[0-9.]+)\s+"
        r"(?P<min>[0-9.]+)\s+"
        r"(?P<mean>[0-9.]+)\s+"
        r"(?P<stddev>[0-9.]+)\s+",
        re.MULTILINE,
    )

    def parse_log(self, text: str) -> dict:
        """Extract bandwidth stats from raw IOR log text.

        Returns a dict keyed by ``{pkg_id}.<op>_<stat>`` (e.g.
        ``ior_smoke.write_max_mibs``). Both the binary (MiB/sec) and
        decimal (MB/sec) maxes are recorded; the summary block fills
        in mean/min/stddev when present. The function never raises —
        unparseable text simply yields an empty dict.
        """
        stats: dict = {}
        prefix = self.pkg_id

        for m in self._MAX_RE.finditer(text):
            op = m.group("op").lower()
            stats[f"{prefix}.{op}_max_mibs"] = float(m.group("mib"))
            stats[f"{prefix}.{op}_max_mbs"] = float(m.group("mb"))

        for m in self._SUMMARY_RE.finditer(text):
            op = m.group("op")
            stats[f"{prefix}.{op}_max_mibs"] = float(m.group("max"))
            stats[f"{prefix}.{op}_min_mibs"] = float(m.group("min"))
            stats[f"{prefix}.{op}_mean_mibs"] = float(m.group("mean"))
            stats[f"{prefix}.{op}_stddev_mibs"] = float(m.group("stddev"))

        return stats

    def _get_stat(self, stat_dict):
        """Populate ``stat_dict`` with bandwidths parsed from the IOR log.

        Reads ``self.config['log']`` (defaulted to ``<shared_dir>/ior.log``
        by ``_configure``) and adds Max/Min/Mean/StdDev MiB/sec entries
        per operation. Missing or unparseable log → only runtime is set.
        """
        stat_dict[f"{self.pkg_id}.runtime"] = getattr(self, "start_time", None)

        log_path = self.config.get("log")
        if not log_path or not os.path.isfile(log_path):
            return

        try:
            with open(log_path, "r") as f:
                text = f.read()
        except OSError:
            return

        stat_dict.update(self.parse_log(text))

    def log(self, message):
        """Simple logging method."""
        print(f"[IOR:{self.pkg_id}] {message}")
