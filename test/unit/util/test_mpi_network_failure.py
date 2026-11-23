"""Tests for classifying a failed launch as an MPI network issue."""

from __future__ import annotations

import json
from pathlib import Path

from jarvis_cd.core.pipeline import Pipeline
from jarvis_cd.util.network import (
    classify_mpi_network_failure,
    launch_ranks_started,
)

SETTINGS_ECHO = (
    "pvserver MPI launch: 4 ranks on n1,n2; network "
    '{"OMPI_MCA_btl_tcp_if_include": "10.0.0.0/24", '
    '"OMPI_MCA_oob_tcp_if_include": "10.0.0.0/24"} --mca oob_tcp_if_include ib0'
)
DAEMON_LOST = (
    "ORTE has lost communication with a remote daemon.\n"
    "[n2:1234] mca_oob_tcp_peer_send_handler: Connection refused"
)
PEER_FAILED = (
    "[n1][[4242,1],0][btl_tcp_endpoint.c:626] received unexpected process identifier"
)


def _launch(root: Path, name: str, nprocs: int, started: int) -> None:
    launch = root / "provenance" / name
    (launch / "ranks").mkdir(parents=True)
    (launch / "launch.json").write_text(
        json.dumps({"nprocs": nprocs, "network": {"data": {"cidr": "10.0.0.0/24"}}}),
        encoding="utf-8",
    )
    for rank in range(started):
        (launch / "ranks" / f"n1-{rank}-{100 + rank}.txt").write_text(
            f"n1 {rank} {100 + rank}\n", encoding="utf-8"
        )


def test_setting_names_are_not_signatures() -> None:
    assert classify_mpi_network_failure(SETTINGS_ECHO) is None


def test_daemon_failure_is_an_issue_when_ranks_are_unknown_or_missing() -> None:
    for ranks_started in (None, False):
        issue = classify_mpi_network_failure(DAEMON_LOST, ranks_started=ranks_started)
        assert issue is not None
        assert issue["kind"] == "mpi_network"
        assert "has lost communication" in issue["details"]["signatures"]


def test_teardown_after_the_application_ran_is_not_an_issue() -> None:
    assert classify_mpi_network_failure(DAEMON_LOST, ranks_started=True) is None


def test_a_rank_failing_its_peers_counts_even_after_ranks_started() -> None:
    issue = classify_mpi_network_failure(PEER_FAILED, ranks_started=True)
    assert issue is not None
    assert issue["details"]["excerpt"] == PEER_FAILED


def test_launch_ranks_started(tmp_path: Path) -> None:
    assert launch_ranks_started(tmp_path / "provenance") is None
    _launch(tmp_path, "launch-001", 4, 4)
    assert launch_ranks_started(tmp_path / "provenance") is True
    _launch(tmp_path, "launch-002", 32, 2)
    assert launch_ranks_started(tmp_path / "provenance") is False


def test_application_failure_after_launch_gets_no_network_issue(tmp_path: Path) -> None:
    _launch(tmp_path, "launch-001", 4, 4)
    (tmp_path / "stdout.log").write_text(
        SETTINGS_ECHO + "\nVelocity diverged!\n" + DAEMON_LOST + "\n", encoding="utf-8"
    )
    error = RuntimeError("Pipeline startup failed at package 'solver': diverged")
    assert Pipeline._launch_failure_issues(tmp_path, error) is None


def test_launch_that_never_started_its_ranks_gets_the_network_issue(
    tmp_path: Path,
) -> None:
    _launch(tmp_path, "launch-001", 4, 0)
    (tmp_path / "stderr.log").write_text(DAEMON_LOST + "\n", encoding="utf-8")
    result = Pipeline._launch_failure_issues(tmp_path, RuntimeError("launch failed"))
    assert result is not None
    (issue,) = result["issues"]
    assert issue["kind"] == "mpi_network"
    assert issue["details"]["ranks_started"] is False
