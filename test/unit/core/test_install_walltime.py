"""Install job wall time from JARVIS's own build estimate."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from jarvis_cd.core import install_placement as placement
from jarvis_cd.core import install_walltime as walltime
from jarvis_cd.core.issues import JarvisIssueError

from test.unit.core.test_scheduler_submission import _real_pipeline

PARAVIEW = {"pkg_id": "pv", "pkg_type": "builtin.paraview",
            "config": {"install_method": "spack", "install": "paraview@5.13.1"}}
OTHER = {"pkg_id": "tool", "pkg_type": "x.tool",
         "config": {"install_method": "spack", "install": "mytool"}}

PARTITIONS = [
    {"partition": "compute", "default": True, "state": "UP", "max_time": "04:00:00",
     "max_time_s": 4 * 3600, "default_time": None, "default_time_s": None},
    {"partition": "long", "default": False, "state": "UP", "max_time": "2-00:00:00",
     "max_time_s": 2 * 86400, "default_time": None, "default_time_s": None},
]


def _missing(count, prefix="n"):
    return [{"name": f"{prefix}{index}", "hash": f"{index:032x}", "source": "build"}
            for index in range(count)]


@pytest.fixture
def site(tmp_path, monkeypatch):
    decisions = tmp_path / "install" / "decisions.yaml"
    decisions.parent.mkdir(parents=True)
    monkeypatch.setenv("JARVIS_SPACK_DECISIONS", str(decisions))
    for name in ("JARVIS_INSTALL_STARTUP_SECONDS", "JARVIS_INSTALL_TIME_FACTOR",
                 "JARVIS_INSTALL_UNKNOWN_BUILD_SECONDS"):
        monkeypatch.delenv(name, raising=False)

    def costs(packages):
        (decisions.parent / "install-costs.json").write_text(
            json.dumps({"schema_version": "x", "packages": packages}), encoding="utf-8")

    return SimpleNamespace(costs=costs, path=tmp_path)


def _ppl(packages, store_records=()):
    store = SimpleNamespace(list=lambda: list(store_records), executions_dir=Path("/x"))
    return SimpleNamespace(name="viz", packages=packages,
                           _execution_store=lambda: store)


def _resolve(ppl, record, *, partitions=PARTITIONS, **kwargs):
    kwargs.setdefault("install_check", False)
    with patch("jarvis_cd.core.installer.Installer.get_installers",
               return_value={"spack": list(ppl.packages)}), \
            patch("jarvis_cd.core.installer.read_install_check",
                  return_value=(record, "" if record else "no_record: none")), \
            patch.object(walltime, "slurm_partitions", return_value=partitions):
        return walltime.resolve(ppl, provider="slurm", partition=kwargs.pop(
            "partition", None), **kwargs)


def _record(missing):
    return {"checked_at": "t", "where": {"host": "comp-1"},
            "packages": {pkg_id: {"status": "missing", "missing": nodes}
                         for pkg_id, nodes in missing.items()}}


def test_time_is_startup_plus_estimate_times_factor(site):
    # ParaView: a prior 5 h build recorded at this site, 62 nodes missing
    site.costs({"paraview": {"build_seconds": 18000, "build_host": "comp-2"}})
    document = _resolve(_ppl([PARAVIEW]), _record({"pv": _missing(62)}),
                        partition="long")
    assert document["source"] == "jarvis_estimate"
    # (600 startup + 600 known-heavy check + 18000 build) x 1.5 = 8 h
    assert document["needed_seconds"] == 19200
    assert document["time"] == "08:00:00"
    assert document["capped_at_max_time"] is False
    assert document["issues"] == []
    assert document["partition_limit"]["max_time"] == "2-00:00:00"


def test_estimate_past_max_time_is_a_typed_issue_not_a_cut(site):
    site.costs({"paraview": {"build_seconds": 18000}})
    document = _resolve(_ppl([PARAVIEW]), _record({"pv": _missing(62)}))
    assert document["partition_limit"]["partition"] == "compute"  # slurm default
    assert document["time"] == "04:00:00" and document["capped_at_max_time"]
    [issue] = document["issues"]
    assert issue["kind"] == "install_time_exceeds_partition"
    assert issue["field"] == "scheduler.install"
    sets = [candidate.get("set") for candidate in issue["candidates"]]
    assert {"scheduler.install.partition": "long"} in sets
    assert {"scheduler.install.time": "04:00:00"} in sets


def test_safety_factor_alone_is_capped_without_an_issue(site):
    site.costs({"paraview": {"build_seconds": 10800}})
    document = _resolve(_ppl([PARAVIEW]), _record({"pv": _missing(5)}))
    # needed 12000 s fits 4 h; 12000 x 1.5 does not: capped, no question
    assert document["needed_seconds"] == 12000
    assert document["time"] == "04:00:00" and document["capped_at_max_time"]
    assert document["issues"] == []


def test_node_count_estimate_and_installed_packages(site):
    record = _record({"tool": _missing(10)})
    record["packages"]["pv"] = {"status": "installed"}
    document = _resolve(_ppl([PARAVIEW, OTHER]), record, partition="long")
    builds = {item["pkg_id"]: item for item in document["estimate"]["packages"]
              if item["phase"] == "build"}
    assert builds["pv"]["seconds"] == 0
    assert builds["tool"]["seconds"] == 10 * placement.NODE_BUILD_SECONDS
    # checks: paraview 600 (known heavy) + mytool 60
    assert document["needed_seconds"] == 600 + 600 + 60 + 10 * placement.NODE_BUILD_SECONDS


def test_explicit_time_wins(site):
    document = _resolve(_ppl([PARAVIEW]), None, explicit_time="01:00:00")
    assert document == {"schema_version": walltime.WALLTIME_SCHEMA,
                        "source": "explicit", "time": "01:00:00", "issues": []}


def test_unknown_estimate_gets_a_default_allowance_never_the_partition_max(site):
    # never built here, not known-heavy, no check record: the time still
    # comes from an estimate, so Slurm never applies DefaultTime / MaxTime
    document = _resolve(_ppl([OTHER]), None, partition="long")
    assert document["source"] == "jarvis_estimate"
    assert document["estimate"]["allowance"] == ["tool"]
    # (600 startup + 60 check + 3600 allowance) x 1.5 = 6390 s, up to 5 min
    assert document["needed_seconds"] == 600 + 60 + walltime.UNKNOWN_BUILD_SECONDS
    assert document["time"] == "01:50:00" and not document["capped_at_max_time"]
    assert "tool" in document["why"] and "+check +submit" in document["why"]
    [build] = [item for item in document["estimate"]["packages"]
               if item["phase"] == "build"]
    assert build["allowance"] and "no_record" in build["basis"]
    assert walltime.summary(document)["estimate"]["allowance"] == ["tool"]
    assert document["issues"] == []


def test_unknown_build_allowance_is_configurable(site, monkeypatch):
    monkeypatch.setenv("JARVIS_INSTALL_UNKNOWN_BUILD_SECONDS", "600")
    document = _resolve(_ppl([OTHER]), None, partition="long")
    assert document["needed_seconds"] == 600 + 60 + 600
    assert document["time"] == "00:35:00"


def test_record_stale_only_by_decisions_still_sizes_the_build(site):
    # answers recorded after the check (often to its own questions)
    with patch("jarvis_cd.core.installer.Installer.get_installers",
               return_value={"spack": [OTHER]}), \
            patch("jarvis_cd.core.installer.read_install_check",
                  return_value=(_record({"tool": _missing(9)}),
                                "inputs_changed: decisions")) as read, \
            patch.object(walltime, "slurm_partitions", return_value=PARTITIONS):
        document = walltime.resolve(_ppl([OTHER]), provider="slurm",
                                    partition=None, install_check=False)
    assert read.call_args.kwargs == {"for_estimate": True}
    record = document["estimate"]["check_record"]
    assert record["used"] and record["stale"] == "inputs_changed: decisions"
    assert document["estimate"]["allowance"] == []
    # (600 + 60 + 9 x 60) x 1.5 = 1800 s
    assert document["time"] == "00:30:00"


def _check_file(tmp_path, stored):
    from jarvis_cd.core import installer

    path = tmp_path / "install-check.json"
    path.write_text(json.dumps({"schema_version": installer.INSTALL_CHECK_SCHEMA,
                                "outcome": "missing", "inputs": stored}),
                    encoding="utf-8")
    return path


@pytest.mark.parametrize("changed,for_estimate,used,why", [
    ({"decisions": "d2"}, True, True, "inputs_changed: decisions"),
    ({"decisions": "d2", "spack_lock": "l2"}, True, True,
     "inputs_changed: decisions, spack_lock"),
    ({"decisions": "d2"}, False, False, "inputs_changed: decisions"),
    ({"spack": "s2"}, True, False, "inputs_changed: spack"),
    ({"packages": "p2", "decisions": "d2"}, True, False,
     "inputs_changed: decisions, packages"),
    ({}, True, True, ""),
])
def test_read_install_check_tolerates_only_decisions_for_an_estimate(
        tmp_path, changed, for_estimate, used, why):
    from jarvis_cd.core import installer

    stored = {"packages": "p1", "decisions": "d1", "spack": "s1", "spack_lock": "l1"}
    path = _check_file(tmp_path, stored)
    with patch.object(installer, "install_check_path", return_value=path), \
            patch.object(installer, "_capture_inputs",
                         return_value={**stored, **changed}):
        document, reason = installer.read_install_check(
            None, [], for_estimate=for_estimate)
    assert (document is not None) is used
    assert reason == why


def test_check_job_uses_check_estimates_only(site):
    document = _resolve(_ppl([OTHER]), None, install_check=True)
    assert document["needed_seconds"] == 600 + 60
    assert document["time"] == "00:20:00"  # 990 s, rounded up to 5 min


def _attempt(tmp_path, execution_id, created, phase, log_lines=(), *, state="failed",
             provider_state="TIMEOUT"):
    log = tmp_path / f"{execution_id}.log"
    log.write_text("".join(line + "\n" for line in log_lines), encoding="utf-8")
    return SimpleNamespace(
        execution_id=execution_id, created_at=created, state=state, terminal=True,
        scheduler_provider="slurm", scheduler_native_id=execution_id[-2:],
        metadata={"action": "install",
                  "install": {"mode": "install", "phase": phase, "log": str(log)},
                  "scheduler_observation": {"provider_state": provider_state}})


def test_resubmit_after_timeout_reports_built_nodes_and_counts_the_rest(site):
    built = [f"==> n{i}: Successfully installed n{i}-1.0-{i:032x}" for i in range(20)]
    records = [
        _attempt(site.path, "inst-03", "2025-01-03", "building", built[10:]),
        _attempt(site.path, "inst-02", "2025-01-02", "building", built[:10]
                 + ["[+] /usr (external glibc)"]),
        _attempt(site.path, "inst-01", "2025-01-01", "done", state="succeeded",
                 provider_state="COMPLETED"),
        _attempt(site.path, "inst-00", "2024-12-31", "building",
                 ["==> zz: Successfully installed zz-1-" + "f" * 32]),
    ]
    document = _resolve(_ppl([OTHER], records), _record({"tool": _missing(62)}),
                        partition="long")
    previous = document["previous"]
    assert [a["execution_id"] for a in previous["attempts"]] == ["inst-03", "inst-02"]
    assert previous["attempts"][0]["provider_state"] == "TIMEOUT"
    assert len(previous["already_built"]) == 20
    [build] = [item for item in document["estimate"]["packages"]
               if item["phase"] == "build"]
    assert build["already_built"] == 20 and \
        build["seconds"] == 42 * placement.NODE_BUILD_SECONDS
    summary = walltime.summary(document)
    assert len(summary["already_built"]) == 20
    assert summary["source"] == "jarvis_estimate"


def test_prior_build_time_is_scaled_by_what_is_left(site):
    site.costs({"mytool": {"build_seconds": 6000}})
    missing = _missing(10)
    with patch("jarvis_cd.core.installer.Installer.get_installers",
               return_value={"spack": [OTHER]}), \
            patch("jarvis_cd.core.installer.read_install_check",
                  return_value=(_record({"tool": missing}), "")):
        estimate = walltime.install_estimate(
            _ppl([OTHER]), install_check=False,
            built_hashes=[node["hash"] for node in missing[:4]])
    [build] = [item for item in estimate["packages"] if item["phase"] == "build"]
    assert build["seconds"] == 3600


def test_measured_nodes_estimate_what_is_left(site):
    # a whole prior build plus Spack's per-node times: the nodes still
    # missing are summed, not the whole build scaled
    missing = _missing(10)
    site.costs({"mytool": {"build_seconds": 6000},
                "n0": {"node_build_seconds": 3000}, "n5": {"node_build_seconds": 50}})
    with patch("jarvis_cd.core.installer.Installer.get_installers",
               return_value={"spack": [OTHER]}), \
            patch("jarvis_cd.core.installer.read_install_check",
                  return_value=(_record({"tool": missing}), "")):
        estimate = walltime.install_estimate(
            _ppl([OTHER]), install_check=False,
            built_hashes=[node["hash"] for node in missing[:4]])
    [build] = [item for item in estimate["packages"] if item["phase"] == "build"]
    assert build["seconds"] == 50 + 5 * placement.NODE_BUILD_SECONDS
    assert "1 of 6 missing Spack node(s) built before" in build["basis"]


def test_format_and_log_parsing(tmp_path):
    assert walltime.format_time(2 * 86400, "slurm") == "2-00:00:00"
    assert walltime.format_time(2 * 86400, "pbs") == "48:00:00"
    assert walltime.format_time(61, "slurm") == "00:02:00"
    log = tmp_path / "install.log"
    log.write_text("==> Installing lua-5.3.6-abc [72/155]\n"
                   "==> lua: Successfully installed lua-5.3.6-afrku6gq\n"
                   "[+] /spack/opt/lua-5.3.6-afrku6gq\n", encoding="utf-8")
    assert walltime.built_nodes(log) == [
        {"name": "lua", "spec": "lua-5.3.6-afrku6gq", "hash": "afrku6gq"}]
    assert walltime.built_nodes(tmp_path / "absent.log") == []


def test_non_slurm_has_no_cap():
    limit = walltime.partition_limit("pbs", "workq")
    assert limit["max_time_s"] is None and "Slurm" in limit["why"]


# --- the submission path -----------------------------------------------------


def test_install_job_gets_the_estimate_and_explicit_time_wins(tmp_path, monkeypatch):
    monkeypatch.delenv("JARVIS_INSTALL_STARTUP_SECONDS", raising=False)
    monkeypatch.delenv("JARVIS_INSTALL_TIME_FACTOR", raising=False)
    pipeline = _real_pipeline(tmp_path)
    root = tmp_path / "shared" / "example" / "executions" / "e1"
    with patch.object(walltime, "slurm_partitions", return_value=PARTITIONS):
        spec, shape = pipeline._submission_scheduler_spec(root, "install", False)
        # nothing to install: (600 s startup + 0) x 1.5 = 15 min
        assert spec["time"] == "00:15:00"
        assert shape["time"] == "00:15:00"
        assert shape["time_estimate"]["source"] == "jarvis_estimate"
        assert shape["time_estimate"]["partition_limit"]["max_time"] == "04:00:00"
        run_spec, _ = pipeline._submission_scheduler_spec(root, "run", False)
        assert "time" not in run_spec

        pipeline.scheduler = {"name": "slurm", "nodes": 1,
                              "install": {"time": "02:00:00"}}
        spec, shape = pipeline._submission_scheduler_spec(root, "install", False)
        assert spec["time"] == "02:00:00"
        assert shape["time_estimate"] == {"source": "explicit"}

        # scheduler.time is the run job's limit: the install job keeps
        # its own estimate (and never a partition maximum)
        pipeline.scheduler = {"name": "slurm", "nodes": 1, "time": "2-00:00:00"}
        spec, shape = pipeline._submission_scheduler_spec(root, "install", False)
        assert spec["time"] == "00:15:00"
        assert shape["time_estimate"]["source"] == "jarvis_estimate"
        run_spec, _ = pipeline._submission_scheduler_spec(root, "run", False)
        assert run_spec["time"] == "2-00:00:00"


def test_submit_refuses_an_install_longer_than_max_time(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_INSTALL_STARTUP_SECONDS", str(5 * 3600))
    pipeline = _real_pipeline(tmp_path)
    with patch.object(walltime, "slurm_partitions", return_value=PARTITIONS), \
            pytest.raises(JarvisIssueError) as refused:
        pipeline.submit(submit=False, execution_id="inst-1", action="install")
    assert refused.value.exit_code == 3
    assert refused.value.issues[0]["kind"] == "install_time_exceeds_partition"
    assert refused.value.document["install_time"]["partition_limit"][
        "partition"] == "compute"
    executions = tmp_path / "shared" / "example" / "executions"
    assert not (executions / "inst-1").exists()
