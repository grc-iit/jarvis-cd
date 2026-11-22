"""
Concurrency tests for the install decision store (decisions.yaml).

Every writer loads the store before a barrier, so each starts from the same
stale copy; without the store's lock and re-read, all but the last answer
would be lost and re-asked.
"""
import multiprocessing
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

import yaml

from jarvis_cd.core.spack_install import Decisions, forget_decisions

WRITERS = 8


def _issue(index):
    return {
        "kind": "ambiguous_variant",
        "subject": f"pkg{index}",
        "issue_id": f"sha256:{index:064x}",
        "scope": "package",
        "package": f"pkg{index}",
    }


def _site_issue():
    return {"kind": "ambiguous_provider", "subject": "mpi",
            "issue_id": "sha256:" + "f" * 64, "scope": "site"}


def _record_after_barrier(path, index, barrier):
    decisions = Decisions(Path(path))
    barrier.wait()
    decisions.record(_issue(index), {"constraint": f"+v{index}"})


def _forget_after_barrier(path, barrier):
    decisions_path = Path(path)
    barrier.wait()
    forget_decisions(decisions_path, ["mpi"])


def _recorded_packages(path):
    with open(path, "r", encoding="utf-8") as stream:
        data = yaml.safe_load(stream)
    return sorted(data.get("packages") or {}), data


class TestDecisionsLock(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix='jarvis_test_decisions_')
        self.path = os.path.join(self.test_dir, 'install', 'decisions.yaml')

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_concurrent_processes_keep_every_answer(self):
        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(WRITERS)
        workers = [
            context.Process(target=_record_after_barrier, args=(self.path, index, barrier))
            for index in range(WRITERS)
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(60)
            self.assertEqual(worker.exitcode, 0)
        packages, _ = _recorded_packages(self.path)
        self.assertEqual(packages, sorted(f"pkg{index}" for index in range(WRITERS)))

    def test_concurrent_threads_keep_every_answer(self):
        barrier = threading.Barrier(WRITERS)
        errors = []

        def run(index):
            try:
                _record_after_barrier(self.path, index, barrier)
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(index,)) for index in range(WRITERS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        self.assertEqual(errors, [])
        packages, _ = _recorded_packages(self.path)
        self.assertEqual(packages, sorted(f"pkg{index}" for index in range(WRITERS)))

    def test_forget_racing_answers_removes_only_its_selector(self):
        Decisions(Path(self.path)).record(_site_issue(), {"constraint": "^openmpi/abc1234"})
        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(WRITERS + 1)
        workers = [
            context.Process(target=_record_after_barrier, args=(self.path, index, barrier))
            for index in range(WRITERS)
        ]
        workers.append(context.Process(target=_forget_after_barrier, args=(self.path, barrier)))
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(60)
            self.assertEqual(worker.exitcode, 0)
        packages, data = _recorded_packages(self.path)
        self.assertEqual(packages, sorted(f"pkg{index}" for index in range(WRITERS)))
        self.assertNotIn("mpi", data.get("site") or {})

    def test_record_refreshes_a_stale_instance(self):
        stale = Decisions(Path(self.path))
        Decisions(Path(self.path)).record(_issue(0), {"constraint": "+v0"})
        stale.record(_issue(1), {"constraint": "+v1"})
        self.assertEqual(stale.package_constraints("pkg0"), ["+v0"])
        packages, _ = _recorded_packages(self.path)
        self.assertEqual(packages, ["pkg0", "pkg1"])


if __name__ == '__main__':
    unittest.main()
