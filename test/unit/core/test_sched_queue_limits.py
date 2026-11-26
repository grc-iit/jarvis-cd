"""``jarvis sched info`` queue limits for Slurm, PBS, LSF and Flux.

Fixture provenance (each constant says it again):

* captured: pasted output from a site, a vendor or a repository, quoted as
  published (web copies turn tabs into spaces);
* reconstructed: written for a case no published capture shows, from the
  vendor's format and, where stated, a site's published limits.
"""

from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from jarvis_cd.core import queue_limits
from jarvis_cd.core.queue_limits import LIMIT_KEYS

# --------------------------------------------------------------------- Slurm

# captured: GianniMoretti/slurm-gui src/slurm_gui/demo/data/partitions.txt;
# Luzivog/mit-battlecode-2026 tools/fixtures/slurm_read.txt;
# DonsonHH/Cowyolo26 research/colo_eval_20261030/evidence/server_preflight.txt
SLURM_CAPTURED = (
    "PartitionName=gpu AllowGroups=ALL AllowAccounts=ALL AllowQos=ALL AllocNodes=ALL Default=YES QoS=N/A DefaultTime=01:00:00 DisableRootJobs=NO ExclusiveUser=NO GraceTime=0 Hidden=NO MaxNodes=UNLIMITED MaxTime=2-00:00:00 MinNodes=0 LLN=NO MaxCPUsPerNode=UNLIMITED Nodes=gpu[01-04] PriorityJobFactor=1 PriorityTier=1 RootOnly=NO ReqResv=NO OverSubscribe=NO OverTimeLimit=NONE PreemptMode=OFF State=UP TotalCPUs=256 TotalNodes=4 SelectTypeParameters=NONE JobDefaults=(null) DefMemPerNode=UNLIMITED MaxMemPerNode=UNLIMITED TRES=cpu=256,mem=2000G,node=4,billing=256,gres/gpu=18,gres/gpu:a100=8,gres/gpu:l40s=8,gres/gpu:v100=2\n"
    "PartitionName=a40 AllowGroups=ALL AllowAccounts=ALL AllowQos=ALL AllocNodes=ALL Default=NO QoS=multigpu DefaultTime=NONE DisableRootJobs=NO ExclusiveUser=NO ExclusiveTopo=NO GraceTime=0 Hidden=NO MaxNodes=UNLIMITED MaxTime=3-00:00:00 MinNodes=0 LLN=YES MaxCPUsPerNode=UNLIMITED MaxCPUsPerSocket=UNLIMITED Nodes=gpuvm[33-35] PriorityJobFactor=1 PriorityTier=20 RootOnly=NO ReqResv=NO OverSubscribe=NO OverTimeLimit=NONE PreemptMode=OFF State=UP TotalCPUs=120 TotalNodes=3 SelectTypeParameters=NONE JobDefaults=(null) DefMemPerCPU=5000 MaxMemPerNode=UNLIMITED TRES=cpu=120,mem=375G,node=3,billing=14,gres/gpu=7 TRESBillingWeights=CPU=0.0625,GRES/gpu=1.0 ResumeTimeout=GLOBAL SuspendTimeout=GLOBAL SuspendTime=GLOBAL PowerDownOnIdle=NO\n"
    "PartitionName=8gpu AllowGroups=ALL AllowAccounts=ALL AllowQos=ALL AllocNodes=ALL Default=NO QoS=N/A DefaultTime=NONE DisableRootJobs=NO ExclusiveUser=NO GraceTime=0 Hidden=NO MaxNodes=UNLIMITED MaxTime=UNLIMITED MinNodes=0 LLN=NO MaxCPUsPerNode=UNLIMITED Nodes=gpu01 PriorityJobFactor=1 PriorityTier=1 RootOnly=NO ReqResv=NO OverSubscribe=NO OverTimeLimit=NONE PreemptMode=OFF State=UP TotalCPUs=32 TotalNodes=1 SelectTypeParameters=NONE JobDefaults=DefCpuPerGPU=4 DefMemPerNode=UNLIMITED MaxMemPerNode=UNLIMITED TRES=cpu=32,mem=385000M,node=1,billing=32,gres/gpu=8\n"
)


def test_slurm_captured_lines_fill_every_key_and_mark_the_absent_ones():
    known = queue_limits.parse_slurm_partitions(SLURM_CAPTURED)
    assert set(known) == {"gpu", "a40", "8gpu"}

    gpu = queue_limits.finish_limits("slurm", "scontrol", known["gpu"])
    assert set(gpu["limits"]) == set(LIMIT_KEYS)
    assert gpu["limits"]["max_time"] == "2-00:00:00"
    assert gpu["limits"]["max_time_s"] == 172800
    assert gpu["limits"]["default_time_s"] == 3600
    assert gpu["limits"]["min_nodes"] == 0
    assert gpu["limits"]["max_nodes"] == "UNLIMITED"
    assert gpu["limits"]["qos"] is None  # N/A: Slurm reports no partition QoS
    assert gpu["limits"]["allow_accounts"] == ["ALL"]
    assert gpu["limits"]["state"] == "UP"
    # this site sets DefMemPerNode, so scontrol prints no DefMemPerCPU
    assert gpu["limits_unknown"] == ["default_mem_per_cpu_mb"]
    assert gpu["limits_source"]["fields"]["max_nodes"] == "MaxNodes"

    a40 = queue_limits.finish_limits("slurm", "scontrol", known["a40"])
    assert a40["limits"]["qos"] == "multigpu"
    assert a40["limits"]["default_mem_per_cpu_mb"] == 5000
    assert a40["limits"]["default_time"] is None
    assert a40["limits"]["priority_tier"] == 20
    assert a40["limits_unknown"] == []

    unlimited = queue_limits.finish_limits("slurm", "scontrol", known["8gpu"])
    assert unlimited["limits"]["max_time"] == "UNLIMITED"
    assert unlimited["limits"]["max_time_s"] is None


def test_slurm_failed_scontrol_marks_every_limit_unknown_with_the_raw_error():
    entries: List[Dict[str, Any]] = [{"partition": "gpu"}]
    failed = SimpleNamespace(returncode=1, stdout="", stderr="slurm_load_partitions: Unable to contact slurm controller")
    with patch("jarvis_cd.core.scheduler._run_scheduler_query",
               return_value=(failed, "scontrol query exited 1: slurm_load_partitions: Unable to contact slurm controller")):
        error = queue_limits.attach_slurm_limits(entries)
    assert error is not None and error["exit_code"] == 1
    assert entries[0]["limits_unknown"] == list(LIMIT_KEYS)
    assert all(value is None for value in entries[0]["limits"].values())
    assert "Unable to contact" in entries[0]["limits_source"]["error"]["output"]


# ----------------------------------------------------------------------- PBS

# captured: HHU Hilbert, PBS Pro ``qstat -Qf`` (ptrebert/project-diploid-assembly
# environment/snakemake/cluster/hhu_pbs/hilbert_queues.md l.2-72). The copy
# turned qstat's continuation tab into 8 spaces; trailing spaces are as published.
PBS_HHU = """Queue: default
    queue_type = Route
    total_jobs = 0
    state_count = Transit:0 Queued:0 Held:0 Waiting:0 Running:0 Exiting:0 Begun
        :0 
    resources_max.walltime = 167:59:59
    route_destinations = CUDA,short,workq,long
    route_retry_time = 30
    enabled = True
    started = True


Queue: short
    queue_type = Execution
    Priority = 120
    total_jobs = -14
    state_count = Transit:0 Queued:0 Held:1 Waiting:0 Running:4 Exiting:0 Begun
        :0 
    max_queued = [u:PBS_GENERIC=1024]
    from_route_only = True
    resources_max.ngpus = 0
    resources_max.walltime = 02:00:00
    resources_default.preempt_targets = NONE
    resources_assigned.mem = 11534336kb
    resources_assigned.mpiprocs = 0
    resources_assigned.ncpus = 4
    resources_assigned.nodect = 4
    max_run = [u:PBS_GENERIC=512]
    enabled = True
    started = True


Queue: workq
    queue_type = Execution
    Priority = 100
    total_jobs = 182
    state_count = Transit:0 Queued:0 Held:11 Waiting:0 Running:105 Exiting:0 Be
        gun:1 
    max_queued = [u:PBS_GENERIC=1024]
    from_route_only = True
    resources_max.ngpus = 0
    resources_max.walltime = 72:00:00
    resources_min.walltime = 02:00:01
    resources_default.preempt_targets = NONE
    resources_assigned.mem = 4672716800kb
    resources_assigned.mpiprocs = 2396
    resources_assigned.ncpus = 2626
    resources_assigned.nodect = 141
    enabled = True
    started = True


Queue: long
    queue_type = Execution
    Priority = 80
    total_jobs = 82
    state_count = Transit:0 Queued:26 Held:2 Waiting:0 Running:95 Exiting:0 Beg
        un:4 
    max_queued = [u:PBS_GENERIC=1024]
    from_route_only = True
    resources_max.ngpus = 0
    resources_max.walltime = 167:00:00
    resources_min.walltime = 72:00:01
    resources_default.preempt_targets = NONE
    resources_assigned.mem = 1525022720kb
    resources_assigned.mpiprocs = 24
    resources_assigned.ncpus = 272
    resources_assigned.nodect = 100
    max_run_res.ncpus = [o:PBS_ALL=1024]
    enabled = True
    started = True
"""

# reconstructed: PBS Pro ``qstat -Qf`` for two ALCF Polaris queues, with the
# node and time limits the ALCF Polaris docs publish (debug 1-2 nodes, 1 h;
# prod a routing queue, 10-496 nodes, 24 h). ALCF prints no raw qstat, so the
# attribute names follow the PBS Pro reference; qstat's real tab continuation.
PBS_POLARIS_RECONSTRUCTED = (
    "Queue: debug\n"
    "    queue_type = Execution\n"
    "    Priority = 150\n"
    "    resources_max.nodect = 2\n"
    "    resources_max.walltime = 01:00:00\n"
    "    resources_min.nodect = 1\n"
    "    resources_min.walltime = 00:05:00\n"
    "    resources_default.walltime = 00:05:00\n"
    "    acl_group_enable = True\n"
    "    acl_groups = users,staff,ALCF_\n"
    "\tstaff\n"
    "    enabled = True\n"
    "    started = False\n"
    "\n"
    "Queue: prod\n"
    "    queue_type = Route\n"
    "    resources_max.nodect = 496\n"
    "    resources_max.walltime = 24:00:00\n"
    "    resources_min.nodect = 10\n"
    "    route_destinations = small,medium,large\n"
    "    enabled = True\n"
    "    started = True\n"
)

# reconstructed: ``qstat -Bf`` cut down from the captured Torque 2.4.2 sample
# (open-mpi users list 2010/10/14620) to its first lines and default_queue.
PBS_SERVER = "Server: polaris-pbs\n    server_state = Active\n    default_queue = prod\n"


def test_pbs_hhu_capture_reads_walltime_state_and_routes():
    queues = queue_limits.parse_pbs_status(PBS_HHU)
    assert list(queues) == ["default", "short", "workq", "long"]
    assert queues["workq"]["state_count"].endswith("Begun:1")  # the mid-token wrap

    entries = {entry["partition"]: entry for entry in queue_limits.pbs_entries(PBS_HHU, None)}
    workq = entries["workq"]
    assert set(workq["limits"]) == set(LIMIT_KEYS)
    assert workq["limits"]["max_time"] == "72:00:00"
    assert workq["limits"]["max_time_s"] == 259200
    assert workq["limits"]["state"] == "UP"
    assert workq["available"] == "up"
    assert workq["limits"]["priority_tier"] == 100
    # HHU sets no node limits: they are unknown, not unlimited
    assert {"min_nodes", "max_nodes", "default_time", "qos"} <= set(workq["limits_unknown"])
    assert workq["limits"]["max_nodes"] is None
    assert workq["limits_source"]["fields"]["max_time"] == "resources_max.walltime"
    assert workq["default"] is None  # no qstat -Bf: the default queue is unknown

    route = entries["default"]
    assert route["queue_type"] == "Route"
    assert route["route_destinations"] == ["CUDA", "short", "workq", "long"]
    assert route["limits"]["max_time_s"] == 167 * 3600 + 59 * 60 + 59


def test_pbs_polaris_reconstruction_gives_node_limits_and_default_queue():
    entries = {entry["partition"]: entry
               for entry in queue_limits.pbs_entries(PBS_POLARIS_RECONSTRUCTED, PBS_SERVER)}
    debug, prod = entries["debug"], entries["prod"]
    assert (debug["limits"]["min_nodes"], debug["limits"]["max_nodes"]) == (1, 2)
    assert (prod["limits"]["min_nodes"], prod["limits"]["max_nodes"]) == (10, 496)
    assert debug["limits"]["default_time_s"] == 300
    assert debug["limits"]["state"] == "DOWN"  # enabled, not started
    assert debug["limits"]["allow_groups"] == ["users", "staff", "ALCF_staff"]
    assert (prod["default"], debug["default"]) == (True, False)
    assert prod["limits_source"]["fields"]["min_nodes"] == "resources_min.nodect"


def test_pbs_queue_inherits_server_limits_and_torque_nodespec():
    server = "Server: s\n    default_queue = batch\n    resources_max.walltime = 48:00:00\n"
    # reconstructed: Torque writes a nodespec, not nodect
    queue = "Queue: batch\n    queue_type = Execution\n    resources_max.nodes = 4:ppn=8\n"
    entry = queue_limits.pbs_entries(queue, server)[0]
    assert entry["limits"]["max_time_s"] == 172800
    assert entry["limits_source"]["fields"]["max_time"] == "server resources_max.walltime"
    assert entry["limits"]["max_nodes"] == 4
    assert "state" in entry["limits_unknown"]


# ----------------------------------------------------------------------- LSF

# captured: Gabor Samu, "LSF output" (gaborsamu.com/blog/lsf_output/), LSF Suite for HPC
LSF_SAMU = """
QUEUE: normal
  -- For normal low priority jobs, running only if hosts are lightly loaded.  This is the default queue.

PARAMETERS/STATISTICS
PRIO NICE STATUS          MAX JL/U JL/P JL/H NJOBS  PEND   RUN SSUSP USUSP  RSV PJOBS 
 30    0  Open:Active       -    -    -    -     0     0     0     0     0    0     0
Interval for a host to accept two jobs is 0 seconds

SCHEDULING PARAMETERS
           r15s   r1m  r15m   ut      pg    io   ls    it    tmp    swp    mem
 loadSched   -     -     -     -       -     -    -     -     -      -      -  
 loadStop    -     -     -     -       -     -    -     -     -      -      -  

SCHEDULING POLICIES:  FAIRSHARE  NO_INTERACTIVE
USER_SHARES:  [default, 1] 

SHARE_INFO_FOR: normal/
 USER/GROUP   SHARES  PRIORITY  STARTED  RESERVED  CPU_TIME  RUN_TIME   ADJUST  GPU_RUN_TIME
gsamu           1       0.333      0        0         0.0        0       0.000             0
elasticsearch     1       0.333      0        0         0.0        0       0.000             0

USERS: all  
HOSTS:  all 
"""

# captured: Sanger pathogen-informatics-training Notebooks/LSF/LSF.tex l.798-835
LSF_SANGER = """QUEUE: yesterday
  -- As in I needed it yesterday highest priority (all nodes)

PARAMETERS/STATISTICS
PRIO NICE STATUS          MAX JL/U JL/P JL/H NJOBS  PEND   RUN SSUSP USUSP  RSV
500   20  Open:Active      20    8    -    -     0     0     0     0     0    0
Interval for a host to accept two jobs is 0 seconds

DEFAULT LIMITS:
 MEMLIMIT
    100 M

MAXIMUM LIMITS:
 RUNLIMIT
     2880.0 min of BL465c_G8

 CORELIMIT MEMLIMIT
      0 M     250 G

SCHEDULING PARAMETERS
           r15s   r1m  r15m   ut      pg    io   ls    it    tmp    swp    mem
 loadSched   -     -     -     -       -     -    -     -     -      -      -
 loadStop    -     -     -     -       -     -    -     -     -      -      -

              poe nrt_windows adapter_windows ntbl_windows  uptime
 loadSched     -           -               -            -       -
 loadStop      -           -               -            -       -

SCHEDULING POLICIES:  FAIRSHARE
USER_SHARES:  [default, 1]

SHARE_INFO_FOR: yesterday/
USER/GROUP   SHARES  PRIORITY  STARTED  RESERVED  CPU_TIME  RUN_TIME   ADJUST
user1            1       0.302      0        0        47.0     1590       0.000
user2            1       0.301      0        0       590.3     1634       0.000

USERS: all
HOSTS:  pcs5a pcs5b+1 others+2
"""

# reconstructed: a closed queue with a default run limit, a host limit per job
# and group-only users, in the bqueues -l layout the captures show
LSF_RECONSTRUCTED = """QUEUE: big
  -- Large jobs.

PARAMETERS/STATISTICS
PRIO NICE STATUS          MAX JL/U JL/P JL/H NJOBS  PEND   RUN SSUSP USUSP  RSV
 40    0  Closed:Inact      -    -    -    -     0     0     0     0     0    0

DEFAULT LIMITS:
 RUNLIMIT
     60.0 min

MAXIMUM LIMITS:
 RUNLIMIT
     1440.0 min

 HOSTLIMIT_PER_JOB
 64

USERS: hpcgroup/ physics/
HOSTS:  all
"""

# captured: IBM Spectrum LSF 10.1 docs, "Viewing cluster information" (bparams)
LSF_BPARAMS = "Default Queues:  normal idle\nMBD_SLEEP_TIME used for calculations:  20 seconds\n"


def test_lsf_captures_read_status_priority_and_run_limit():
    entries = {entry["partition"]: entry
               for entry in queue_limits.lsf_entries(LSF_SAMU + LSF_SANGER, LSF_BPARAMS)}
    normal, yesterday = entries["normal"], entries["yesterday"]
    assert normal["default"] is True and yesterday["default"] is False
    assert normal["limits"]["state"] == "UP"
    assert normal["limits"]["priority_tier"] == 30
    assert normal["limits"]["allow_groups"] == ["ALL"]
    assert "max_time" in normal["limits_unknown"]  # normal prints no RUNLIMIT
    assert yesterday["limits"]["max_time"] == "2880.0 min of BL465c_G8"
    assert yesterday["limits"]["max_time_s"] == 2880 * 60
    assert yesterday["limits_source"]["fields"]["max_time"] == "MAXIMUM LIMITS RUNLIMIT"
    assert "min_nodes" in yesterday["limits_unknown"]
    assert set(yesterday["limits"]) == set(LIMIT_KEYS)


def test_lsf_block_values_align_under_their_headers():
    queue = queue_limits.parse_bqueues_long(LSF_SANGER)["yesterday"]
    assert queue["maximum"] == {"RUNLIMIT": "2880.0 min of BL465c_G8",
                                "CORELIMIT": "0 M", "MEMLIMIT": "250 G"}
    assert queue["default"] == {"MEMLIMIT": "100 M"}


def test_lsf_reconstruction_reads_host_limit_default_time_and_groups():
    big = queue_limits.lsf_entries(LSF_RECONSTRUCTED, None)[0]
    assert big["limits"]["max_nodes"] == 64
    assert big["limits"]["default_time_s"] == 3600
    assert big["limits"]["state"] == "INACTIVE"
    assert big["limits"]["allow_groups"] == ["hpcgroup", "physics"]
    assert big["default"] is None  # bparams not read


# ---------------------------------------------------------------------- Flux

# captured: LLNL Corona (llnl/HPC-Tutorials flux/section3.md)
FLUX_CORONA = """QUEUE    EN ST TDEFAULT   TLIMIT     NNODES     NCORES      NGPUS
pbatch*   ✔  ✔      30m       1d      0-inf      0-inf      0-inf
pdebug    ✔  ✔      30m       1h      0-inf      0-inf      0-inf
pnvmeof   ✔  ✔      30m       1d      0-inf      0-inf      0-inf
"""

# captured: llnl/maestrowf docs/Maestro/how_to_guides/running_with_flux.md
FLUX_MAESTRO = """QUEUE    EN ST TDEFAULT   TLIMIT     NNODES     NCORES      NGPUS
pdebug    ✔  ✔       1h       4h        0-2      0-512       0-32
pbatch    ✔  ✗      24h       1d       0-20      0-8192      0-512
"""

# captured: flux-framework/Tutorials 2025/HPCIC-AWS (anonymous queue) with the
# colour codes of the 2025/ISC-AWS notebook row
FLUX_ANONYMOUS = (" EN ST TDEFAULT   TLIMIT     NNODES     NCORES      NGPUS\n"
                  " \u001b[01;32m ✔\u001b[0;0m \u001b[01;32m ✔\u001b[0;0m      inf      inf        0-4       0-40        0-0\n")

# reconstructed: what JARVIS's own ``-o`` format prints for a long queue name
FLUX_OWN_FORMAT = ("QUEUE EN ST TDEFAULT TLIMIT NNODES\n"
                   "debug-scaling* y y 5m 1h 1-10\n"
                   "capacity n y 5m 7d 1-inf\n")


def test_flux_captures_read_ranges_times_and_default():
    corona = {entry["partition"]: entry for entry in queue_limits.flux_entries(FLUX_CORONA, "flux queue list")}
    assert corona["pbatch"]["default"] is True and corona["pdebug"]["default"] is False
    assert corona["pdebug"]["limits"]["max_time_s"] == 3600
    assert corona["pbatch"]["limits"]["default_time_s"] == 1800
    assert corona["pbatch"]["limits"]["max_nodes"] == "UNLIMITED"
    assert corona["pbatch"]["limits"]["min_nodes"] == 0
    assert corona["pbatch"]["limits"]["state"] == "UP"

    maestro = {entry["partition"]: entry for entry in queue_limits.flux_entries(FLUX_MAESTRO, "flux queue list")}
    assert maestro["pbatch"]["limits"]["max_nodes"] == 20
    assert maestro["pbatch"]["limits"]["state"] == "DOWN"  # enabled, not started
    assert set(maestro["pbatch"]["limits_unknown"]) == set(LIMIT_KEYS) - {
        "max_time", "max_time_s", "default_time", "default_time_s",
        "min_nodes", "max_nodes", "state"}

    anonymous = queue_limits.flux_entries(FLUX_ANONYMOUS, "flux queue list")
    assert len(anonymous) == 1
    assert anonymous[0]["partition"] is None and anonymous[0]["default"] is True
    assert anonymous[0]["limits"]["max_time"] == "UNLIMITED"
    assert anonymous[0]["limits"]["max_nodes"] == 4


def test_flux_reader_falls_back_to_the_default_table():
    calls = []

    def query(argv):
        calls.append(argv)
        if argv[-1] == "list":
            return SimpleNamespace(returncode=0, stdout=FLUX_MAESTRO, stderr=""), None
        return (SimpleNamespace(returncode=1, stdout="", stderr="flux-queue: unknown format"),
                "flux query exited 1: flux-queue: unknown format")

    with patch("jarvis_cd.core.scheduler._run_scheduler_query", side_effect=query):
        entries, error = queue_limits.flux_partitions()
    assert error is None and len(calls) == 3
    assert entries[1]["limits_source"]["fields"]["max_nodes"] == "limits.range.effective.nnodes"

    own = queue_limits.flux_entries(FLUX_OWN_FORMAT, "flux queue list -o ...")
    assert own[0]["partition"] == "debug-scaling" and own[0]["default"] is True
    assert (own[0]["limits"]["min_nodes"], own[0]["limits"]["max_nodes"]) == (1, 10)
    assert own[1]["limits"]["state"] == "DRAIN"


# ---------------------------------------------------------------- sched info


def _sched_info(provider, outputs):
    from jarvis_cd.core.sched_info import sched_info

    def query(argv):
        result = outputs.get(argv[0] + " " + argv[1])
        if result is None:
            return None, f"{argv[0]} query failed: not found"
        return SimpleNamespace(returncode=0, stdout=result, stderr=""), None

    jarvis = SimpleNamespace(resource_graph_file=None, resource_graph_is_site=False)
    with patch("jarvis_cd.core.scheduler._run_scheduler_query", side_effect=query):
        return sched_info(jarvis, provider, include_mpi=False)


def test_sched_info_reports_pbs_queues_and_says_it_cannot_submit():
    document = _sched_info("pbs", {"qstat -Qf": PBS_POLARIS_RECONSTRUCTED, "qstat -Bf": PBS_SERVER})
    assert [entry["partition"] for entry in document["partitions"]] == ["debug", "prod"]
    assert "pbs" in document["providers_queues_readable"]
    kinds = [issue["kind"] for issue in document["issues"]]
    assert "scheduler_unsupported" in kinds and "scheduler_query_failed" not in kinds


@pytest.mark.parametrize("provider", ["pbs", "lsf", "flux"])
def test_sched_info_failed_queue_read_raises_the_raw_error(provider):
    document = _sched_info(provider, {})
    assert document["partitions"] == []
    failed = [issue for issue in document["issues"] if issue["kind"] == "scheduler_query_failed"]
    assert failed and "not found" in failed[0]["message"]
