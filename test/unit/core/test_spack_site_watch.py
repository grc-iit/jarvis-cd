"""Stall watching of jarvis commands (Spack, the compiler probe), and
refusals in issues.jsonl."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jarvis_cd.core import spack_site
from jarvis_cd.core.spack_install import (
    InstallContext,
    InstallFailed,
    prepare_chained_spack,
    probe_compiler,
)
from jarvis_cd.util import stall

HAS_PROC = os.path.isdir("/proc/self")


def _short_window():
    return mock.patch.multiple(stall, stall_seconds=mock.Mock(return_value=1),
                               POLL_SECONDS=0.2, PROGRESS_SECONDS=1)


def _watch(command):
    said = []
    with _short_window():
        completed = spack_site.run_watched(command, None, lambda text, **_: said.append(text),
                                           what="probe")
    return completed, said


def _stub_compiler(directory, body):
    """A ``cc`` that runs ``body`` and then writes a runnable hello program."""
    path = Path(directory) / "cc"
    path.write_text(
        f"#!{sys.executable}\n"
        "import os, sys, time\n"
        f"{body}\n"
        "out = sys.argv[sys.argv.index('-o') + 1]\n"
        "open(out, 'w').write('#!/bin/sh\\necho hello\\n')\n"
        "os.chmod(out, 0o755)\n")
    path.chmod(0o755)
    return str(path)


@unittest.skipUnless(HAS_PROC, "stall detection reads /proc")
class TestRunWatched(unittest.TestCase):
    def test_stalled_command_is_retried_then_refused_with_its_output(self):
        script = "import sys, time; print('indexing', flush=True); time.sleep(600)"
        completed, said = _watch([sys.executable, "-c", script])

        self.assertEqual(completed.returncode, 124)
        self.assertIn("indexing", completed.stdout)
        stall = getattr(completed, "stall")
        self.assertFalse(stall["recovered"])
        self.assertEqual([item["attempt"] for item in stall["stalls"]], [1, 2])
        self.assertIn("indexing", stall["stalls"][0]["output_tail"])
        self.assertTrue(any("retrying (attempt 2/2)" in line for line in said))
        message = spack_site._failed_message("spack compiler find", completed)
        self.assertIn("stalled", message)
        self.assertIn("indexing", message)
        self.assertEqual(spack_site._failed_detail(completed)["exit_code"], 124)

    def test_silent_busy_command_runs_past_the_window(self):
        script = "import time\nend = time.time() + 4\nwhile time.time() < end: pass\nprint('done')"
        completed, said = _watch([sys.executable, "-c", script])

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout.strip(), "done")
        self.assertIsNone(getattr(completed, "stall", None))
        self.assertTrue(any("still running" in line for line in said))


@unittest.skipUnless(HAS_PROC, "stall detection reads /proc")
class TestCompilerProbeWatch(unittest.TestCase):
    def test_busy_silent_compile_runs_past_the_window(self):
        with tempfile.TemporaryDirectory() as temp:
            cc = _stub_compiler(temp, "end = time.time() + 3\nwhile time.time() < end: pass")
            with _short_window():
                result = probe_compiler({"cc": cc}, {})["cc"]

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["step"], "run")
        self.assertIn("hello", result["output"])
        self.assertNotIn("stall", result)

    def test_stalled_compile_is_reported_with_its_output(self):
        with tempfile.TemporaryDirectory() as temp:
            cc = _stub_compiler(temp, "print('cc1: reading', flush=True); time.sleep(600)")
            with _short_window():
                result = probe_compiler({"cc": cc}, {})["cc"]

        self.assertFalse(result["ok"])
        self.assertEqual(result["step"], "compile")
        self.assertIsNone(result["exit_code"])
        self.assertIn("stalled", result["error"])
        self.assertIn("cc1: reading", result["output"])
        self.assertEqual(len(result["stall"]["stalls"]), stall.STALL_ATTEMPTS)


class TestRefusalReachesIssues(unittest.TestCase):
    def test_site_prep_refusal_lands_in_issues_jsonl(self):
        with tempfile.TemporaryDirectory() as temp:
            context = InstallContext(install_dir=Path(temp) / "install").open()
            refusal = spack_site._refuse(
                "spack_compiler_find_failed", "spack compiler find stalled",
                root="/x/spack", evidence={"exit_code": 124, "output": "==> Reading config"})
            try:
                with mock.patch.object(spack_site, "prepare_site", side_effect=refusal):
                    with self.assertRaises(InstallFailed) as raised:
                        prepare_chained_spack(context, Path("/site/spack"),
                                              Path(temp) / "spacks" / "0.23.1-x")
            finally:
                context.close()

            failure = raised.exception
            self.assertEqual(failure.exit_code, 5)
            self.assertEqual(failure.to_dict()["refusal"]["schema_version"],
                             spack_site.SITE_PREP_SCHEMA)
            lines = context.issues_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            issue = json.loads(lines[0])
            self.assertEqual(issue["kind"], "spack_compiler_find_failed")
            self.assertEqual(issue["issue_id"], refusal.issues[0]["issue_id"])
            self.assertEqual(issue["stage"], "spack_site_prep")
            self.assertFalse(issue["answerable"])
            self.assertEqual(issue["details"]["output"], "==> Reading config")
            self.assertIn("REFUSED", context.log_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
