"""Install temp files and full storage (``storage_unusable``).

The compiler probe and Spack's builds write their temp files to the
install's own temp directory, not the node's ``/tmp``; a full or unwritable
disk is a node problem naming the host and path, never a compiler question.
"""

import os
import subprocess
from pathlib import Path
from typing import Any, cast
from unittest import mock

import pytest
import yaml

from jarvis_cd.core import spack_install as si


class _Runner:
    """A SpackRunner stand-in: answers ``spack config get config``."""

    def __init__(self, config=None, rc=0):
        self.config, self.rc = config, rc
        self.calls = []
        self.environment = {}
        self.config_overrides = []

    def run(self, arguments, stream=False):
        self.calls.append(list(arguments))
        out = yaml.safe_dump({"config": self.config or {}})
        return subprocess.CompletedProcess(arguments, self.rc, out, "")


def _runner(config=None) -> Any:
    return cast(Any, _Runner(config))


def _context(tmp_path, mode="install"):
    context = si.InstallContext(install_dir=tmp_path / "install",
                                decisions_path=tmp_path / "d.yaml", mode=mode)
    context.open()
    return context


# -- recognising full storage ------------------------------------------------

@pytest.mark.parametrize("line, path", [
    ("Error: error closing /tmp/ccSN2wEK.s: No space left on device", "/tmp/ccSN2wEK.s"),
    ("OSError: [Errno 28] No space left on device: "
     "'/tmp/u/jarvis-spack-stage/spack-stage-ior/x.tar'",
     "/tmp/u/jarvis-spack-stage/spack-stage-ior/x.tar"),
    ("tar: spack-src/a.c: Cannot write: No space left on device", None),
    ("cc1: error: /scratch/q/x.o: Disk quota exceeded", "/scratch/q/x.o"),
])
def test_storage_fault_names_the_path(line, path):
    fault = si.storage_fault(f"==> Installing ior\n{line}\n")
    assert fault is not None
    assert fault["path"] == path
    assert "Installing" not in str(fault["error"])


def test_other_errors_are_not_storage():
    assert si.storage_fault("configure: error: C compiler cannot create executables") is None
    assert si.storage_fault("") is None


def test_storage_problem(tmp_path):
    assert si.storage_problem(tmp_path) is None
    assert "cannot write" in str(si.storage_problem(tmp_path / "absent"))
    assert "MiB free" in str(si.storage_problem(tmp_path, min_free_bytes=2**62))
    assert list(tmp_path.iterdir()) == []


def test_storage_unusable_is_a_node_problem_without_choices():
    issue = si.storage_unusable_issue(host="node-a", path="/tmp/x.s",
                                      error="No space left on device",
                                      role="storage", origin="spack install")
    assert issue["kind"] == "storage_unusable"
    assert issue["scope"] == "node"
    assert issue["answerable"] is False
    assert issue["candidates"] == []
    assert (issue["host"], issue["path"], issue["directory"]) == ("node-a", "/tmp/x.s", "/tmp")
    assert "node-a" in issue["remedy"] and si.INSTALL_TMPDIR_ENV in issue["remedy"]
    assert "compiler" not in issue["remedy"]


# -- classifying a failed install ----------------------------------------------

def test_a_full_disk_in_the_output_is_storage_not_build_failed():
    output = ("==> Installing ior-3.3.0-abc\n"
              "==> Error: OSError: [Errno 28] No space left on device: "
              "'/tmp/u/jarvis-spack-stage/s/ior.tar.gz'\n")
    issue = si.classify_install_failure(output, stage="install", packages=["ior"])
    assert issue["kind"] == "storage_unusable"
    assert issue["path"] == "/tmp/u/jarvis-spack-stage/s/ior.tar.gz"
    assert issue["packages_failed"] == ["ior"]


@pytest.mark.skipif(os.name == "nt", reason="Spack prints POSIX build-log paths")
def test_a_full_disk_in_the_build_log_is_storage(tmp_path):
    log = tmp_path / "spack-build-out.txt"
    log.write_text("checking for gcc... gcc\n"
                   "Error: error closing /tmp/ccSN2wEK.s: No space left on device\n"
                   "configure: error: C compiler cannot create executables\n")
    output = ("==> Error: ProcessError: Command exited with status 77\n"
              f"See build log for details:\n  {log}\n")
    issue = si.classify_install_failure(output, stage="install", packages=["zlib"])
    assert issue["kind"] == "storage_unusable"
    assert issue["path"] == "/tmp/ccSN2wEK.s"


def test_cannot_create_executables_is_storage_only_when_the_temp_dir_fails(tmp_path):
    output = "configure: error: C compiler cannot create executables\n"
    issue = si.classify_install_failure(output, stage="install", packages=["zlib"],
                                        temp_dir=tmp_path / "gone")
    assert issue["kind"] == "storage_unusable"
    assert issue["path"] == str(tmp_path / "gone")
    issue = si.classify_install_failure(output, stage="install", packages=["zlib"],
                                        temp_dir=tmp_path)
    assert issue["kind"] == "compiler_unavailable"


# -- the build-host compiler probe ---------------------------------------------

_GCC = {"spec": "gcc@11.4.0", "paths": {"cc": "/usr/bin/gcc", "cxx": "/usr/bin/g++"}}


def test_a_probe_spoiled_by_a_full_disk_keeps_the_compiler(tmp_path):
    spoiled = {"cc": "compile exited 1: error closing /tmp/ccX.s: No space left on device",
               "cxx": "ok"}
    with mock.patch.object(si, "_executable_problem", return_value=None), \
            mock.patch.object(si, "probe_compiler", return_value=spoiled) as probe:
        document = si.verify_compilers([_GCC], probe=True, temp_dir=tmp_path)
    assert probe.call_args.args[2] == tmp_path
    item = document["compilers"][0]
    assert item["usable"] is True
    assert "failures" not in item
    assert "c" in item["languages"]
    assert document["usable"] == ["gcc@11.4.0"]
    assert document["storage_faults"][0]["path"] == "/tmp/ccX.s"
    assert document["temp_dir"] == str(tmp_path)


def test_a_real_compiler_failure_still_makes_it_unusable(tmp_path):
    broken = {"cc": "compile exited 1: cannot find crt1.o", "cxx": "ok"}
    with mock.patch.object(si, "_executable_problem", return_value=None), \
            mock.patch.object(si, "probe_compiler", return_value=broken):
        document = si.verify_compilers([_GCC], probe=True, temp_dir=tmp_path)
    assert document["compilers"][0]["usable"] is False
    assert "storage_faults" not in document


@pytest.mark.skipif(os.name == "nt", reason="the probe runs a POSIX shell script")
def test_the_probe_compiles_in_the_temp_dir(tmp_path):
    compiler = tmp_path / "cc"
    compiler.write_text('#!/bin/sh\necho "$TMPDIR" > "$TMPDIR/seen"\n'
                        'printf "#!/bin/sh\\nexit 0\\n" > "$3"; chmod +x "$3"\n')
    compiler.chmod(0o755)
    temp = tmp_path / "tmp"
    temp.mkdir()
    assert si.probe_compiler({"cc": str(compiler)}, {}, temp) == {"cc": "ok"}
    assert (temp / "seen").read_text().strip() == str(temp)


# -- the install's temp directory ----------------------------------------------

def test_open_makes_the_temp_dir_under_the_install_and_close_removes_it(tmp_path):
    context, runner = _context(tmp_path), _runner()
    try:
        temp = si.InstallTemp.open(context, runner)
        assert context.storage_issue is None
        assert temp.path is not None and temp.path.parent == tmp_path / "install" / "tmp"
        assert context.temp_dir == temp.path
        assert runner.environment["TMPDIR"] == str(temp.path)
        (temp.path / "stage").mkdir()
        (temp.path / "stage" / "f").write_text("x")
        (temp.path / "stage").chmod(0o500)
        temp.prepare_stage(runner, context)
        assert runner.calls == [["config", "get", "config"]]
        assert runner.config_overrides == [
            f"config:build_stage:{temp.path / 'spack-stage'}"]
        assert temp.describe()["build_stage"]["source"] == "jarvis"
        temp.close(context)
        assert not temp.path.exists()
    finally:
        context.close()


@pytest.mark.parametrize("stage, follows", [
    (["$tempdir/$user/jarvis-spack-stage"], True),
    ("/scratch/site/stage", False),
])
def test_a_configured_build_stage_is_left_alone(tmp_path, stage, follows):
    context, runner = _context(tmp_path), _runner({"build_stage": stage})
    try:
        temp = si.InstallTemp.open(context, runner)
        temp.prepare_stage(runner, context)
        assert runner.config_overrides == []
        assert temp.build_stage["follows_tmpdir"] is follows
        temp.close(context)
    finally:
        context.close()


def test_an_unusable_temp_dir_is_storage_unusable(tmp_path, monkeypatch):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    monkeypatch.setenv(si.INSTALL_TMPDIR_ENV, str(blocker))
    context, runner = _context(tmp_path), _runner()
    try:
        temp = si.InstallTemp.open(context, runner)
        issue = context.storage_issue
        assert temp.path is None and context.temp_dir is None
        assert "TMPDIR" not in runner.environment
        assert issue is not None and issue["kind"] == "storage_unusable"
        assert issue["path"] == str(blocker)
        assert issue["role"] == "the install temp directory"
    finally:
        context.close()


def test_too_little_free_space_is_storage_unusable(tmp_path, monkeypatch):
    monkeypatch.setenv(si.INSTALL_TMP_MIN_MB_ENV, str(2**40))
    context, runner = _context(tmp_path), _runner()
    try:
        temp = si.InstallTemp.open(context, runner)
        assert context.storage_issue is not None
        assert "MiB free" in context.storage_issue["error"]
        assert list((tmp_path / "install" / "tmp").iterdir()) == []
        assert temp.describe()["problem"]
    finally:
        context.close()


def test_a_build_log_in_the_temp_dir_is_kept(tmp_path):
    context = _context(tmp_path)
    try:
        temp = si.InstallTemp.open(context, _runner())
        assert temp.path is not None
        stage = temp.path / "spack-stage" / "spack-stage-zlib-1.3-abc"
        stage.mkdir(parents=True)
        (stage / "spack-build-out.txt").write_text("log")
        issue = {"build_log": str(stage / "spack-build-out.txt")}
        si.keep_stage_build_log(context, issue)
        temp.close(context)
        kept = Path(issue["build_log"])
        assert kept.read_text() == "log"
        assert kept.parent == tmp_path / "install" / "build-logs" / stage.name
        assert issue["build_log_stage"] == str(stage / "spack-build-out.txt")
    finally:
        context.close()


# -- answers resent every round -------------------------------------------------

def test_the_same_answer_again_is_not_re_recorded(tmp_path):
    decisions = si.Decisions(tmp_path / "d.yaml")
    site = {"kind": "compiler_unavailable", "subject": "compiler",
            "issue_id": "sha256:" + "a" * 64, "scope": "site"}
    package = {"kind": "site_copy", "subject": "ior", "package": "ior",
               "issue_id": "sha256:" + "b" * 64, "scope": "package"}
    decisions.record(site, {"constraint": "gcc@11.4.0", "answered_at": "T1"})
    decisions.record(package, {"constraint": "spack_build", "answered_at": "T1"})
    before = (tmp_path / "d.yaml").read_text()
    decisions.record(site, {"constraint": "gcc@11.4.0", "answered_at": "T2",
                            "basis": "resent by relay"})
    decisions.record(package, {"constraint": "spack_build", "answered_at": "T2"})
    assert (tmp_path / "d.yaml").read_text() == before
    decisions.record(site, {"constraint": "gcc@13.2.0", "answered_at": "T3"})
    recorded = si.Decisions(tmp_path / "d.yaml").site("compiler")
    assert recorded is not None and recorded["answered_at"] == "T3"
