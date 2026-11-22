"""Per-component state over time: ``<execution_root>/components.jsonl``.

One JSON line per transition, schema ``jarvis.job.v1``::

    {"schema_version": "jarvis.job.v1", "component": "xcompact3d",
     "package": "builtin.xcompact3d", "state": "running",
     "hosts": ["ares-comp-01", ...], "at": "2025-...Z", "reason": null}

States are ``waiting``, ``starting``, ``running``, ``done``, ``failed`` and
``cancelled``; a component's latest line is its state. The first terminal
line wins: later lines for that component are dropped, so a reader can stop
watching a component once it is terminal. When the execution record turns
terminal, every component not yet terminal is closed out from the record's
state (``completed`` -> done, ``canceled`` -> cancelled, ``failed`` ->
failed, or cancelled for a component that never started).

Writers append one line with a single ``O_APPEND`` write. They never raise:
a state line must not fail the run it describes.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

COMPONENT_STATES_SCHEMA = "jarvis.job.v1"
COMPONENT_STATES_FILE = "components.jsonl"
COMPONENT_STATES = ("waiting", "starting", "running", "done", "failed", "cancelled")
TERMINAL_COMPONENT_STATES = frozenset({"done", "failed", "cancelled"})

_FROM_EXECUTION = {"completed": "done", "canceled": "cancelled", "failed": "failed"}


def component_states_path(execution_root: Any) -> Path:
    return Path(execution_root) / COMPONENT_STATES_FILE


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def read_component_states(execution_root: Any) -> List[Dict[str, Any]]:
    """Every well-formed line, in file order."""
    path = component_states_path(execution_root)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    lines = []
    for raw in text.splitlines():
        try:
            line = json.loads(raw)
        except ValueError:
            continue
        if isinstance(line, dict) and line.get("schema_version") == COMPONENT_STATES_SCHEMA:
            lines.append(line)
    return lines


def latest_component_states(execution_root: Any) -> Dict[str, Dict[str, Any]]:
    """Each component's current line (the first terminal one, once terminal)."""
    latest: Dict[str, Dict[str, Any]] = {}
    for line in read_component_states(execution_root):
        name = str(line.get("component"))
        if latest.get(name, {}).get("state") in TERMINAL_COMPONENT_STATES:
            continue
        latest[name] = line
    return latest


def append_component_state(
    execution_root: Any,
    component: str,
    state: str,
    *,
    package: Optional[str] = None,
    hosts: Optional[Iterable[str]] = None,
    reason: Optional[str] = None,
) -> bool:
    """Append one transition; False when skipped (terminal already) or not written."""
    if state not in COMPONENT_STATES:
        raise ValueError(f"unknown component state: {state}")
    if execution_root is None:
        return False
    try:
        previous = latest_component_states(execution_root).get(str(component))
        if previous and previous.get("state") in TERMINAL_COMPONENT_STATES:
            return False
        if hosts is None and previous:
            hosts = previous.get("hosts")
        if package is None and previous:
            package = previous.get("package")
        line = {
            "schema_version": COMPONENT_STATES_SCHEMA,
            "component": str(component),
            "package": package,
            "state": state,
            "hosts": list(hosts) if hosts is not None else None,
            "at": _utc_now(),
            "reason": (str(reason)[:512] if reason else None),
        }
        data = (json.dumps(line, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        path = component_states_path(execution_root)
        if not path.parent.is_dir():
            return False
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        return True
    except Exception as error:  # a state line must never fail a run
        try:
            from jarvis_cd.util.logger import logger

            logger.warning(f"component state not recorded ({component} {state}): {error}")
        except Exception:
            pass
        return False


def close_component_states(execution_root: Any, execution_state: str,
                           reason: Optional[str] = None) -> List[str]:
    """Close out every non-terminal component from the execution's final state."""
    target = _FROM_EXECUTION.get(execution_state)
    if target is None:
        return []
    closed = []
    for name, line in latest_component_states(execution_root).items():
        if line.get("state") in TERMINAL_COMPONENT_STATES:
            continue
        state = target
        why = reason or f"execution {execution_state}"
        if target == "failed" and line.get("state") == "waiting":
            state, why = "cancelled", f"not started: execution {execution_state}"
        if append_component_state(execution_root, name, state, reason=why):
            closed.append(name)
    return closed
