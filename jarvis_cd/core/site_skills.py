"""Site skills: notes for CLIO (or a person) stored next to the site graph.

A skill is one Markdown document about the site (how its queues, MPI or
filesystems behave), kept beside the resource graph so every JARVIS root
that shares the graph (``JARVIS_RESOURCE_GRAPH``) shares its skills:

``<dir of the resource graph>/skills/<name>.md``   the document
``<dir of the resource graph>/skills/index.json``  ``jarvis.site-skills.v1``

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
    """``<dir of the resource graph>/skills``."""
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
    """The skills directory next to one resource graph file."""

    def __init__(self, resource_graph_file: Path, *, site: bool = False):
        self.graph = Path(resource_graph_file).expanduser().resolve()
        self.directory = skills_dir(self.graph)
        self.index_path = self.directory / "index.json"
        self.site = site

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        self.directory.mkdir(parents=True, exist_ok=True)
        with open(self.directory / ".lock", "a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _index(self) -> Dict[str, Any]:
        try:
            loaded = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            loaded = {}
        skills = loaded.get("skills") if isinstance(loaded, dict) else None
        return {"schema_version": SKILLS_SCHEMA,
                "skills": dict(skills) if isinstance(skills, dict) else {}}

    def _entry(self, name: str, meta: Dict[str, Any]) -> Dict[str, Any]:
        path = self.directory / f"{name}.md"
        entry = {"name": name, "path": str(path), "present": path.is_file()}
        entry.update({key: meta.get(key) for key in
                      ("description", "sha256", "bytes", "updated_at", "updated_by")})
        return entry

    def listing(self) -> Dict[str, Any]:
        index = self._index()
        names = set(index["skills"])
        if self.directory.is_dir():
            names.update(path.stem for path in self.directory.glob("*.md")
                         if _NAME.fullmatch(path.stem))
        return {
            "schema_version": SKILLS_SCHEMA,
            "graph": str(self.graph),
            "site_graph": self.site,
            "directory": str(self.directory),
            "skills": [self._entry(name, index["skills"].get(name) or {})
                       for name in sorted(names)],
        }

    def show(self, name: str) -> Dict[str, Any]:
        name = _valid_name(name)
        path = self.directory / f"{name}.md"
        if not path.is_file():
            raise _refuse("skill_not_found", f"no site skill {name} in {self.directory}",
                          name=name)
        data = path.read_bytes()[:MAX_SKILL_BYTES]
        entry = self._entry(name, self._index()["skills"].get(name) or {})
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
        with self._locked():
            index = self._index()
            path = self.directory / f"{name}.md"
            existed = path.is_file() or name in index["skills"]
            path.unlink(missing_ok=True)
            index["skills"].pop(name, None)
            _write_atomic(self.index_path, (json.dumps(index, indent=2, sort_keys=True)
                                            + "\n").encode("utf-8"))
        return {"name": name, "action": "removed" if existed else "absent",
                "directory": str(self.directory)}

    def summary(self) -> List[Dict[str, Any]]:
        """Names, descriptions and paths only (for ``sched info``)."""
        return [{key: entry[key] for key in ("name", "description", "path", "sha256")}
                for entry in self.listing()["skills"]]
