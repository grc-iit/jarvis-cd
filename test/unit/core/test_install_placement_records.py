"""
Install placement recommendations follow the site's recorded answers.

A recorded constraint (this home's decisions, then the resource graph's
``install_placement`` section) is recommended with its basis; the other
phase's answer informs it where cost orders the phases; otherwise cost and
load decide. Confirmed answers are copied into the graph, survive a rebuild
of the graph object, and leave it when the person forgets them.
"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

import yaml

from jarvis_cd.core import install_placement as placement
from jarvis_cd.core.spack_install import Decisions, forget_decisions
from jarvis_cd.util.resource_graph import ResourceGraph

IDLE = {"defer": False, "free_cores": 16}
BUSY = {"defer": True, "defer_reasons": ["load 40 on 32 cores"]}
SHORT = {"class": "short", "human": "1m", "seconds": 60}
LONG = {"class": "long", "human": "4h", "seconds": 14400}


def _entry(constraint, subject="install_placement.check", **extra):
    return {"kind": "install_placement", "subject": subject,
            "issue_id": "sha256:" + "a" * 64, "constraint": constraint,
            "answered_at": "2026-11-25T07:23:00Z", "answered_by": "person", **extra}


class TestRecommend(unittest.TestCase):

    def test_heuristic_without_records(self):
        choice, _reason, basis = placement.recommend("check", "short", IDLE, {}, SHORT)
        self.assertEqual(choice, placement.LOGIN_LIMITED)
        self.assertEqual(basis["source"], "heuristic")

    def test_recorded_compute_job_beats_short_check(self):
        site = {"install_placement.check": _entry("compute_job", basis="site docs")}
        choice, reason, basis = placement.recommend("check", "short", IDLE, site, SHORT)
        self.assertEqual(choice, placement.COMPUTE_JOB)
        self.assertEqual(basis["source"], "site_decision")
        self.assertEqual(basis["answered_by"], "person")
        self.assertIn("site docs", reason)

    def test_graph_answer_when_home_has_none(self):
        graph = {"check": {**_entry("compute_job"), "graph": "/site/resource_graph.yaml"}}
        choice, reason, basis = placement.recommend("check", "short", IDLE, {}, SHORT,
                                                    graph)
        self.assertEqual(choice, placement.COMPUTE_JOB)
        self.assertEqual(basis["source"], "resource_graph")
        self.assertIn("/site/resource_graph.yaml", reason)

    def test_home_beats_graph(self):
        site = {"install_placement.check": _entry("login_limited")}
        graph = {"check": _entry("compute_job")}
        choice, _reason, basis = placement.recommend("check", "long", IDLE, site, LONG,
                                                     graph)
        self.assertEqual(choice, placement.LOGIN_LIMITED)
        self.assertEqual(basis["source"], "site_decision")

    def test_ask_each_time_is_not_a_constraint(self):
        site = {"install_placement.check": _entry("ask_each_time")}
        graph = {"check": _entry("compute_job")}
        choice, _reason, basis = placement.recommend("check", "short", IDLE, site, SHORT,
                                                     graph)
        self.assertEqual(choice, placement.COMPUTE_JOB)
        self.assertEqual(basis["source"], "resource_graph")

    def test_check_compute_job_implies_build_compute_job(self):
        site = {"install_placement.check": _entry("compute_job")}
        choice, _reason, basis = placement.recommend("build", "short", IDLE, site, SHORT)
        self.assertEqual(choice, placement.COMPUTE_JOB)
        self.assertEqual(basis["inferred_for"], "build")

    def test_build_login_limited_implies_check_login_limited(self):
        site = {"install_placement.build": _entry("login_limited",
                                                  subject="install_placement.build")}
        choice, _reason, basis = placement.recommend("check", "long", IDLE, site, LONG)
        self.assertEqual(choice, placement.LOGIN_LIMITED)
        self.assertEqual(basis["inferred_for"], "check")

    def test_check_login_limited_does_not_imply_build(self):
        site = {"install_placement.check": _entry("login_limited")}
        choice, _reason, basis = placement.recommend("build", "short", IDLE, site, SHORT)
        self.assertEqual(choice, placement.COMPUTE_JOB)
        self.assertEqual(basis["source"], "heuristic")

    def test_busy_host_keeps_recorded_login_limited_and_says_so(self):
        site = {"install_placement.check": _entry("login_limited")}
        choice, reason, _basis = placement.recommend("check", "short", BUSY, site, SHORT)
        self.assertEqual(choice, placement.LOGIN_LIMITED)
        self.assertIn("busy", reason)

    def test_busy_host_without_records(self):
        choice, _reason, _basis = placement.recommend("check", "short", BUSY, {}, SHORT)
        self.assertEqual(choice, placement.COMPUTE_JOB)

    def test_legacy_per_class_entry_counts(self):
        site = {"install_placement.build.short": _entry(
            "login_limited", subject="install_placement.build.short")}
        choice, _reason, basis = placement.recommend("build", "short", IDLE, site, SHORT)
        self.assertEqual(choice, placement.LOGIN_LIMITED)
        self.assertEqual(basis["subject"], "install_placement.build.short")


class TestGraphAnswers(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        # the graph the site publishes (read-only) and JARVIS's own, in its home
        self.site = self.tmp / "resource_graph.yaml"
        self.site.write_text(yaml.safe_dump({"net": {"selected": {}},
                                             "storage": {}}))
        self.graph = self.tmp / "home" / "resource_graph.yaml"
        self._env = {key: os.environ.get(key)
                     for key in ("JARVIS_RESOURCE_GRAPH", "JARVIS_SITE_HOME")}
        os.environ["JARVIS_RESOURCE_GRAPH"] = str(self.site)
        os.environ["JARVIS_SITE_HOME"] = str(self.graph.parent)

    def tearDown(self):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_persist_read_and_keep_other_sections(self):
        before = self.site.read_bytes()
        result = placement.persist_graph_answer(
            "check", _entry("compute_job", basis="docs"), recorded_in=self.tmp / "d.yaml")
        self.assertTrue(result["persisted"])
        self.assertEqual(self.site.read_bytes(), before)
        data = yaml.safe_load(self.graph.read_text())
        self.assertEqual(data["install_placement"]["schema_version"],
                         placement.GRAPH_SCHEMA)
        answers = placement.graph_answers()
        self.assertEqual(answers["check"]["constraint"], "compute_job")
        self.assertEqual(answers["check"]["basis"], "docs")

    def test_ask_each_time_and_older_answers_are_not_written(self):
        self.assertFalse(placement.persist_graph_answer(
            "check", _entry("ask_each_time"))["persisted"])
        placement.persist_graph_answer("check", _entry("compute_job"))
        older = {**_entry("login_limited"), "answered_at": "2020-01-01T00:00:00Z"}
        self.assertFalse(placement.persist_graph_answer("check", older)["persisted"])
        self.assertEqual(placement.graph_answers()["check"]["constraint"], "compute_job")

    def test_resource_graph_round_trips_the_section(self):
        placement.persist_graph_answer("build", _entry(
            "compute_job", subject="install_placement.build"))
        graph = ResourceGraph()
        graph.load_from_file(self.graph, quiet=True)
        self.assertEqual(graph.to_dict()["install_placement"]["build"]["constraint"],
                         "compute_job")

    def test_forget_drops_the_same_record_only(self):
        decisions_path = self.tmp / "decisions.yaml"
        issue = {"kind": "install_placement", "subject": "install_placement.check",
                 "issue_id": "sha256:" + "b" * 64, "scope": "site"}
        Decisions(decisions_path).record(issue, {"constraint": "compute_job",
                                                 "answered_by": "person"})
        entry = Decisions(decisions_path).site("install_placement.check")
        placement.persist_graph_answer("check", entry or {})
        placement.persist_graph_answer("build", _entry(
            "compute_job", subject="install_placement.build"))
        report = forget_decisions(decisions_path, ["install_placement.check"])
        self.assertEqual([item["phase"] for item in report["graph_forgotten"]], ["check"])
        answers = placement.graph_answers()
        self.assertNotIn("check", answers)
        self.assertIn("build", answers)


if __name__ == '__main__':
    unittest.main()
