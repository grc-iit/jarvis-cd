"""Where JARVIS reads the site's graph and where it keeps its own state.

``JARVIS_RESOURCE_GRAPH`` names a graph the site publishes. JARVIS reads it
and never writes it or anything in its directory: it may live in a shared
admin location the person can't or shouldn't write to.

JARVIS keeps its own state in its site home:

* ``JARVIS_SITE_HOME`` when set, with or without a site graph;
* else, when a site graph is named, ``~/.ppi-jarvis/site``: one home shared
  by every JARVIS root of this person (a relay's per-job roots included);
* else the JARVIS root (``JARVIS_ROOT``, else ``~/.ppi-jarvis``), as before.

The site home holds JARVIS's own graph (``resource_graph.yaml``: the net
facts it collected, the person's placement answers, a ``rg build``), the
``site-facts/`` answers, ``skills/``, ``miniforge3/`` and the chained
``spacks/``. With neither a site graph nor ``JARVIS_SITE_HOME``, JARVIS's
own graph stays the root's ``resource_graph.yaml``.

Every reader of the graph reads the effective graph (:func:`effective_graph`),
never one file alone.

Reading overlays JARVIS's own graph on the site graph, one top-level section
at a time: a section JARVIS wrote wins, every other section comes from the
site. ``install_placement`` answers merge per phase, the newer answer
winning.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

RESOURCE_GRAPH_ENVIRONMENT = "JARVIS_RESOURCE_GRAPH"
SITE_HOME_ENVIRONMENT = "JARVIS_SITE_HOME"
GRAPH_NAME = "resource_graph.yaml"
#: ``install_placement`` answers merge per phase (newer ``answered_at`` wins)
_PER_ENTRY_SECTIONS = ("install_placement",)


def site_graph_file() -> Optional[Path]:
    """The graph the site publishes (read-only), or None when none is named."""
    value = os.environ.get(RESOURCE_GRAPH_ENVIRONMENT, "").strip()
    return Path(value).expanduser().resolve() if value else None


def _jarvis_root() -> Path:
    try:
        from jarvis_cd.core.config import Jarvis

        instance = Jarvis._instance
        if instance is not None and getattr(instance, "_initialized", False):
            return Path(instance.jarvis_root)
    except Exception:
        pass
    root = os.environ.get("JARVIS_ROOT", "").strip()
    return Path(root).expanduser() if root else Path.home() / ".ppi-jarvis"


def configured_site_home() -> Optional[Path]:
    """``JARVIS_SITE_HOME``, or None when it is not set."""
    value = os.environ.get(SITE_HOME_ENVIRONMENT, "").strip()
    return Path(value).expanduser().resolve() if value else None


def shared_site_home() -> bool:
    """Whether the site home is shared by roots (a site graph or JARVIS_SITE_HOME).

    False only when every root is its own site record.
    """
    return site_graph_file() is not None or configured_site_home() is not None


def site_home() -> Path:
    """The directory where JARVIS keeps its own per-site state (writable).

    ``JARVIS_SITE_HOME`` when set; else ``~/.ppi-jarvis/site`` with a site
    graph; else the JARVIS root, so every root is its own site record.
    """
    configured = configured_site_home()
    if configured is not None:
        return configured
    if site_graph_file() is None:
        return _jarvis_root()
    return (Path.home() / ".ppi-jarvis" / "site").resolve()


def own_graph_file() -> Path:
    """The graph file JARVIS writes; never the site graph.

    Raises ValueError when ``JARVIS_SITE_HOME`` would make JARVIS's own
    graph the site graph itself.
    """
    site = site_graph_file()
    if site is None:
        return site_home() / GRAPH_NAME
    own = site_home() / GRAPH_NAME
    if own.resolve() == site:
        raise ValueError(
            f"{SITE_HOME_ENVIRONMENT} {own.parent} holds the site graph {site} "
            f"({RESOURCE_GRAPH_ENVIRONMENT}); JARVIS reads that graph and never "
            f"writes it, so its own state needs another directory"
        )
    return own


def published_dir(name: str) -> Optional[Path]:
    """``<site graph dir>/<name>`` (read-only), or None without a site graph."""
    site = site_graph_file()
    if site is None:
        return None
    directory = site.parent / name
    try:
        if directory.resolve() == (site_home() / name).resolve():
            return None
    except (OSError, RuntimeError):
        return None
    return directory


def load_graph_file(path: Optional[Path]
                    ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """One graph file as ``(mapping, error)``.

    An absent file is ``(None, None)``. A file that exists but can't be read
    or parsed, or is not a mapping, is ``(None, {"path", "error"})`` with the
    reader's own error text.
    """
    if path is None:
        return None, None
    import json

    import yaml

    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, None
    except OSError as error:
        return None, {"path": str(path), "error": str(error)[:1024]}
    try:
        data = json.loads(text) if Path(path).suffix.lower() == ".json" else yaml.safe_load(text)
    except (ValueError, yaml.YAMLError) as error:
        return None, {"path": str(path), "error": str(error)[:1024]}
    if data is None:
        return None, None
    if not isinstance(data, dict):
        return None, {"path": str(path),
                      "error": f"the graph is a {type(data).__name__}, not a mapping"}
    return data, None


def read_graph_file(path: Optional[Path]) -> Optional[Dict[str, Any]]:
    """One graph file as a mapping; None when absent, empty or unreadable."""
    return load_graph_file(path)[0]


def merge_graphs(site: Optional[Mapping[str, Any]],
                 own: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """JARVIS's own sections over the site's (see the module docstring)."""
    merged: Dict[str, Any] = dict(site or {})
    for key, value in (own or {}).items():
        base = merged.get(key)
        if key in _PER_ENTRY_SECTIONS and isinstance(value, Mapping) and isinstance(base, Mapping):
            section = dict(base)
            for name, entry in value.items():
                current = section.get(name)
                if (isinstance(current, Mapping) and isinstance(entry, Mapping)
                        and str(current.get("answered_at") or "")
                        > str(entry.get("answered_at") or "")):
                    continue
                section[name] = entry
            merged[key] = section
        else:
            merged[key] = value
    return merged


def own_sections(site: Optional[Mapping[str, Any]],
                 graph: Mapping[str, Any]) -> Dict[str, Any]:
    """The part of ``graph`` JARVIS must keep itself: what differs from the site.

    The inverse of :func:`merge_graphs`, so ``merge_graphs(site,
    own_sections(site, graph)) == graph`` for every section ``graph`` carries.
    """
    site = site or {}
    own: Dict[str, Any] = {}
    for key, value in graph.items():
        base = site.get(key)
        if key in _PER_ENTRY_SECTIONS and isinstance(value, Mapping) and isinstance(base, Mapping):
            entries = {name: entry for name, entry in value.items() if base.get(name) != entry}
            if set(entries) - {"schema_version"}:
                if "schema_version" in value:
                    entries["schema_version"] = value["schema_version"]
                own[key] = entries
        elif key not in site or base != value:
            own[key] = value
    return own


def effective_graph() -> Dict[str, Any]:
    """The graph JARVIS acts on: its own graph overlaid on the site's."""
    return effective_graph_status()["graph"]


def effective_graph_status() -> Dict[str, Any]:
    """The effective graph with where each section came from and read errors.

    ``graph``: the merged mapping. ``section_files``: per top-level section,
    the file it comes from. ``files``: the files read (own first).
    ``errors``: ``{"path", "error"}`` for each file that exists but could not
    be read. Raises ValueError as :func:`own_graph_file` does.
    """
    own_file = own_graph_file()
    site_file = site_graph_file()
    own, own_error = load_graph_file(own_file)
    site, site_error = load_graph_file(site_file) if site_file is not None else (None, None)
    section_files: Dict[str, str] = {key: str(site_file) for key in (site or {})}
    section_files.update({key: str(own_file) for key in (own or {})})
    return {
        "graph": merge_graphs(site, own) if site_file is not None else dict(own or {}),
        "section_files": section_files,
        "files": [str(own_file)] + ([str(site_file)] if site_file is not None else []),
        "errors": [error for error in (own_error, site_error) if error],
    }


def graph_sources() -> Dict[str, Any]:
    """Both files, for records: which one is read-only."""
    site = site_graph_file()
    return {
        "site_graph": str(site) if site is not None else None,
        "site_graph_read_only": site is not None,
        "own_graph": str(own_graph_file()),
        "site_home": str(site_home()),
    }
