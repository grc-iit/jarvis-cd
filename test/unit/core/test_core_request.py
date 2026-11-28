"""Ranks claim whole cores: the Slurm request, the plan's cores, the graph's cpu."""
from pathlib import Path
from unittest.mock import patch

from jarvis_cd.core.scheduler import Scheduler
from jarvis_cd.core.submit_plan import cores_plan
from jarvis_cd.site.queues import core_request
from jarvis_cd.util.placement import cpu_topology, node_threads_per_core
from jarvis_cd.util.resource_graph import ResourceGraph, merge_cpu_facts

ARES_COMPUTE = {
    "partition": "compute", "default": True, "sockets_per_node": 2,
    "cores_per_socket": 10, "threads_per_core": 2, "physical_cores_per_node": 20,
    "cpus_per_node": 40, "heterogeneous": False,
}


def _custom(tmp_path: Path, **spec):
    mapping = Scheduler({"name": "slurm", **spec}, tmp_path)._mapping
    return mapping["custom_attributes"]


def test_rank_request_claims_one_core_per_task(tmp_path):
    custom = _custom(tmp_path, nodes=2, ntasks_per_node=16)
    assert custom["slurm.ntasks-per-core"] == "1"
    assert _custom(tmp_path, ntasks=1)["slurm.ntasks-per-core"] == "1"


def test_no_rank_request_adds_nothing(tmp_path):
    assert "slurm.ntasks-per-core" not in _custom(tmp_path, nodes=2)


def test_person_mapping_wins(tmp_path):
    off = _custom(tmp_path, ntasks_per_node=24, ntasks_per_core=False)
    assert "slurm.ntasks-per-core" not in off
    two = _custom(tmp_path, ntasks_per_node=24, ntasks_per_core=2)
    assert two["slurm.ntasks-per-core"] == "2"
    hint = _custom(tmp_path, ntasks_per_node=4, sbatch_args=["--hint=multithread"])
    assert "slurm.ntasks-per-core" not in hint and hint["slurm.hint"] == "multithread"
    threads = _custom(tmp_path, ntasks_per_node=4, threads_per_core=1)
    assert "slurm.ntasks-per-core" not in threads
    assert core_request("slurm", {"ntasks_per_node": 4, "hint": "nomultithread"})[
        "source"] == "pipeline.scheduler"


def _plan(spec, nodes=2, facts=(ARES_COMPUTE,)):
    with patch("jarvis_cd.site.queues.partition_facts", return_value=list(facts)):
        return cores_plan("slurm", spec, nodes)


def test_plan_says_what_ranks_claim():
    plan = _plan({"nodes": 2, "ntasks_per_node": 16})
    assert plan["source"] == "sinfo" and plan["partition"] == "compute"
    assert plan["ntasks_per_core"] == 1 and plan["ntasks_per_core_source"] == "jarvis"
    assert plan["cores_per_rank"] == 1
    assert plan["cores_claimed_per_node"] == 16
    assert plan["fits"] is True
    assert "16 of the 20 physical cores" in plan["meaning"]
    assert "exclusive is not set" in plan["meaning"]


def test_plan_cpus_per_task_rounds_up_to_whole_cores():
    plan = _plan({"nodes": 1, "ntasks_per_node": 4, "cpus_per_task": 3})
    assert plan["cores_per_rank"] == 2 and plan["cores_claimed_per_node"] == 8


def test_plan_flags_more_ranks_than_cores():
    plan = _plan({"nodes": 1, "ntasks_per_node": 24})
    assert plan["fits"] is False
    assert "refuses" in plan["meaning"]


def test_plan_person_opt_out_and_unknown_topology():
    plan = _plan({"nodes": 1, "ntasks_per_node": 24, "ntasks_per_core": False})
    assert plan["fits"] is None and plan["ntasks_per_core_source"] == "pipeline.scheduler"
    with patch("jarvis_cd.core.resource_graph.ResourceGraphManager",
               side_effect=RuntimeError("no graph")):
        plan = _plan({"nodes": 1, "ntasks_per_node": 4}, facts=())
    assert plan["source"] is None and plan["fits"] is None
    assert "topology unknown" in plan["meaning"]


def _fake_sysfs(root: Path, siblings: int, cores: int, sockets: int) -> None:
    cpu = 0
    total = siblings * cores * sockets
    for _thread in range(siblings):
        for package in range(sockets):
            for core in range(cores):
                topo = root / f"cpu{cpu}" / "topology"
                topo.mkdir(parents=True)
                (topo / "core_id").write_text(f"{core}\n")
                (topo / "physical_package_id").write_text(f"{package}\n")
                cpu += 1
    (root / "online").write_text(f"0-{total - 1}\n")


def test_cpu_topology_counts_cores_once(tmp_path):
    _fake_sysfs(tmp_path, siblings=2, cores=10, sockets=2)
    topology = cpu_topology(str(tmp_path))
    assert topology == {"cpus": 40, "sockets": 2, "physical_cores": 20,
                        "cores_per_socket": 10, "threads_per_core": 2, "source": "sysfs"}
    assert node_threads_per_core(str(tmp_path)) == {"value": 2, "source": "sysfs"}
    assert node_threads_per_core(str(tmp_path / "missing"))["value"] == 1


def test_graph_cpu_merges_per_host_and_round_trips(tmp_path):
    fat = {"cpus": 40, "sockets": 2, "cores_per_socket": 10, "physical_cores": 20,
           "threads_per_core": 2}
    thin = {**fat, "cpus": 16, "cores_per_socket": 4, "physical_cores": 8}
    first = merge_cpu_facts(None, {"ares-comp-21": fat})
    assert first["smallest"]["physical_cores"] == 20 and first["heterogeneous"] is False
    merged = merge_cpu_facts(first, {"ares-comp-01": thin})
    assert sorted(merged["hosts"]) == ["ares-comp-01", "ares-comp-21"]
    assert merged["smallest"]["physical_cores"] == 8 and merged["heterogeneous"] is True
    assert merge_cpu_facts(merged, {}) is merged
    graph = ResourceGraph()
    graph.cpu = merged
    path = tmp_path / "graph.yaml"
    graph.save_to_file(path)
    loaded = ResourceGraph()
    loaded.load_from_file(path, quiet=True)
    assert loaded.cpu == merged
