"""Site skills: notes for CLIO (or a person) about the site.

A skill is one Markdown document about the site (how its queues, MPI or
filesystems behave). JARVIS keeps the skills it is given in its site home,
beside its own graph, so every JARVIS root that shares the home shares them:

``<site home>/skills/<name>.md``   the document
``<site home>/skills/index.json``  ``jarvis.site-skills.v1``

When ``JARVIS_RESOURCE_GRAPH`` names a graph the site publishes, skills the
site publishes beside it (``<dir of that graph>/skills``, same layout) are
read too, with ``source: site``. JARVIS never writes there; a skill of
JARVIS's own with the same name wins.

The index records each skill's description, sha256, size, when and by whom
it was set. Writes are atomic and serialized by a flock on the index.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from jarvis_cd.core.issues import JarvisIssueError, make_jarvis_issue

SKILLS_SCHEMA = "jarvis.site-skills.v1"
SKILL_SCHEMA = "jarvis.site-skill.v1"
MAX_SKILL_BYTES = 256 * 1024
_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def skills_dir(resource_graph_file: Path) -> Path:
    """``<dir of JARVIS's own graph>/skills``."""
    return Path(resource_graph_file).expanduser().resolve().parent / "skills"


def _refuse(kind: str, message: str, field: str = "skill name", **details: Any) -> JarvisIssueError:
    return JarvisIssueError([
        make_jarvis_issue(kind=kind, subject="site skill", message=message,
                          identity={k: str(v) for k, v in details.items()},
                          field=field, details=details)
    ])


def _valid_name(name: str) -> str:
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise _refuse("skill_name_invalid", f"skill name {name!r} must match "
                      f"{_NAME.pattern}", name=name)
    return name


def _write_atomic(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(temporary, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


class SiteSkills:
    """The skills directory next to JARVIS's own graph, over the site's."""

    def __init__(self, resource_graph_file: Path, *, site: bool = False):
        from jarvis_cd.util.site_state import published_dir, site_graph_file

        self.graph = Path(resource_graph_file).expanduser().resolve()
        self.directory = skills_dir(self.graph)
        self.index_path = self.directory / "index.json"
        self.site = site
        self.site_graph = site_graph_file() if site else None
        self.published = published_dir("skills") if site else None

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        self.directory.mkdir(parents=True, exist_ok=True)
        with open(self.directory / ".lock", "a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _index(self, directory: Optional[Path] = None) -> Dict[str, Any]:
        path = (directory / "index.json") if directory is not None else self.index_path
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            loaded = {}
        skills = loaded.get("skills") if isinstance(loaded, dict) else None
        return {"schema_version": SKILLS_SCHEMA,
                "skills": dict(skills) if isinstance(skills, dict) else {}}

    def _entry(self, name: str, meta: Dict[str, Any],
               directory: Optional[Path] = None) -> Dict[str, Any]:
        published = directory is not None and directory != self.directory
        path = (directory or self.directory) / f"{name}.md"
        entry = {"name": name, "path": str(path), "present": path.is_file(),
                 "source": "site" if published else "jarvis"}
        entry.update({key: meta.get(key) for key in
                      ("description", "sha256", "bytes", "updated_at", "updated_by")})
        return entry

    def _names(self, directory: Optional[Path]) -> Dict[str, Dict[str, Any]]:
        if directory is None:
            return {}
        index = self._index(directory)
        names = set(index["skills"])
        if directory.is_dir():
            names.update(path.stem for path in directory.glob("*.md")
                         if _NAME.fullmatch(path.stem))
        return {name: index["skills"].get(name) or {} for name in names}

    def _where(self, name: str) -> Optional[Path]:
        """The directory whose ``<name>.md`` applies (JARVIS's own first)."""
        for directory in (self.directory, self.published):
            if directory is not None and (directory / f"{name}.md").is_file():
                return directory
        return None

    def listing(self) -> Dict[str, Any]:
        entries = {name: self._entry(name, meta, self.published)
                   for name, meta in self._names(self.published).items()}
        entries.update({name: self._entry(name, meta)
                        for name, meta in self._names(self.directory).items()})
        return {
            "schema_version": SKILLS_SCHEMA,
            "graph": str(self.graph),
            "site_graph": self.site,
            "site_graph_file": str(self.site_graph) if self.site_graph else None,
            "directory": str(self.directory),
            "published_directory": str(self.published) if self.published else None,
            "skills": [entries[name] for name in sorted(entries)],
        }

    def show(self, name: str) -> Dict[str, Any]:
        name = _valid_name(name)
        directory = self._where(name)
        if directory is None:
            raise _refuse("skill_not_found", f"no site skill {name} in {self.directory}"
                          + (f" or {self.published}" if self.published else ""),
                          name=name)
        path = directory / f"{name}.md"
        data = path.read_bytes()[:MAX_SKILL_BYTES]
        entry = self._entry(name, self._index(directory)["skills"].get(name) or {},
                            directory)
        entry.update({"schema_version": SKILL_SCHEMA,
                      "content": data.decode("utf-8", errors="replace"),
                      "sha256_actual": "sha256:" + hashlib.sha256(data).hexdigest()})
        return entry

    def set(self, name: str, content: str, *, description: Optional[str] = None,
            set_by: Optional[str] = None) -> Dict[str, Any]:
        name = _valid_name(name)
        data = content.encode("utf-8")
        if len(data) > MAX_SKILL_BYTES:
            raise _refuse("skill_too_large", f"skill {name} is {len(data)} bytes; the "
                          f"limit is {MAX_SKILL_BYTES}", field="skill content",
                          name=name, bytes=len(data))
        with self._locked():
            index = self._index()
            previous = index["skills"].get(name) or {}
            _write_atomic(self.directory / f"{name}.md", data)
            meta = {
                "description": description if description is not None
                else previous.get("description"),
                "sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
                "updated_at": _now(),
                "updated_by": set_by or os.environ.get("USER") or None,
            }
            if previous.get("sha256") and previous.get("sha256") != meta["sha256"]:
                meta["previous_sha256"] = previous.get("sha256")
            index["skills"][name] = meta
            _write_atomic(self.index_path, (json.dumps(index, indent=2, sort_keys=True)
                                            + "\n").encode("utf-8"))
        return {**self._entry(name, meta), "action": "set",
                "previous_sha256": meta.get("previous_sha256")}

    def remove(self, name: str) -> Dict[str, Any]:
        name = _valid_name(name)
        if (self.published is not None and (self.published / f"{name}.md").is_file()
                and not (self.directory / f"{name}.md").is_file()
                and name not in self._index()["skills"]):
            raise _refuse("skill_published", f"site skill {name} is the one the site "
                          f"publishes in {self.published}, which JARVIS only reads",
                          name=name, published_directory=str(self.published))
        with self._locked():
            index = self._index()
            path = self.directory / f"{name}.md"
            existed = path.is_file() or name in index["skills"]
            path.unlink(missing_ok=True)
            index["skills"].pop(name, None)
            _write_atomic(self.index_path, (json.dumps(index, indent=2, sort_keys=True)
                                            + "\n").encode("utf-8"))
        return {"name": name, "action": "removed" if existed else "absent",
                "directory": str(self.directory),
                **({"published_still_applies": str(self.published / f"{name}.md")}
                   if self.published is not None
                   and (self.published / f"{name}.md").is_file() else {})}

    def summary(self) -> List[Dict[str, Any]]:
        """Names, descriptions, paths and source only (for ``sched info``)."""
        return [{key: entry[key] for key in ("name", "description", "path", "sha256",
                                             "source")}
                for entry in self.listing()["skills"]]
