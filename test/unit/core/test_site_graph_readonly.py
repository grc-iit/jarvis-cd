"""A site-named graph (JARVIS_RESOURCE_GRAPH) is read-only input.

JARVIS reads the graph the site publishes, and the skills and site-fact
answers beside it, but writes its own state (net facts, placement answers,
saved graphs, skills, site-fact answers, Miniforge, chained Spacks) only in
its site home (JARVIS_SITE_HOME, else ~/.ppi-jarvis/site).
"""

from __future__ import annotations

import stat
from pathlib import Path
from typing import Dict, Tuple

import pytest
import yaml

from jarvis_cd.core import install_placement as placement
from jarvis_cd.core.config import Jarvis
from jarvis_cd.core.issues import JarvisIssueError
from jarvis_cd.core.method_install import site_home as method_site_home
from jarvis_cd.core.site_facts import SiteFactStore
from jarvis_cd.core.site_skills import SiteSkills
from jarvis_cd.core.spack_install import chained_spack_root
from jarvis_cd.util import site_state
from jarvis_cd.util.network import persist_net_section

OLD = "2026-01-01T00:00:00Z"
NEW = "2026-06-01T00:00:00Z"


def _answer(constraint: str, answered_at: str, phase: str = "check") -> dict:
    return {"kind": "install_placement", "subject": f"install_placement.{phase}",
            "issue_id": "sha256:" + "c" * 64, "constraint": constraint,
            "answered_at": answered_at, "answered_by": "person"}


def _snapshot(directory: Path) -> Dict[str, Tuple[bytes, int]]:
    return {str(path.relative_to(directory)): (path.read_bytes() if path.is_file() else b"",
                                               path.stat().st_mtime_ns)
            for path in sorted(directory.rglob("*"))}


@pytest.fixture
def site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A published site graph with skills and site-fact answers beside it."""
    directory = tmp_path / "published"
    (directory / "skills").mkdir(parents=True)
    (directory / "site-facts").mkdir()
    graph = directory / "resource_graph.yaml"
    graph.write_text(yaml.safe_dump({
        "storage": {"node1": [{"mount": "/site/scratch"}]},
        "net": {"selected": {"interface": "ib0"}, "collected_on": ["login"]},
        "install_placement": {"schema_version": placement.GRAPH_SCHEMA,
                              "build": _answer("compute_job", OLD, "build")},
    }))
    (directory / "skills" / "queues.md").write_text("# queues\n")
    (directory / "skills" / "index.json").write_text(
        '{"schema_version": "jarvis.site-skills.v1", "skills": '
        '{"queues": {"description": "site queues"}}}')
    (directory / "site-facts" / "answers.yaml").write_text(yaml.safe_dump(
        {"schema_version": "x", "facts": {"scratch": {"value": "/site/scratch"}}}))
    home = tmp_path / "home"
    monkeypatch.setenv("JARVIS_RESOURCE_GRAPH", str(graph))
    monkeypatch.setenv("JARVIS_SITE_HOME", str(home))
    monkeypatch.setenv("JARVIS_ROOT", str(tmp_path / "root"))
    Jarvis._instance = None
    before = _snapshot(directory)
    modes = {path: path.stat().st_mode for path in [directory, *directory.rglob("*")]
             if path.is_dir()}
    for path in modes:
        path.chmod(0o555)
    try:
        yield directory, graph, home.resolve()
    finally:
        for path, mode in modes.items():
            path.chmod(stat.S_IMODE(mode))
        Jarvis._instance = None
    assert _snapshot(directory) == before, "the site graph's directory changed"


def test_site_home_and_own_graph(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "root"
    monkeypatch.setenv("JARVIS_ROOT", str(root))
    monkeypatch.delenv("JARVIS_SITE_HOME", raising=False)
    monkeypatch.delenv("JARVIS_RESOURCE_GRAPH", raising=False)
    Jarvis._instance = None
    # with neither, the root is the site record, as before
    assert site_state.site_home() == root
    assert site_state.own_graph_file() == root / "resource_graph.yaml"
    assert not site_state.shared_site_home()

    # JARVIS_SITE_HOME applies without a site graph too
    home = tmp_path / "home"
    monkeypatch.setenv("JARVIS_SITE_HOME", str(home))
    assert site_state.site_home() == home.resolve()
    assert site_state.own_graph_file() == home.resolve() / "resource_graph.yaml"
    assert site_state.shared_site_home()
    jarvis = Jarvis.get_instance()
    assert jarvis.resource_graph_file == home.resolve() / "resource_graph.yaml"
    assert method_site_home() == home.resolve()
    saved = persist_net_section({"selected": {"interface": "eth0"}})
    assert Path(saved["path"]) == home.resolve() / "resource_graph.yaml", saved
    Jarvis._instance = None

    graph = tmp_path / "published" / "resource_graph.yaml"
    monkeypatch.setenv("JARVIS_RESOURCE_GRAPH", str(graph))
    monkeypatch.delenv("JARVIS_SITE_HOME")
    assert site_state.site_home() == (Path.home() / ".ppi-jarvis" / "site").resolve()

    monkeypatch.setenv("JARVIS_SITE_HOME", str(graph.parent))
    with pytest.raises(ValueError, match="never\\s+writes it"):
        site_state.own_graph_file()


def test_merge_and_own_sections_round_trip():
    site = {"storage": {"a": 1}, "net": {"selected": "ib0"},
            "install_placement": {"check": _answer("login_limited", NEW),
                                  "build": _answer("compute_job", OLD, "build")}}
    own = {"net": {"selected": "eth0"},
           "install_placement": {"check": _answer("compute_job", OLD),
                                 "build": _answer("login_limited", NEW, "build")}}
    merged = site_state.merge_graphs(site, own)
    assert merged["storage"] == {"a": 1}
    assert merged["net"] == {"selected": "eth0"}
    # per phase, the newer answer wins whichever file holds it
    assert merged["install_placement"]["check"]["constraint"] == "login_limited"
    assert merged["install_placement"]["build"]["constraint"] == "login_limited"

    kept = site_state.own_sections(site, merged)
    assert "storage" not in kept
    assert set(kept["install_placement"]) == {"build"}
    assert site_state.merge_graphs(site, kept) == merged


def test_writers_keep_out_of_the_site_graph_directory(site):
    directory, graph, home = site
    jarvis = Jarvis.get_instance()
    assert jarvis.site_graph_file == graph.resolve()
    assert jarvis.resource_graph_file == home / "resource_graph.yaml"

    saved = persist_net_section({"selected": {"interface": "eth0"}})
    assert saved["persisted"], saved
    assert Path(saved["path"]) == home / "resource_graph.yaml"

    placed = placement.persist_graph_answer("check", _answer("compute_job", NEW))
    assert placed["persisted"], placed
    answers = placement.graph_answers()
    assert answers["check"]["constraint"] == "compute_job"
    assert answers["build"]["site_graph"] is True

    effective = jarvis.load_resource_graph()
    assert effective["storage"] == {"node1": [{"mount": "/site/scratch"}]}
    assert effective["net"]["selected"] == {"interface": "eth0"}
    effective["fs"] = [{"mount": "/mine"}]
    jarvis.save_resource_graph(effective)
    own = yaml.safe_load((home / "resource_graph.yaml").read_text())
    # only what differs from the site is JARVIS's to keep
    assert "storage" not in own and own["fs"] == [{"mount": "/mine"}]
    assert set(own["install_placement"]) == {"schema_version", "check"}
    assert jarvis.load_resource_graph()["storage"] == effective["storage"]

    skills = SiteSkills(jarvis.resource_graph_file, site=True)
    skills.set("mine", "# mine\n", description="own")
    listed = {entry["name"]: entry["source"] for entry in skills.listing()["skills"]}
    assert listed == {"queues": "site", "mine": "jarvis"}
    assert skills.show("queues")["content"] == "# queues\n"
    with pytest.raises(JarvisIssueError):
        skills.remove("queues")

    facts = SiteFactStore(jarvis.resource_graph_file)
    assert facts.directory == home / "site-facts"
    facts.record_answer("launcher", {"value": "srun"})
    answers = facts.answers()
    assert answers["scratch"]["published_in"] == str(directory / "site-facts" / "answers.yaml")
    assert answers["launcher"]["value"] == "srun"

    assert method_site_home() == home
    assert home in chained_spack_root(Path("/opt/site/spack")).parents
    leftovers = [path.name for path in directory.iterdir() if path.name.startswith(".")]
    assert leftovers == []


def test_a_newer_site_answer_is_not_overwritten_by_an_older_one(site):
    _directory, _graph, home = site
    result = placement.persist_graph_answer(
        "build", _answer("login_limited", "2025-01-01T00:00:00Z", "build"))
    assert not result["persisted"]
    assert placement.graph_answers()["build"]["constraint"] == "compute_job"
    assert not (home / "resource_graph.yaml").exists()


def test_site_home_equal_to_the_graph_directory_is_refused(site, monkeypatch):
    directory, _graph, _home = site
    monkeypatch.setenv("JARVIS_SITE_HOME", str(directory))
    result = placement.persist_graph_answer("check", _answer("compute_job", NEW))
    assert not result["persisted"] and "never" in result["error"]
    assert not persist_net_section({"selected": {}})["persisted"]

LEGACY_NET = [
    {"domain": "enp47s0np0", "fabric": "172.25.0.0/16", "provider": "tcp;ofi_rxm",
     "shared": True, "speed": 42949672960},
    {"domain": "eno1", "fabric": "172.20.0.0/16", "provider": "tcp;ofi_rxm",
     "shared": True, "speed": 42949672960},
    {"domain": "lo", "fabric": "127.0.0.0/8", "provider": "tcp;ofi_rxm",
     "shared": False, "speed": 42949672960},
]


@pytest.fixture
def named_graph(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A writable site-named graph; the test fills it."""
    graph = tmp_path / "site" / "resource_graph.yaml"
    graph.parent.mkdir()
    monkeypatch.setenv("JARVIS_RESOURCE_GRAPH", str(graph))
    monkeypatch.setenv("JARVIS_SITE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_ROOT", str(tmp_path / "root"))
    Jarvis._instance = None
    yield graph
    Jarvis._instance = None


def test_graph_copies_read_the_site_graphs_software(named_graph: Path):
    from jarvis_cd.core.site_copies import graph_copies

    named_graph.write_text(yaml.safe_dump({"fs": [], "software": [
        {"name": "ior", "version": "3.3.0", "modules": ["ior/3.3.0"]}]}))
    searched: list = []
    copies = graph_copies("ior", searched)
    assert [copy["version"] for copy in copies] == ["3.3.0"]
    assert copies[0]["graph"] == str(named_graph.resolve())
    assert "none readable" not in " ".join(searched)

    named_graph.write_text("software: [unclosed\n")
    searched = []
    assert graph_copies("ior", searched) == []
    assert any("unreadable" in line and str(named_graph.resolve()) in line
               for line in searched), searched


def test_a_legacy_list_net_is_read_not_missing(named_graph: Path):
    from jarvis_cd.core.resource_graph import ResourceGraphManager
    from jarvis_cd.util.network import load_net_section, net_collect_reasons, net_section_status
    from jarvis_cd.util.resource_graph import ResourceGraph

    named_graph.write_text(yaml.safe_dump({"fs": [], "net": LEGACY_NET}))
    report = ResourceGraphManager().net_report()
    assert report["present"] is True and report["form"] == "legacy_list"
    assert [issue["kind"] for issue in report["issues"]] == ["resource_graph_net_legacy"]
    assert report["issues"][0]["blocking"] is False
    fabrics = {entry["fabric"]: entry for entry in report["legacy"]["fabrics"]}
    assert fabrics["172.25.0.0/16"]["domains"] == ["enp47s0np0"]
    assert report["legacy"]["entries"] == LEGACY_NET

    # the launch has no per-node facts from it, and says why it collects
    assert load_net_section() is None
    status = net_section_status()
    assert status["form"] == "legacy_list"
    assert net_collect_reasons(None, launch_nodes=2, form=status["form"]) == ["net_legacy_form"]

    graph = ResourceGraph()
    graph.load_from_dict({"net": LEGACY_NET}, hostname="g")
    assert graph.to_dict()["net"] == LEGACY_NET  # a save keeps it as written


def test_an_unreadable_net_is_reported_with_its_error(named_graph: Path):
    from jarvis_cd.core.resource_graph import ResourceGraphManager

    named_graph.write_text(yaml.safe_dump({"fs": [], "net": "eth0"}))
    report = ResourceGraphManager().net_report()
    assert report["form"] == "unreadable"
    issue, = report["issues"]
    assert issue["kind"] == "resource_graph_net_unreadable"
    assert "net is a str" in issue["details"]["error"]

    Jarvis._instance = None
    named_graph.write_text("net: {selected: [\n")
    report = ResourceGraphManager().net_report()
    assert report["form"] == "unreadable"
    issue, = report["issues"]
    assert issue["kind"] == "resource_graph_net_unreadable"
    assert issue["details"]["graph_errors"][0]["path"] == str(named_graph.resolve())