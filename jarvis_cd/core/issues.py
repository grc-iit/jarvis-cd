"""Typed, machine-readable JARVIS issues (``jarvis.issue.v1``).

Every refusal or gap JARVIS reports to a caller (a relay, a UI, a person) is
one issue document with a stable ``issue_id``. The id digests the kind, the
subject and the identity fields, so the same situation keeps its id and a
changed situation gets a new one. Install questions use the richer
``jarvis.install-issue.v1`` from :mod:`jarvis_cd.core.spack_install`.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional, Sequence

ISSUE_SCHEMA = "jarvis.issue.v1"

#: CLI exit code for a typed refusal (3 = install decision, 4 = build refused)
EXIT_REFUSED = 5


def make_jarvis_issue(
    *,
    kind: str,
    subject: str,
    message: str,
    identity: Optional[Mapping[str, Any]] = None,
    field: Optional[str] = None,
    details: Optional[Mapping[str, Any]] = None,
    candidates: Sequence[Mapping[str, Any]] = (),
    remedy: Optional[str] = None,
) -> Dict[str, Any]:
    """Build one ``jarvis.issue.v1`` document.

    ``field`` names the configuration a person can change to resolve it (for
    example ``resource_graph.net.override`` or ``pkgs[1].nprocs``).
    """
    digest = hashlib.sha256(
        json.dumps(
            {"kind": kind, "subject": subject, "identity": dict(identity or {})},
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    issue: Dict[str, Any] = {
        "schema": ISSUE_SCHEMA,
        "issue_id": f"sha256:{digest}",
        "kind": kind,
        "subject": subject,
        "message": message,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    if field is not None:
        issue["field"] = field
    if details:
        issue["details"] = dict(details)
    if candidates:
        issue["candidates"] = [dict(item) for item in candidates]
    if remedy:
        issue["remedy"] = remedy
    return issue


class JarvisIssueError(RuntimeError):
    """A refusal that carries typed issues; the CLI exits ``exit_code``."""

    def __init__(
        self,
        issues: Sequence[Mapping[str, Any]],
        *,
        exit_code: int = EXIT_REFUSED,
        document: Optional[Mapping[str, Any]] = None,
    ):
        self.issues = [dict(issue) for issue in issues]
        self.exit_code = exit_code
        self.document = dict(document or {})
        super().__init__("; ".join(str(issue.get("message")) for issue in self.issues))

    def to_dict(self) -> Dict[str, Any]:
        document = {
            "schema_version": "jarvis.refusal.v1",
            "refused": True,
            "exit_code": self.exit_code,
            "issues": self.issues,
        }
        document.update(self.document)
        return document
