"""
Resource graph management for Jarvis.
Coordinates resource collection across nodes and provides analysis capabilities.
"""

import hashlib
import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Any, Optional, cast
from concurrent.futures import ThreadPoolExecutor, as_completed

from jarvis_cd.core.config import Jarvis
from jarvis_cd.util.resource_graph import ResourceGraph
from jarvis_cd.util.logger import logger
from jarvis_cd.shell import PsshExecInfo, LocalExec, LocalExecInfo


MAX_BUILTIN_RESOURCE_GRAPH_BYTES = 64 * 1024 * 1024


def _read_regular_file(path: Path) -> tuple[bytes, str]:
    """Read one bounded graph while binding the opened file to its path."""

    def identity(details: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
        # Windows can advance ctime merely by opening the file, so descriptor
        # binding there relies on the stable volume/file identity plus the
        # reparse-point rejection. POSIX ctime is included because an
        # unprivileged alternate hardlink writer cannot restore it.
        stable_ctime_ns = 0 if os.name == "nt" else details.st_ctime_ns
        return (
            details.st_mode,
            details.st_dev,
            details.st_ino,
            details.st_nlink,
            details.st_size,
            details.st_mtime_ns,
            stable_ctime_ns,
        )

    def is_reparse_point(details: os.stat_result) -> bool:
        attributes = cast(int, getattr(details, "st_file_attributes", 0))
        reparse_flag = cast(int, getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
        return bool(attributes & reparse_flag)

    linked_before = path.lstat()
    if (
        not stat.S_ISREG(linked_before.st_mode)
        or is_reparse_point(linked_before)
        or linked_before.st_nlink < 1
        or linked_before.st_size <= 0
        or linked_before.st_size > MAX_BUILTIN_RESOURCE_GRAPH_BYTES
    ):
        raise ValueError("builtin resource graph source must be one bounded regular file")
    flags = (
        os.O_RDONLY
        | cast(int, getattr(os, "O_BINARY", 0))
        | cast(int, getattr(os, "O_CLOEXEC", 0))
        | cast(int, getattr(os, "O_NOFOLLOW", 0))
    )
    descriptor = os.open(path, flags)
    try:
        opened_before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened_before.st_mode)
            or is_reparse_point(opened_before)
            or identity(opened_before) != identity(linked_before)
        ):
            raise ValueError("builtin resource graph source changed before activation")
        chunks: list[bytes] = []
        remaining = MAX_BUILTIN_RESOURCE_GRAPH_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        opened_after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    linked_after = path.lstat()
    if (
        len(payload) != opened_before.st_size
        or is_reparse_point(linked_after)
        or identity(opened_after) != identity(opened_before)
        or identity(linked_after) != identity(opened_after)
    ):
        raise ValueError("builtin resource graph source changed during activation")
    return payload, hashlib.sha256(payload).hexdigest()


class ResourceGraphManager:
    """
    Manages resource graph collection and analysis across the Jarvis cluster.
    """

    def __init__(self):
        """
        Initialize resource graph manager.
        Gets Jarvis singleton internally.
        """
        self.jarvis = Jarvis.get_instance()
        self.resource_graph = ResourceGraph()

        # Try to load existing resource graph if available
        default_path = self.jarvis.resource_graph_file
        if default_path.exists():
            try:
                self.resource_graph.load_from_file(default_path, quiet=True)
            except Exception:
                # Silently continue if load fails
                pass

    def build(
        self,
        benchmark: bool = True,
        duration: int = 25,
        net_only: bool = False,
        hosts: Optional[List[str]] = None,
    ):
        """
        Build resource graph by collecting information from all nodes in hostfile.

        Run it on the compute nodes (inside an allocation, or as the
        ``builtin.resource_graph`` package through ``jarvis ppl submit``):
        the ``net`` section describes the nodes that ran the collector.

        :param benchmark: Whether to run performance benchmarks
        :param duration: Benchmark duration in seconds
        :param net_only: Collect only the ``net`` section; keep the stored fs
        :param hosts: Nodes to collect from (default: the active hostfile)
        """
        from jarvis_cd.util.network import analyze_net

        nodes = list(hosts or [])
        if not nodes:
            jarvis = Jarvis.get_instance()
            if not jarvis.hostfile:
                raise ValueError(
                    "No hostfile set. Use 'jarvis hostfile set <path>' first."
                )
            nodes = jarvis.hostfile.hosts

        if not nodes:
            raise ValueError("Hostfile contains no hosts")

        logger.pipeline(f"Building resource graph for {len(nodes)} nodes...")

        previous = self.resource_graph
        previous_override = (previous.net or {}).get("override")

        # Clear existing resource graph (net-only keeps the stored storage view)
        self.resource_graph = ResourceGraph()
        if net_only:
            self.resource_graph.nodes = previous.nodes
            self.resource_graph.common_mounts = previous.common_mounts
        self._net_facts: Dict[str, Dict[str, Any]] = {}

        # Collect resources from all nodes in parallel
        self._collected_hosts: List[str] = []
        self._collect_from_nodes(nodes, benchmark, duration, net_only=net_only)
        failed_hosts = [host for host in nodes if host not in self._collected_hosts]
        if failed_hosts:
            # Never publish a partial graph: a net view built from some nodes
            # would steer MPI onto a network the others may not reach.
            self.resource_graph = previous
            raise self._collect_error(nodes, failed_hosts)
        if self._net_facts:
            self.resource_graph.net = analyze_net(
                self._net_facts, override=previous_override
            )
        else:
            self.resource_graph.net = previous.net

        # Save resource graph
        self._save()

        # Display summary
        if not net_only:
            self.resource_graph.print_summary()
            self.resource_graph.print_common_storage()
        if self.resource_graph.net:
            selected = self.resource_graph.net.get("selected") or {}
            for role in ("data", "control"):
                choice = selected.get(role) or {}
                logger.info(
                    f"net {role}: {choice.get('cidr')} "
                    f"({choice.get('interface')}, {choice.get('speed_mbps')} Mb/s)"
                )

    def _collect_from_nodes(
        self,
        nodes: List[str],
        benchmark: bool,
        duration: int,
        net_only: bool = False,
    ):
        """
        Collect resource information from multiple nodes in parallel.

        :param nodes: List of node hostnames/IPs
        :param benchmark: Whether to run benchmarks
        :param duration: Benchmark duration
        """

        from jarvis_cd.core.rg_collector import collector_command

        cmd = collector_command(net_only=net_only, benchmark=benchmark, duration=duration)
        self._collect_failures: Dict[str, Dict[str, Any]] = {}

        def tail(text: Any, limit: int = 2000) -> str:
            text = str(text or "")
            return text if len(text) <= limit else "…" + text[-limit:]

        def collect_from_node(hostname: str) -> Dict[str, Any]:
            """Collect from a single node."""
            via = "local"
            try:
                logger.package(f"Collecting resources from {hostname}...")

                # Get current hostname using LocalExec to compare
                exec_info_hostname = LocalExecInfo(
                    collect_output=True, hide_output=True
                )
                hostname_result = LocalExec("hostname", exec_info_hostname)
                current_hostname = hostname_result.stdout.get("localhost", "").strip()

                # Use local execution for localhost, otherwise use SSH
                if (
                    hostname in ["localhost", "127.0.0.1"]
                    or hostname == current_hostname
                ):
                    exec_info = LocalExecInfo(collect_output=True, hide_output=True)
                else:
                    via = "ssh"
                    # Create a temporary hostfile for this specific node
                    from jarvis_cd.util.hostfile import Hostfile

                    single_host_hostfile = Hostfile(
                        hosts=[hostname], find_ips=False
                    )

                    # Create execution info for this node
                    exec_info = PsshExecInfo(
                        hostfile=single_host_hostfile,
                        collect_output=True,
                        hide_output=True,
                    )

                logger.debug(f"Command to execute: {cmd}")

                # Execute resource collection based on execution type
                if exec_info.exec_type.name == "LOCAL":
                    executor = LocalExec(cmd, exec_info)
                else:
                    from jarvis_cd.shell import Exec

                    executor = Exec(cmd, exec_info)
                    executor.run()

                # Results are keyed by the host, or "localhost" for LocalExec.
                key = hostname if hostname in executor.exit_code else "localhost"
                exit_code = executor.exit_code.get(key, 1)
                stderr = executor.stderr.get(key, "")

                logger.debug(f"Exit codes: {executor.exit_code}")
                logger.debug(f"Stdout: {executor.stdout}")
                logger.debug(f"Stderr: {executor.stderr}")

                def failed(reason: str) -> None:
                    self._collect_failures[hostname] = {
                        "host": hostname,
                        "via": via,
                        "reason": reason,
                        "exit_code": exit_code,
                        "stderr": tail(stderr),
                        "command": cmd,
                    }
                    logger.error(f"Failed to collect from {hostname}: {reason}: {tail(stderr, 400)}")

                if exit_code != 0:
                    failed("collector exited non-zero")
                    return None

                # Parse JSON output
                json_output = executor.stdout.get(key, "")
                if not json_output.strip():
                    failed("collector printed nothing")
                    return None
                try:
                    resource_data = json.loads(json_output)
                except ValueError as error:
                    failed(f"collector output is not JSON: {error}")
                    return None
                logger.package(
                    f"Collected {len(resource_data.get('fs', []))} storage devices from {hostname}"
                )

                return resource_data

            except Exception as e:
                self._collect_failures[hostname] = {
                    "host": hostname,
                    "via": via,
                    "reason": f"error running the collector: {e}",
                    "exit_code": None,
                    "stderr": "",
                    "command": cmd,
                }
                logger.error(f"Error collecting from {hostname}: {e}")
                return None

        # Use ThreadPoolExecutor for parallel collection
        max_workers = min(len(nodes), 10)  # Limit concurrent connections

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Submit all collection tasks
            future_to_hostname = {
                executor.submit(collect_from_node, hostname): hostname
                for hostname in nodes
            }

            # Process results as they complete
            for future in as_completed(future_to_hostname):
                hostname = future_to_hostname[future]
                try:
                    resource_data = future.result()
                    if resource_data:
                        self._collected_hosts.append(hostname)
                        net_facts = resource_data.pop("net", None)
                        if isinstance(net_facts, dict):
                            self._net_facts[hostname] = net_facts
                        # Add to resource graph
                        if not net_only:
                            self.resource_graph.add_node_data(hostname, resource_data)
                        logger.success(f"Added resources from {hostname} to graph")
                    else:
                        logger.warning(f"No resource data collected from {hostname}")

                except Exception as e:
                    self._collect_failures.setdefault(
                        hostname,
                        {
                            "host": hostname,
                            "via": None,
                            "reason": f"error processing the collector output: {e}",
                            "exit_code": None,
                            "stderr": "",
                            "command": cmd,
                        },
                    )
                    logger.error(f"Exception processing {hostname}: {e}")

    def _collect_error(self, nodes: List[str], failed_hosts: List[str]):
        """The typed error for a collection that missed some or all nodes."""
        from jarvis_cd.core.issues import JarvisIssueError
        from jarvis_cd.core.rg_collector import collect_failure_issue

        failures = [
            self._collect_failures.get(
                host,
                {"host": host, "via": None, "reason": "no data returned"},
            )
            for host in failed_hosts
        ]
        collected = [host for host in nodes if host in self._collected_hosts]
        return JarvisIssueError([collect_failure_issue(nodes, collected, failures)])

    def _save(self):
        """Save resource graph through the configured JARVIS state boundary."""
        self.jarvis.save_resource_graph(self.resource_graph.to_dict())

    def _activate(self, loaded_graph: ResourceGraph) -> None:
        """Publish a graph while keeping every live view coherent on failure."""
        try:
            self.jarvis.save_resource_graph(loaded_graph.to_dict())
        except Exception:
            # The Jarvis cache changes immediately after the atomic replace. A
            # later directory-fsync error therefore means the file and cache are
            # new even though activation reports an error. Reload the active file
            # so this manager cannot remain a third, stale live view.
            active_graph = ResourceGraph()
            try:
                active_graph.load_from_file(self.jarvis.resource_graph_file)
            except Exception as recovery_error:
                # A failure before replace leaves this manager's previous view
                # correct. Preserve the publication error, adding recovery
                # evidence without masking the primary failure.
                publication_error = sys.exception()
                if publication_error is not None:
                    publication_error.add_note(
                        "Could not reload the active resource graph after failed "
                        f"activation: {recovery_error}"
                    )
            else:
                self.resource_graph = active_graph
            raise
        self.resource_graph = loaded_graph

    def load(self, file_path: Optional[Path] = None):
        """
        Load resource graph from file.

        :param file_path: Source graph, or the configured active graph when omitted
        """
        if file_path is None:
            file_path = self.jarvis.resource_graph_file

        if not file_path.exists():
            raise FileNotFoundError(f"Resource graph file not found: {file_path}")

        loaded_graph = ResourceGraph()
        loaded_graph.load_from_file(file_path)
        self._activate(loaded_graph)
        logger.success(
            f"Loaded resource graph from {file_path} and activated it at "
            f"{self.jarvis.resource_graph_file}"
        )

    def list_builtins(self) -> List[str]:
        """Return the exact packaged resource-graph profile catalog."""
        return list(self.jarvis.list_builtin_resource_graphs())

    def load_builtin(self, profile: str) -> tuple[Path, str]:
        """Activate one exact packaged resource-graph profile."""
        source = self.jarvis.get_builtin_resource_graph_path(profile)
        payload, source_sha256 = _read_regular_file(source)
        with tempfile.TemporaryDirectory(prefix="jarvis-builtin-graph-") as directory:
            snapshot = Path(directory) / source.name
            snapshot.write_bytes(payload)
            loaded_graph = ResourceGraph()
            loaded_graph.load_from_file(snapshot)
        self._activate(loaded_graph)
        logger.success(
            f"Loaded builtin resource graph from {source} and activated it at "
            f"{self.jarvis.resource_graph_file}"
        )
        return source, source_sha256

    def net_report(self) -> Dict[str, Any]:
        """Return the ``net`` query: selection, override, effective choice."""
        from jarvis_cd.util.network import NET_SCHEMA, resolve_launch_network

        net = self.resource_graph.net
        document: Dict[str, Any] = {
            "schema_version": "jarvis.resource-graph.net-report.v1",
            "graph": str(self.jarvis.resource_graph_file),
            "site_graph": bool(getattr(self.jarvis, "resource_graph_is_site", False)),
            "present": bool(net),
        }
        if not net:
            document["issues"] = [self._net_missing_issue()]
            return document
        effective = resolve_launch_network(net, net.get("collected_on") or [])
        document.update(
            {
                "net_schema": net.get("schema_version", NET_SCHEMA),
                "collected_at": net.get("collected_at"),
                "collected_on": net.get("collected_on"),
                "collected_in": net.get("collected_in"),
                "selected": net.get("selected"),
                "override": net.get("override"),
                "effective": {
                    "data": (effective or {}).get("data"),
                    "control": (effective or {}).get("control"),
                    "sources": (effective or {}).get("sources"),
                },
                "networks": net.get("networks"),
            }
        )
        if not net.get("collected_in"):
            document["warnings"] = [
                "net was not collected inside a scheduler allocation; rebuild it "
                "on the compute nodes (jarvis rg build +net_only in a job)"
            ]
        return document

    @staticmethod
    def _net_missing_issue() -> Dict[str, Any]:
        from jarvis_cd.core.issues import make_jarvis_issue

        return make_jarvis_issue(
            kind="resource_graph_net_missing",
            subject="resource graph net",
            message=(
                "The resource graph has no 'net' section. Collect it on the "
                "compute nodes: submit the builtin.resource_graph package with "
                "'jarvis ppl submit', or run 'jarvis rg build +net_only' inside "
                "an allocation."
            ),
            field="resource_graph.net",
            remedy="jarvis ppl submit of builtin.resource_graph (net_only=true)",
        )

    def net_set(
        self,
        *,
        data: Optional[str],
        control: Optional[str],
        reason: Optional[str],
        set_by: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Record a person's override of the data/control networks."""
        from jarvis_cd.core.issues import JarvisIssueError
        from jarvis_cd.util.network import make_override

        if data is None and control is None:
            raise ValueError("rg net set needs --data and/or --control")
        net = self.resource_graph.net
        if not net:
            raise JarvisIssueError([self._net_missing_issue()])
        previous = net.get("override") or {}
        override = make_override(
            net, data=data, control=control, reason=reason, set_by=set_by
        )
        for role in ("data", "control"):
            if role not in override and role in previous:
                override[role] = previous[role]
        if previous:
            override["previous"] = {
                key: previous.get(key)
                for key in ("data", "control", "set_at", "set_by", "reason")
            }
        net["override"] = override
        self._save()
        return self.net_report()

    def net_clear(self) -> Dict[str, Any]:
        """Drop the person's override; the graph selection applies again."""
        net = self.resource_graph.net
        if net and net.get("override"):
            net["override"] = None
            self._save()
        return self.net_report()

    def show(self):
        """Display the current resource graph YAML file."""
        default_path = self.jarvis.resource_graph_file

        if not default_path.exists():
            logger.warning("No resource graph found. Run 'jarvis rg build' first.")
            return

        # Read and print raw YAML file contents
        with open(default_path, "r") as f:
            print(f.read())

    def show_node_details(self, hostname: str):
        """
        Show detailed storage information for a specific node.

        :param hostname: Hostname to show details for
        """
        if not self.resource_graph.get_all_nodes():
            # Try to load the current resource graph
            try:
                self.load()
            except FileNotFoundError:
                logger.warning("No resource graph loaded. Run 'jarvis rg build' first.")
                return

        self.resource_graph.print_node_details(hostname)

    def list_nodes(self):
        """List all nodes in the resource graph."""
        if not self.resource_graph.get_all_nodes():
            # Try to load the current resource graph
            try:
                self.load()
            except FileNotFoundError:
                logger.warning(
                    "No nodes in resource graph. Run 'jarvis rg build' first."
                )
                return

        nodes = self.resource_graph.get_all_nodes()

        logger.info(f"Nodes in resource graph ({len(nodes)}):")
        for node in sorted(nodes):
            storage_count = len(self.resource_graph.get_node_storage(node))
            logger.info(f"  {node}: {storage_count} storage devices")

    def filter_by_type(self, dev_type: str):
        """
        Show storage devices filtered by type.

        :param dev_type: Device type to filter by (ssd, hdd, etc.)
        """
        if not self.resource_graph.get_all_nodes():
            logger.warning("No resource graph loaded. Run 'jarvis rg build' first.")
            return

        filtered = self.resource_graph.filter_by_type(dev_type)

        if not filtered:
            logger.warning(f"No {dev_type} devices found")
            return

        logger.info(f"=== {dev_type.upper()} Storage Devices ===")
        for hostname, devices in filtered.items():
            logger.info(f"\n{hostname}:")
            for device in devices:
                perf_info = ""
                if (
                    device.randwrite_4k_bw != "unknown"
                    and device.seqwrite_1m_bw != "unknown"
                ):
                    perf_info = (
                        f" [4K: {device.randwrite_4k_bw}, 1M: {device.seqwrite_1m_bw}]"
                    )
                logger.info(f"  {device.mount}: {device.avail}{perf_info}")

    def get_common_mounts(self) -> List[str]:
        """Get list of common mount points."""
        return list(self.resource_graph.get_common_storage().keys())

    def show_path(self):
        """Show the path to the current resource graph file."""
        default_path = self.jarvis.resource_graph_file

        if default_path.exists():
            # Print only the path for shell command substitution
            print(default_path)
        else:
            # Exit with error code for missing file
            print(f"Error: No resource graph found at {default_path}", file=sys.stderr)
            print("Run 'jarvis rg build' to create a resource graph", file=sys.stderr)
            sys.exit(1)
