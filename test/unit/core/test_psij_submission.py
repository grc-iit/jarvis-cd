"""The scheduler block maps onto one PSI/J job that covers every package slice."""
from pathlib import Path

import pytest

from jarvis_cd.core.scheduler import Scheduler, node_shape
from jarvis_cd.submit.entry import allocation_hosts


def _mapping(tmp_path: Path, **spec):
    return Scheduler({"name": "slurm", **spec}, tmp_path, pipeline_name="demo")._mapping


def test_scheduler_keys_land_on_psij_fields(tmp_path):
    mapping = _mapping(
        tmp_path, job_name="gs", nodes=2, ntasks_per_node=4, cpus_per_task=2,
        exclusive=True, mem="8G", time="01:30:00", partition="compute",
        account="proj", reservation="res1", qos="debug", gres="gpu:1",
        sbatch_args=["--hint=nomultithread"])
    resources = mapping["resources"]
    assert (resources["node_count"], resources["processes_per_node"],
            resources["cpu_cores_per_process"]) == (2, 4, 2)
    assert resources["exclusive_node_use"] is True
    assert resources["memory"] == 8 * 1024 ** 3
    assert mapping["attributes"] == {
        "queue_name": "compute", "account": "proj", "reservation_id": "res1",
        "duration_seconds": 5400}
    assert mapping["name"] == "gs"
    assert mapping["custom_attributes"]["slurm.qos"] == "debug"
    assert mapping["custom_attributes"]["slurm.gres"] == "gpu:1"
    assert mapping["custom_attributes"]["slurm.hint"] == "nomultithread"


@pytest.mark.parametrize("spec", [
    {"array": "0-3"},
    {"hetjob": "yes"},
    {"clusters": "other"},
    {"sbatch_args": ["--array=0-3"]},
    {"output": "/scratch/job-%j.out"},
    {"ntasks": 5, "nodes": 2, "ntasks_per_node": 2},
    {"constraint": True},
])
def test_unsupported_requests_are_refused(tmp_path, spec):
    with pytest.raises(ValueError):
        _mapping(tmp_path, **spec)


def _package(pkg_id, **config):
    return {"pkg_id": pkg_id, "config": config}


def test_node_count_covers_every_slice_and_largest_per_node_demand():
    packages = [
        _package("sim", host_slice="0:4", nprocs=16),
        _package("viz", host_slice="4:6", ppn=8),
        _package("own", hostfile="/etc/hosts", host_slice="0:64"),
    ]
    nodes, per_node, problems = node_shape(packages, declared_nodes=2)
    assert (nodes, per_node, problems) == (6, 8, [])
    assert node_shape(packages, declared_nodes=10)[0] == 10


def test_slice_that_selects_no_node_is_reported():
    nodes, _per_node, problems = node_shape([_package("bad", host_slice="3:1")], 2)
    assert nodes == 2
    assert problems and problems[0]["pkg_id"] == "bad"


def test_allocation_hosts_dedupes_in_allocation_order(tmp_path):
    nodefile = tmp_path / "nodes"
    nodefile.write_text("n3\nn3\nn1\nn1\nn2\n", encoding="utf-8")
    assert allocation_hosts(str(nodefile), "-ib") == ["n3-ib", "n1-ib", "n2-ib"]
    assert allocation_hosts("n2 n1 n2") == ["n2", "n1"]
    assert allocation_hosts(None) == ["localhost"]


def test_local_job_runs_on_the_hosts_sealed_at_submit(tmp_path):
    local = Scheduler({"name": "local"}, tmp_path, pipeline_name="demo", pipeline_yaml="p.yaml",
                      local_hosts=["vm-0", "vm-1", "vm-2"])
    local.write_script()
    nodefile = tmp_path / Scheduler.LOCAL_NODEFILE_NAME
    arguments = local.entry_arguments()
    assert arguments[arguments.index("--nodefile") + 1] == str(nodefile)
    assert allocation_hosts(str(nodefile)) == ["vm-0", "vm-1", "vm-2"]
    batch = Scheduler({"name": "slurm"}, tmp_path / "b", pipeline_name="demo", pipeline_yaml="p.yaml",
                      local_hosts=["vm-0"])
    assert "--nodefile" not in batch.entry_arguments()
