"""Measured build times feed the next estimate (``install-costs.json``)."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from jarvis_cd.core import install_placement as placement
from jarvis_cd.core.installer import _record_install_costs
from jarvis_cd.core.spack_install import (
    _attribute_env_build_seconds,
    _node_install_seconds,
)

PARAVIEW = {"pkg_id": "pv", "pkg_type": "builtin.paraview",
            "config": {"install_method": "spack", "install": "paraview@5.13.1"}}


class _Runner:
    def __init__(self, stdout, rc=0):
        self.stdout, self.rc, self.calls = stdout, rc, []

    def run(self, arguments, stream=False):
        self.calls.append(list(arguments))
        return subprocess.CompletedProcess(arguments, self.rc, self.stdout, "")


def _runner(stdout, rc=0) -> Any:
    return cast(Any, _Runner(stdout, rc))


def _prefix(root: Path, name, full_hash, total, cache=False):
    prefix = root / f"{name}-{full_hash}"
    (prefix / ".spack").mkdir(parents=True)
    (prefix / ".spack" / "install_times.json").write_text(json.dumps(
        {"name": name, "hash": full_hash, "total": total, "cache": cache,
         "phases": []}), encoding="utf-8")
    return prefix


def test_node_install_seconds_reads_spack_install_times(tmp_path):
    llvm = _prefix(tmp_path, "llvm", "a" * 32, 3276.4)
    paraview = _prefix(tmp_path, "paraview", "b" * 32, 2698.04)
    cached = _prefix(tmp_path, "zlib", "c" * 32, 2.0, cache=True)
    old = _prefix(tmp_path, "mesa", "d" * 32, 9999.0)
    runner = _runner("".join([
        f"llvm {'a' * 32} {llvm}\n", f"paraview {'b' * 32} {paraview}\n",
        f"zlib {'c' * 32} {cached}\n", f"mesa {'d' * 32} {old}\n",
        "mpich " + "e" * 32 + " /nowhere\n"]))
    nodes = [{"name": "llvm", "hash": "aaaaaaa"}, {"name": "paraview", "hash": "bbbbbbb"},
             {"name": "zlib", "hash": "ccccccc"}, {"name": "mpich", "hash": "eeeeeee"},
             {"name": "mesa", "hash": "fffffff"}]  # mesa: another build's hash
    assert _node_install_seconds(runner, nodes) == {"llvm": 3276.4, "paraview": 2698.0}
    assert runner.calls == [["find", "--format", "{name} {hash} {prefix}"]]
    assert _node_install_seconds(_runner(""), []) == {}


def test_env_build_seconds_are_attributed_to_what_was_built():
    one = [{"status": "installed"}, {"status": "satisfied"}]
    _attribute_env_build_seconds(one, 8700.0)
    assert one[0]["build_seconds"] == 8700.0
    assert one[0]["build_seconds_source"] == "wall_clock"
    assert "build_seconds" not in one[1]
    two = [{"status": "installed", "node_build_seconds": {"a": 10.0, "b": 5.0}},
           {"status": "installed"}]
    _attribute_env_build_seconds(two, 100.0)
    assert two[0]["build_seconds"] == 15.0
    assert "build_seconds" not in two[1]


def test_a_build_records_package_and_node_seconds_for_the_next_estimate(tmp_path):
    decisions = tmp_path / "decisions.yaml"
    context = SimpleNamespace(decisions_path=decisions, log=lambda text: None)
    record = {"packages": {"pv": {
        "status": "installed", "build_seconds": 8700.0,
        "built": ["llvm", "paraview", "mesa", "zlib"],
        "node_build_seconds": {"llvm": 3276.4, "paraview": 2698.0, "mesa": 116.0,
                               "zlib": 30.0}}}}
    _record_install_costs([PARAVIEW], record, 40.0, context)
    costs = placement.read_costs(placement.costs_path(decisions))
    assert costs["paraview"]["build_seconds"] == 8700.0
    assert costs["paraview"]["built_nodes"] == 4
    assert costs["paraview"]["node_build_seconds"] == 2698.0
    assert costs["llvm"]["node_build_seconds"] == 3276.4

    # the same nodes missing again: their measured sum
    again = placement.estimate("build", "paraview", costs,
                               ["llvm", "paraview", "mesa", "zlib"])
    assert again["seconds"] == round(3276.4 + 2698.0 + 116.0 + 30.0, 1)
    assert "4 of 4 missing Spack node(s) built before" in again["basis"]
    # another package sharing llvm: llvm measured, the rest heuristic
    other = placement.estimate("build", "mytool", costs, ["llvm", "mytool"])
    assert other["seconds"] == round(3276.4 + placement.NODE_BUILD_SECONDS, 1)
    assert "1 of 2" in other["basis"] and "1 missing Spack node(s)" in other["basis"]


def test_heuristic_is_calibrated_per_heavy_node():
    # ParaView 5.13.1 on Ares: 59 ordinary nodes + llvm, mesa, paraview
    missing = [f"n{index}" for index in range(59)] + ["llvm", "mesa", "paraview"]
    estimate = placement.estimate("build", "paraview", {}, missing)
    assert estimate["seconds"] == 59 * 60 + 3600 + 600 + 3000
    assert "62 missing Spack node(s): 59 x 60 s" in estimate["basis"]
    assert "llvm 3600 s" in estimate["basis"] and "mesa 600 s" in estimate["basis"]
    assert placement.node_build_seconds("vtk") == placement.HEAVY_NODE_BUILD_SECONDS
    no_check = placement.estimate("build", "paraview", {}, None)
    assert no_check["seconds"] == placement.HEAVY_PACKAGE_BUILD_SECONDS
