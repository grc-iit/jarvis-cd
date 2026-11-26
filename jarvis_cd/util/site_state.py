"""Where JARVIS reads the site's graph and where it keeps its own state.

``JARVIS_RESOURCE_GRAPH`` names a graph the site publishes. JARVIS reads it
and never writes it or anything in its directory: it may live in a shared
admin location the person can't or shouldn't write to.

JARVIS keeps its own state in its site home:

* ``JARVIS_SITE_HOME`` when set (with a site graph);
* else, when a site graph is named, ``~/.ppi-jarvis/site``: one home shared
  by every JARVIS root of this person (a relay's per-job roots included);
* else the JARVIS root (``JARVIS_ROOT``, else ``~/.ppi-jarvis``), as before.

The site home holds JARVIS's own graph (``resource_graph.yaml``: the net
facts it collected, the person's placement answers, a ``rg build``), the
``site-facts/`` answers, ``skills/``, ``miniforge3/`` and the chained
``spacks/``. Without a site graph, JARVIS's own graph stays the root's
``resource_graph.yaml``.

Reading overlays JARVIS's own graph on the site graph, one top-level section
at a time: a section JARVIS wrote wins, every other section comes from the
site. ``install_placement`` answers merge per phase, the newer answer
winning.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

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


def site_home() -> Path:
    """The directory where JARVIS keeps its own per-site state (writable).

    ``JARVIS_SITE_HOME`` applies with a site graph only: without one, every
    root is its own site record, as before.
    """
    if site_graph_file() is None:
        return _jarvis_root()
    configured = os.environ.get(SITE_HOME_ENVIRONMENT, "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / ".ppi-jarvis" / "site").resolve()


def own_graph_file() -> Path:
    """The graph file JARVIS writes; never the site graph.

    Raises ValueError when ``JARVIS_SITE_HOME`` would make JARVIS's own
    graph the site graph itself.
    """
    site = site_graph_file()
    if site is None:
        return _jarvis_root() / GRAPH_NAME
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


def read_graph_file(path: Optional[Path]) -> Optional[Dict[str, Any]]:
    """One graph file as a mapping; None when absent or unreadable."""
    if path is None:
        return None
    import json

    import yaml

    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(text) if Path(path).suffix.lower() == ".json" else yaml.safe_load(text)
    except (ValueError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None


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
    own = read_graph_file(own_graph_file())
    site_file = site_graph_file()
    if site_file is None:
        return dict(own or {})
    return merge_graphs(read_graph_file(site_file), own)


def graph_sources() -> Dict[str, Any]:
    """Both files, for records: which one is read-only."""
    site = site_graph_file()
    return {
        "site_graph": str(site) if site is not None else None,
        "site_graph_read_only": site is not None,
        "own_graph": str(own_graph_file()),
        "site_home": str(site_home()),
    }
