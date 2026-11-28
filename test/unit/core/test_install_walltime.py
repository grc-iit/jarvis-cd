"""Install job wall time from JARVIS's own build estimate."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from jarvis_cd.core import install_walltime as walltime
from jarvis_cd.core.pipeline import Pipeline

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
    for name in ("JARVIS_INSTALL_STARTUP_SECONDS", "JARVIS_INSTALL_TIME_FACTOR"):
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


def _measured(nodes, seconds, **packages):
    """Costs: each node built here in ``seconds``, plus per-package fields."""
    return {**{node["name"]: {"node_build_seconds": seconds} for node in nodes},
            **packages}


def _record(missing):
    return {"checked_at": "t", "where": {"host": "comp-1"},
            "packages": {pkg_id: {"status": "missing", "missing": nodes}
                         for pkg_id, nodes in missing.items()}}


def test_time_is_startup_plus_estimate_times_factor(site):
    # ParaView: 3 missing nodes, each built here before, and a timed check
    site.costs(_measured(_missing(3), 6000, paraview={"check_seconds": 600}))
    document = _resolve(_ppl([PARAVIEW]), _record({"pv": _missing(3)}),
                        partition="long")
    assert document["source"] == "jarvis_estimate"
    # (600 startup + 600 check + 3 x 6000 build) x 1.5 = 8 h
    assert document["needed_seconds"] == 19200
    assert document["time"] == "08:00:00"
    assert document["capped_at_max_time"] is False
    assert document["may_take_more_than_one_job"] is False
    assert "note" not in document and "issues" not in document
    assert document["partition_limit"]["max_time"] == "2-00:00:00"


def test_measured_estimate_past_max_time_is_capped_and_may_take_more_jobs(site):
    site.costs(_measured(_missing(3), 6000, paraview={"check_seconds": 600}))
    document = _resolve(_ppl([PARAVIEW]), _record({"pv": _missing(3)}))
    assert document["partition_limit"]["partition"] == "compute"  # slurm default
    assert document["time"] == "04:00:00" and document["capped_at_max_time"]
    assert document["may_take_more_than_one_job"] is True
    assert "more than one job" in document["note"]
    assert walltime.summary(document)["note"] == document["note"]


def test_safety_factor_alone_is_capped_but_fits_one_job(site):
    site.costs(_measured(_missing(5), 2160, paraview={"check_seconds": 600}))
    document = _resolve(_ppl([PARAVIEW]), _record({"pv": _missing(5)}))
    # needed 12000 s fits 4 h; 12000 x 1.5 does not: capped, one job
    assert document["needed_seconds"] == 12000
    assert document["time"] == "04:00:00" and document["capped_at_max_time"]
    assert document["may_take_more_than_one_job"] is False


def test_any_unmeasured_node_gives_the_partition_max_time(site):
    nodes = _missing(10)
    site.costs({**_measured(nodes[:9], 60), "mytool": {"check_seconds": 60},
                "paraview": {"check_seconds": 60}})
    record = _record({"tool": nodes})
    record["packages"]["pv"] = {"status": "installed"}
    document = _resolve(_ppl([PARAVIEW, OTHER]), record, partition="long")
    builds = {item["pkg_id"]: item for item in document["estimate"]["packages"]
              if item["phase"] == "build"}
    assert builds["pv"]["seconds"] == 0
    assert builds["tool"]["seconds"] is None
    assert builds["tool"]["unmeasured"] == ["n9"]
    assert builds["tool"]["basis"].startswith("unmeasured: 1 of 10")
    assert document["estimate"]["unknown"] == ["tool"]
    assert document["source"] == "partition_max_time"
    assert document["time"] == "2-00:00:00" and document["time_seconds"] == 2 * 86400
    assert document["may_take_more_than_one_job"] is True
    assert "tool" in document["why"] and "more than one job" in document["note"]


def test_explicit_time_wins(site):
    document = _resolve(_ppl([PARAVIEW]), None, explicit_time="01:00:00")
    assert document == {"schema_version": walltime.WALLTIME_SCHEMA,
                        "source": "explicit", "time": "01:00:00"}


def test_unmeasured_with_no_max_time_leaves_the_scheduler_limit(site):
    unlimited = [{**PARTITIONS[0], "max_time": "UNLIMITED", "max_time_s": None}]
    document = _resolve(_ppl([OTHER]), None, partitions=unlimited)
    assert document["source"] == "scheduler_limit"
    assert document["time"] is None
    assert document["may_take_more_than_one_job"] is True
    [build] = [item for item in document["estimate"]["packages"]
               if item["phase"] == "build"]
    assert build["seconds"] is None and build["basis"].startswith("unmeasured:")


def test_record_stale_only_by_decisions_still_sizes_the_build(site):
    site.costs(_measured(_missing(9), 60, mytool={"check_seconds": 60}))
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


def _packages_digest(pkg_def):
    from jarvis_cd.core import installer

    with patch("jarvis_cd.core.spack_install.spack_env_enabled", return_value=False):
        return installer._capture_inputs(SimpleNamespace(name="p"), [pkg_def])["packages"]


def test_packages_digest_ignores_replayed_site_copy_answers(site):
    import copy

    from jarvis_cd.core import site_copies

    written = {"pkg_id": "ior", "pkg_type": "builtin.ior",
               "config": {"install_method": "spack", "install_query": "ior@3.3.0+hdf5",
                          "install_site_search": True}}
    expected = _packages_digest(written)
    # the check job applies the person's answer, a later +plan replays the record
    for why in ("the person's answer", "the site's recorded answer"):
        built = copy.deepcopy(written)
        site_copies.apply_choice(built, "spack", None, why, "sha256:i1")
        assert _packages_digest(built) == expected
    chosen = copy.deepcopy(written)
    site_copies.apply_choice(chosen, "site:ior", {"source": "spack", "spack_hash": "abc"},
                             "the person's answer", "sha256:i1")
    assert chosen["config"]["install_method"] == site_copies.METHOD
    assert _packages_digest(chosen) == expected
    changed = copy.deepcopy(written)
    changed["config"]["install_query"] = "ior@3.3.0"
    assert _packages_digest(changed) != expected


def test_packages_digest_keeps_the_chosen_method_not_its_why(site):
    import copy

    from jarvis_cd.core.method_install import apply_method

    written = {"pkg_id": "x", "pkg_type": "x.tool",
               "config": {"install_methods": [{"method": "pip", "query": "x"},
                                              {"method": "spack", "query": "x"}]}}
    login, job = copy.deepcopy(written), copy.deepcopy(written)
    apply_method(login, {"method": "pip", "query": "x"}, "fits (pip on this host)")
    apply_method(job, {"method": "pip", "query": "x"}, "fits (pip at /node/bin/pip)")
    assert _packages_digest(login) == _packages_digest(job)
    other = copy.deepcopy(written)
    apply_method(other, {"method": "spack", "query": "x"}, "fits (pip on this host)")
    assert _packages_digest(other) != _packages_digest(login)

def test_check_job_uses_check_estimates_only(site):
    site.costs({"mytool": {"check_seconds": 60}})
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
    assert build["already_built"] == 20 and build["seconds"] is None
    assert len(build["unmeasured"]) == 42
    summary = walltime.summary(document)
    assert len(summary["already_built"]) == 20
    assert summary["source"] == "partition_max_time"


def test_measured_nodes_estimate_what_is_left(site):
    # Spack's per-node times: only the nodes still missing are summed, and a
    # whole prior build of the package does not count
    missing = _missing(10)
    site.costs({"mytool": {"build_seconds": 6000}, **_measured(missing[4:], 50)})
    with patch("jarvis_cd.core.installer.Installer.get_installers",
               return_value={"spack": [OTHER]}), \
            patch("jarvis_cd.core.installer.read_install_check",
                  return_value=(_record({"tool": missing}), "")):
        estimate = walltime.install_estimate(
            _ppl([OTHER]), install_check=False,
            built_hashes=[node["hash"] for node in missing[:4]])
    [build] = [item for item in estimate["packages"] if item["phase"] == "build"]
    assert build["seconds"] == 6 * 50
    assert "6 missing Spack node(s), each built before" in build["basis"]
    assert "4 of 10 missing node(s) already built" in build["basis"]


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


def _real_pipeline(tmp_path: Path) -> Pipeline:
    """A real Pipeline whose config/shared/private roots are test-local."""
    repository_root = Path(__file__).parents[3]
    jarvis = SimpleNamespace(
        hostfile=None,
        get_pipeline_dir=lambda name: tmp_path / "config" / "pipelines" / name,
        get_pipeline_shared_dir=lambda name: tmp_path / "shared" / name,
        get_pipeline_private_dir=lambda name: tmp_path / "private" / name,
        get_builtin_repo_path=lambda: repository_root / "builtin",
        set_current_pipeline=lambda _name: None,
        jarvis_root=tmp_path / "jarvis-root",
    )
    with patch("jarvis_cd.core.pipeline.Jarvis.get_instance", return_value=jarvis):
        pipeline = Pipeline()
    pipeline.create("example")
    pipeline.scheduler = {"name": "slurm", "nodes": 1}
    pipeline.packages = [
        {
            "pkg_type": "builtin.echo",
            "pkg_id": "echo",
            "pkg_name": "echo",
            "global_id": "example.echo",
            "config": {},
        }
    ]
    pipeline.env = {"PATH": "/spack/a/bin", "SPACK_ROOT": "/opt/spack"}
    pipeline.last_loaded_file = "/operator/source.yaml"
    pipeline.save()
    return pipeline


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


def test_an_install_longer_than_max_time_is_submitted_at_max_time(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_INSTALL_STARTUP_SECONDS", str(5 * 3600))
    pipeline = _real_pipeline(tmp_path)
    root = tmp_path / "shared" / "example" / "executions" / "e1"
    with patch.object(walltime, "slurm_partitions", return_value=PARTITIONS):
        spec, shape = pipeline._submission_scheduler_spec(root, "install", False)
    assert spec["time"] == "04:00:00"
    estimate = shape["time_estimate"]
    assert estimate["capped_at_max_time"] and estimate["may_take_more_than_one_job"]
    assert "more than one job" in estimate["note"]
