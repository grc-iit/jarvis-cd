"""Thread counts per rank at MPI launch (OMP/OPENBLAS/MKL_NUM_THREADS)."""
import os
from unittest.mock import patch

from jarvis_cd.shell.exec_info import MpiExecInfo
from jarvis_cd.shell.mpi_exec import OpenMpiExec
from jarvis_cd.util.hostfile import Hostfile
from jarvis_cd.util.placement import THREAD_VARIABLES, rank_threads

SLURM = {
    "SLURM_JOB_ID": "29114",
    "SLURM_JOB_NODELIST": "ares-comp-[21-22]",
    "SLURM_JOB_CPUS_PER_NODE": "40(x2)",
}


def test_cpus_per_node_divided_by_ranks_per_node():
    env = {}
    record = rank_threads(32, 16, ["ares-comp-21", "ares-comp-22"], env, SLURM)
    assert record["threads_per_rank"] == 2
    assert record["ranks_per_node"] == 16
    assert record["cpus"]["source"] == "SLURM_JOB_CPUS_PER_NODE"
    assert {k: env[k] for k in THREAD_VARIABLES} == dict.fromkeys(THREAD_VARIABLES, "2")
    assert record["set"] == dict.fromkeys(THREAD_VARIABLES, "2")
    assert record["kept"] == {}


def test_ranks_per_node_from_nprocs_when_no_ppn():
    env = {}
    record = rank_threads(8, None, ["ares-comp-21", "ares-comp-22"], env, SLURM)
    assert record["ranks_per_node"] == 4
    assert env["OMP_NUM_THREADS"] == "10"


def test_cpus_per_task_wins():
    env = {}
    record = rank_threads(4, 4, ["ares-comp-21"], env,
                          {**SLURM, "SLURM_CPUS_PER_TASK": "5"})
    assert record["cpus"]["source"] == "SLURM_CPUS_PER_TASK"
    assert env["OMP_NUM_THREADS"] == "5"


def test_oversubscribed_floor_is_one():
    env = {}
    rank_threads(64, 64, ["ares-comp-21"], env, SLURM)
    assert env["MKL_NUM_THREADS"] == "1"


def test_package_and_person_values_are_kept():
    env = {"OMP_NUM_THREADS": "4"}
    record = rank_threads(32, 16, ["ares-comp-21"], env,
                          {**SLURM, "OPENBLAS_NUM_THREADS": "1"})
    assert env["OMP_NUM_THREADS"] == "4"
    assert env["OPENBLAS_NUM_THREADS"] == "1"
    assert env["MKL_NUM_THREADS"] == "2"
    assert record["kept"] == {
        "OMP_NUM_THREADS": {"value": "4", "from": "rank_env"},
        "OPENBLAS_NUM_THREADS": {"value": "1", "from": "environment"},
    }
    assert record["set"] == {"MKL_NUM_THREADS": "2"}


def test_off_switch_and_outside_allocation_set_nothing():
    env = {"JARVIS_MPI_THREADS": "off"}
    record = rank_threads(32, 16, ["ares-comp-21"], env, SLURM)
    assert record["disabled"] is True
    assert not any(k in env for k in THREAD_VARIABLES)
    env = {}
    assert rank_threads(32, 16, ["localhost"], env, {}) is None
    assert env == {}


def test_launch_passes_threads_to_ranks_and_records_them():
    hostfile = Hostfile(hosts=["ares-comp-21"], find_ips=False)
    package_env = {"TEST_VAR": "x"}
    info = MpiExecInfo(nprocs=4, ppn=4, hostfile=hostfile, env=package_env,
                       dry_run=True)
    environ = {k: v for k, v in os.environ.items() if k not in THREAD_VARIABLES}
    environ.update(SLURM, SLURM_JOB_NODELIST="ares-comp-21",
                   SLURM_JOB_CPUS_PER_NODE="40", SLURM_NTASKS_PER_NODE="4")
    with patch.dict(os.environ, environ, clear=True), \
            patch.object(OpenMpiExec, "_collect_launch_net", return_value=None):
        launch = OpenMpiExec("./myapp", info)
    assert '-x OMP_NUM_THREADS="10"' in launch.get_cmd()
    assert launch.threads["set"]["OPENBLAS_NUM_THREADS"] == "10"
    assert launch.placement["threads"] is launch.threads
    assert launch.launch_record()["threads"]["threads_per_rank"] == 10
    assert "OMP_NUM_THREADS" not in package_env
