"""Find-first Spack installation for Jarvis-CD pipelines.

The planner resolves each package's ``install_query`` with ``spack spec -I``
before building anything. Nodes that Spack already has (local ``[+]``,
upstream ``[^]`` or external ``[e]``) are reused; only missing nodes are
built. Choices JARVIS must not make silently (several installed MPIs, a
variant conflict) become machine-readable issues in ``issues.jsonl`` and the
run ends with :data:`DECISION_NEEDED_EXIT_CODE`, so a caller can ask a person
and re-run with an ``answers.json``.

Files, all under one install directory (``<execution>/install`` inside an
execution, else ``<pipeline shared>/install``):

``install.log``          append-only log of every Spack command and its output
``issues.jsonl``         ``jarvis.install-issue.v1`` lines from the latest run
``install-record.json``  ``jarvis.install-record.v1`` resolved hashes per package
``decisions.yaml``       recorded answers (``JARVIS_SPACK_DECISIONS`` overrides)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any, Callable, Dict, List, Mapping, Optional, Sequence

import yaml


ISSUE_SCHEMA = "jarvis.install-issue.v1"
RECORD_SCHEMA = "jarvis.install-record.v1"
REPORT_SCHEMA = "jarvis.install-report.v1"
DECISIONS_SCHEMA = "jarvis.install-decisions.v1"
PROGRESS_SCHEMA = "jarvis.install.progress.v1"

#: exit status of ``jarvis`` when an install needs a decision or (in check
#: mode) an install before it can continue. Read ``issues.jsonl``.
DECISION_NEEDED_EXIT_CODE = 3

MPI_PROVIDERS = (
    "openmpi",
    "mpich",
    "mvapich2",
    "mvapich",
    "intel-oneapi-mpi",
    "intel-mpi",
    "cray-mpich",
    "hpcx-mpi",
    "mpt",
    "spectrum-mpi",
)

_STATUS = {"[+]": "installed", "[^]": "upstream", "[e]": "external", " - ": "missing"}
_SPEC_LINE = re.compile(
    r"^(?P<status>\[[+^e]\]| - )\s+(?P<hash>[a-z0-9]{7,32})\s+"
    r"(?P<indent>\s*)(?P<caret>\^?)(?P<spec>[A-Za-z0-9].*?)\s*$"
)
_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")
_FIND_LINE = re.compile(
    r"^(?P<name>[a-z0-9][a-z0-9_-]*)@(?P<version>\S+)\s+"
    r"(?P<hash>[a-z0-9]{32})\s+(?P<prefix>/\S.*)$"
)
_MAX_ISSUE_TEXT = 4096


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class InstallDecisionNeeded(RuntimeError):
    """An install stopped because a person or caller has to decide.

    ``issues_path`` holds one ``jarvis.install-issue.v1`` line per question.
    The CLI exits with :data:`DECISION_NEEDED_EXIT_CODE`.
    """

    exit_code = DECISION_NEEDED_EXIT_CODE

    def __init__(
        self,
        issues: Sequence[Mapping[str, Any]],
        issues_path: Path,
        report: Optional[Mapping[str, Any]] = None,
    ):
        kinds = sorted({str(issue.get("kind")) for issue in issues})
        super().__init__(
            f"install needs {len(issues)} decision(s) ({', '.join(kinds)}); "
            f"see {issues_path}"
        )
        self.issues = [dict(issue) for issue in issues]
        self.issues_path = Path(issues_path)
        self.report = dict(report) if report is not None else None


# ---------------------------------------------------------------------------
# Spack invocation
# ---------------------------------------------------------------------------


def spack_root() -> Optional[str]:
    """The explicit Spack root: ``JARVIS_SPACK_ROOT``, else ``SPACK_ROOT``."""
    for name in ("JARVIS_SPACK_ROOT", "SPACK_ROOT"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return None


def spack_shell_prefix() -> str:
    """Shell prefix that sources the selected Spack's ``setup-env.sh``."""
    root = spack_root()
    if root is None:
        return ""
    setup = Path(root) / "share" / "spack" / "setup-env.sh"
    if os.environ.get("JARVIS_SPACK_ROOT") and not setup.is_file():
        raise RuntimeError(f"JARVIS_SPACK_ROOT has no {setup}")
    return f". {shlex.quote(str(setup))} && "


def spack_subprocess_environment() -> Dict[str, str]:
    """Process environment for Spack, isolated by ``JARVIS_SPACK_CONFIG``."""
    environment = dict(os.environ)
    config = os.environ.get("JARVIS_SPACK_CONFIG", "").strip()
    if config:
        environment["SPACK_USER_CONFIG_PATH"] = config
        environment["SPACK_DISABLE_LOCAL_CONFIG"] = "1"
    return environment


class SpackRunner:
    """Run ``spack`` through bash with the selected root, logging everything."""

    def __init__(self, log: Optional[IO[str]] = None):
        self.log = log
        self.commands: List[str] = []

    def _log(self, text: str) -> None:
        if self.log is not None:
            self.log.write(text)
            self.log.flush()

    def run(
        self, arguments: Sequence[str], *, stream: bool = False
    ) -> subprocess.CompletedProcess[str]:
        """Run one Spack command; ``stream`` copies output line by line."""
        command = "spack " + shlex.join(arguments)
        self.commands.append(command)
        script = spack_shell_prefix() + command
        self._log(f"[{_utc_now()}] $ {command}\n")
        environment = spack_subprocess_environment()
        if stream:
            process = subprocess.Popen(
                ["bash", "-c", script],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
            )
            lines: List[str] = []
            assert process.stdout is not None
            for line in process.stdout:
                lines.append(line)
                self._log(line)
            return_code = process.wait()
            output = "".join(lines)
            completed = subprocess.CompletedProcess(
                ["bash", "-c", script], return_code, output, ""
            )
        else:
            completed = subprocess.run(
                ["bash", "-c", script],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
                check=False,
            )
            self._log(completed.stdout)
            self._log(completed.stderr)
        self._log(f"[{_utc_now()}] exit {completed.returncode}\n")
        if completed.returncode == 127 and "spack" in (
            (completed.stderr or "") + (completed.stdout or "")
        ):
            raise RuntimeError(
                "spack is not available; set JARVIS_SPACK_ROOT to a Spack checkout"
            )
        return completed


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SpecNode:
    """One node of a ``spack spec -I -l`` tree."""

    status: str
    hash: str
    name: str
    spec: str
    depth: int

    @property
    def present(self) -> bool:
        return self.status != "missing"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "hash": self.hash,
            "status": self.status,
            "spec": self.spec[:512],
        }


def parse_spec_tree(text: str) -> List[SpecNode]:
    """Parse the concretized tree printed by ``spack spec -I -l``."""
    lines = text.splitlines()
    start = 0
    for index, line in enumerate(lines):
        if line.strip().lower() == "concretized":
            start = index + 1
    nodes: List[SpecNode] = []
    seen: set[str] = set()
    for line in lines[start:]:
        match = _SPEC_LINE.match(line)
        if match is None:
            continue
        spec = match.group("spec")
        name_match = _NAME.match(spec)
        if name_match is None:
            continue
        node_hash = match.group("hash")
        if node_hash in seen:
            continue
        seen.add(node_hash)
        nodes.append(
            SpecNode(
                status=_STATUS[match.group("status")],
                hash=node_hash,
                name=name_match.group(0),
                spec=spec,
                depth=0 if not match.group("caret") else 1 + len(match.group("indent")) // 4,
            )
        )
    return nodes


def classify_concretize_failure(output: str) -> str:
    """Map a failed ``spack spec`` into an issue kind."""
    lowered = output.lower()
    if "conflict" in lowered:
        return "variant_conflict"
    if (
        "unknownpackageerror" in lowered
        or "unknown package" in lowered
        or "no such package" in lowered
        or re.search(r"package '?[a-z0-9_.-]+'? not found", lowered)
        or "repository is not registered" in lowered
    ):
        return "no_recipe"
    return "concretize_failed"


def _bounded(text: str, limit: int = _MAX_ISSUE_TEXT) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return "[truncated] " + text[-limit:]


def _error_excerpt(output: str) -> str:
    """The Spack error lines, or the tail of the output."""
    errors = [line for line in output.splitlines() if "error" in line.lower()]
    return _bounded("\n".join(errors) if errors else output, 2048)


# ---------------------------------------------------------------------------
# Issues, answers and decisions
# ---------------------------------------------------------------------------


def make_issue(
    *,
    kind: str,
    package: str,
    subject: str,
    scope: str,
    question: str,
    candidates: Sequence[Mapping[str, Any]] = (),
    detail: str = "",
    evidence: Sequence[str] = (),
    answerable: bool,
    hint: Optional[str] = None,
) -> Dict[str, Any]:
    """Build one ``jarvis.install-issue.v1`` record with a stable id.

    The id digests the kind, subject, package (for package scope) and the
    candidate set, so the same situation keeps its id and a changed candidate
    set asks a new question.
    """
    identity = {
        "kind": kind,
        "subject": subject,
        "package": package if scope == "package" else None,
        "candidates": sorted(str(item.get("constraint")) for item in candidates),
        "detail": detail if not candidates else "",
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    issue: Dict[str, Any] = {
        "schema": ISSUE_SCHEMA,
        "issue_id": f"sha256:{digest}",
        "kind": kind,
        "package": package,
        "subject": subject,
        "scope": scope,
        "question": question,
        "candidates": [dict(item) for item in candidates],
        "answerable": answerable,
        "evidence": [_bounded(item, 1024) for item in evidence],
        "created_at": _utc_now(),
    }
    if detail:
        issue["detail"] = _bounded(detail)
    if hint:
        issue["hint"] = hint
    return issue


def _validated_constraint(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 1024:
        raise ValueError("an install answer constraint must be a non-empty string")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("an install answer constraint cannot contain control text")
    shlex.split(value)
    return value.strip()


def load_answers(path: Optional[Path]) -> Dict[str, Dict[str, Any]]:
    """Read ``answers.json``: ``{issue_id: constraint | {constraint, ...}}``."""
    if path is None:
        return {}
    with Path(path).open("r", encoding="utf-8") as stream:
        raw = json.load(stream)
    if not isinstance(raw, dict):
        raise ValueError("answers.json must map issue ids to constraints")
    answers: Dict[str, Dict[str, Any]] = {}
    for issue_id, value in raw.items():
        if not isinstance(issue_id, str) or not issue_id.startswith("sha256:"):
            raise ValueError(f"answers.json key {issue_id!r} is not an issue id")
        entry = dict(value) if isinstance(value, Mapping) else {"constraint": value}
        entry["constraint"] = _validated_constraint(entry.get("constraint"))
        answers[issue_id] = {
            key: entry[key]
            for key in ("constraint", "answered_by", "ask_id")
            if entry.get(key) is not None
        }
    return answers


class Decisions:
    """Recorded answers, keyed by scope: ``site`` subjects and per package."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.data: Dict[str, Any] = {
            "schema_version": DECISIONS_SCHEMA,
            "site": {},
            "packages": {},
        }
        if self.path.is_file():
            with self.path.open("r", encoding="utf-8") as stream:
                loaded = yaml.safe_load(stream) or {}
            if not isinstance(loaded, dict):
                raise ValueError(f"{self.path} must be a mapping")
            self.data["site"] = dict(loaded.get("site") or {})
            self.data["packages"] = {
                str(name): list(entries or [])
                for name, entries in (loaded.get("packages") or {}).items()
            }

    def site(self, subject: str) -> Optional[Dict[str, Any]]:
        value = self.data["site"].get(subject)
        return dict(value) if isinstance(value, Mapping) else None

    def package_constraints(self, package: str) -> List[str]:
        return [
            str(entry["constraint"])
            for entry in self.data["packages"].get(package, [])
            if isinstance(entry, Mapping) and entry.get("constraint")
        ]

    def record(self, issue: Mapping[str, Any], answer: Mapping[str, Any]) -> None:
        entry = {
            "kind": issue["kind"],
            "subject": issue["subject"],
            "issue_id": issue["issue_id"],
            "constraint": answer["constraint"],
            "answered_at": _utc_now(),
        }
        for key in ("answered_by", "ask_id"):
            if answer.get(key) is not None:
                entry[key] = answer[key]
        if issue["scope"] == "site":
            self.data["site"][issue["subject"]] = entry
        else:
            entries = [
                existing
                for existing in self.data["packages"].get(issue["package"], [])
                if existing.get("issue_id") != issue["issue_id"]
            ]
            entries.append(entry)
            self.data["packages"][issue["package"]] = entries
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(self.path, yaml.safe_dump(self.data, sort_keys=True))


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------


@dataclass
class InstallContext:
    """Where one install run reads answers and writes its log and results."""

    install_dir: Path
    mode: str = "install"
    answers: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    decisions_path: Optional[Path] = None
    progress: Optional[Callable[..., None]] = None

    def __post_init__(self) -> None:
        if self.mode not in {"install", "check"}:
            raise ValueError("install mode must be 'install' or 'check'")
        self.install_dir = Path(self.install_dir)
        if self.decisions_path is None:
            configured = os.environ.get("JARVIS_SPACK_DECISIONS", "").strip()
            self.decisions_path = (
                Path(configured) if configured else self.install_dir / "decisions.yaml"
            )
        self.issues: List[Dict[str, Any]] = []
        self.used_answers: List[str] = []
        self._log_stream: Optional[IO[str]] = None

    @property
    def log_path(self) -> Path:
        return self.install_dir / "install.log"

    @property
    def issues_path(self) -> Path:
        return self.install_dir / "issues.jsonl"

    @property
    def record_path(self) -> Path:
        return self.install_dir / "install-record.json"

    def open(self) -> "InstallContext":
        """Create the directory, truncate issues and open the append-only log."""
        self.install_dir.mkdir(parents=True, exist_ok=True)
        self.issues_path.write_text("", encoding="utf-8")
        self._log_stream = self.log_path.open("a", encoding="utf-8")
        self.log(f"=== jarvis install ({self.mode}) started {_utc_now()} ===\n")
        return self

    def close(self) -> None:
        if self._log_stream is not None:
            self.log(f"=== jarvis install ({self.mode}) ended {_utc_now()} ===\n")
            self._log_stream.close()
            self._log_stream = None

    @property
    def log_stream(self) -> Optional[IO[str]]:
        return self._log_stream

    def log(self, text: str) -> None:
        if self._log_stream is not None:
            self._log_stream.write(text)
            self._log_stream.flush()

    def report_progress(self, **fields: Any) -> None:
        if self.progress is not None:
            self.progress(**fields)

    def raise_issue(self, issue: Dict[str, Any]) -> None:
        if any(existing["issue_id"] == issue["issue_id"] for existing in self.issues):
            return
        self.issues.append(issue)
        with self.issues_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(issue, sort_keys=True, separators=(",", ":")) + "\n")
        self.log(f"ISSUE {issue['kind']} {issue['issue_id']}: {issue['question']}\n")

    def answer_for(self, issue: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        answer = self.answers.get(str(issue["issue_id"]))
        if answer is None:
            return None
        candidates = issue.get("candidates") or []
        if candidates and answer["constraint"] not in {
            str(candidate.get("constraint")) for candidate in candidates
        }:
            raise ValueError(
                f"answer for {issue['issue_id']} must be one of the candidate "
                "constraints"
            )
        self.used_answers.append(str(issue["issue_id"]))
        return answer


def _query_pins_mpi(tokens: Sequence[str]) -> bool:
    text = " ".join(tokens)
    return any(
        re.search(rf"\^\s*{re.escape(provider)}(?![a-z0-9_-])", text)
        for provider in MPI_PROVIDERS
    )


class SpackPlanner:
    """Resolve, reuse and (in install mode) build each package's spec."""

    def __init__(self, context: InstallContext, runner: Optional[SpackRunner] = None):
        self.context = context
        self.runner = runner or SpackRunner(context.log_stream)
        self.decisions = Decisions(context.decisions_path)  # type: ignore[arg-type]
        self._mpi_candidates: Optional[List[Dict[str, Any]]] = None

    # -- discovery ---------------------------------------------------------

    def mpi_candidates(self) -> List[Dict[str, Any]]:
        """Installed, upstream and used-external MPIs, merged by prefix."""
        if self._mpi_candidates is not None:
            return self._mpi_candidates
        by_prefix: Dict[str, Dict[str, Any]] = {}
        for provider in MPI_PROVIDERS:
            completed = self.runner.run(
                ["find", "--format", "{name}@{version} {hash} {prefix}", provider]
            )
            if completed.returncode != 0:
                continue
            for line in completed.stdout.splitlines():
                match = _FIND_LINE.match(line.strip())
                if match is None:
                    continue
                prefix = match.group("prefix").strip()
                short = match.group("hash")[:7]
                existing = by_prefix.get(prefix)
                if existing is not None:
                    existing["hashes"].append(short)
                    continue
                by_prefix[prefix] = {
                    "id": f"{match.group('name')}-{match.group('version')}-{short}",
                    "constraint": f"^{match.group('name')}/{short}",
                    "name": match.group("name"),
                    "version": match.group("version"),
                    "prefix": prefix,
                    "hashes": [short],
                }
        candidates = []
        for candidate in by_prefix.values():
            others = candidate["hashes"][1:]
            candidate["source"] = f"spack find: {candidate['prefix']}" + (
                f" (also registered as {', '.join('/' + h for h in others)}, same prefix)"
                if others
                else ""
            )
            candidates.append(candidate)
        candidates.sort(key=lambda item: item["id"])
        self._mpi_candidates = candidates
        return candidates

    def _spec(self, tokens: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return self.runner.run(["spec", "-I", "-l", *tokens])

    def ensure_repos(self, repos: Sequence[str]) -> None:
        for repo in repos:
            completed = self.runner.run(["repo", "add", str(repo)])
            output = (completed.stdout or "") + (completed.stderr or "")
            if completed.returncode != 0 and "already" not in output.lower():
                raise RuntimeError(f"spack repo add {repo} failed: {_error_excerpt(output)}")

    # -- per package -------------------------------------------------------

    def plan_package(self, package: str, query: str) -> Dict[str, Any]:
        """Resolve one package; returns its install-record entry."""
        base_tokens = shlex.split(query)
        constraints = self.decisions.package_constraints(package)
        entry: Dict[str, Any] = {
            "query": query,
            "status": "resolving",
            "constraints": list(constraints),
            "mpi": None,
        }
        nodes: Optional[List[SpecNode]] = None
        for _attempt in range(4):
            tokens = [*base_tokens, *(t for c in constraints for t in shlex.split(c))]
            completed = self._spec(tokens)
            output = (completed.stdout or "") + (completed.stderr or "")
            if completed.returncode == 0:
                nodes = parse_spec_tree(completed.stdout)
                if nodes:
                    break
                output = "spack spec printed no concretized tree\n" + output
            kind = classify_concretize_failure(output)
            issue = self._concretize_issue(kind, package, tokens, output)
            answer = self.context.answer_for(issue) if issue["answerable"] else None
            if answer is None:
                self.context.raise_issue(issue)
                entry["status"] = "needs_decision"
                entry["issue_id"] = issue["issue_id"]
                return entry
            self.decisions.record(issue, answer)
            constraints.append(answer["constraint"])
            entry["constraints"] = list(constraints)
        if nodes is None:
            raise RuntimeError(f"{package}: spack spec did not converge")

        tokens = [*base_tokens, *(t for c in constraints for t in shlex.split(c))]
        mpi_nodes = [node for node in nodes if node.name in MPI_PROVIDERS]
        if mpi_nodes and not _query_pins_mpi(tokens):
            pin, source = self._choose_mpi(package, mpi_nodes[0])
            if pin is None and source.startswith("needs_decision:"):
                entry["status"] = "needs_decision"
                entry["issue_id"] = source.split(":", 1)[1]
                return entry
            if pin is not None:
                tokens = [*tokens, *shlex.split(pin)]
                constraints.append(pin)
                entry["constraints"] = list(constraints)
                completed = self._spec(tokens)
                output = (completed.stdout or "") + (completed.stderr or "")
                nodes = parse_spec_tree(completed.stdout) if completed.returncode == 0 else []
                if not nodes:
                    issue = self._concretize_issue(
                        classify_concretize_failure(output), package, tokens, output
                    )
                    self.context.raise_issue(issue)
                    entry["status"] = "needs_decision"
                    entry["issue_id"] = issue["issue_id"]
                    return entry
            mpi_node = next(node for node in nodes if node.name in MPI_PROVIDERS)
            entry["mpi"] = {"name": mpi_node.name, "hash": mpi_node.hash, "source": source}
        elif mpi_nodes:
            entry["mpi"] = {
                "name": mpi_nodes[0].name,
                "hash": mpi_nodes[0].hash,
                "source": "query",
            }
        self._fill_nodes(entry, nodes, tokens)
        return entry

    def _fill_nodes(
        self, entry: Dict[str, Any], nodes: Sequence[SpecNode], tokens: Sequence[str]
    ) -> None:
        root = nodes[0]
        missing = [node for node in nodes if not node.present]
        counts: Dict[str, int] = {}
        for node in nodes:
            counts[node.status] = counts.get(node.status, 0) + 1
        entry.update(
            {
                "spec": shlex.join(tokens),
                "root": {"name": root.name, "hash": root.hash, "spec": root.spec[:512]},
                "nodes_total": len(nodes),
                "nodes_by_status": counts,
                "missing": [node.to_dict() for node in missing],
                "status": "satisfied" if not missing else "missing",
            }
        )

    def _choose_mpi(
        self, package: str, concretized: SpecNode
    ) -> tuple[Optional[str], str]:
        candidates = self.mpi_candidates()
        constraints = {candidate["constraint"] for candidate in candidates}
        recorded = self.decisions.site("mpi")
        if recorded is not None and recorded.get("constraint") in constraints:
            return str(recorded["constraint"]), "decision"
        if len(candidates) == 1:
            return candidates[0]["constraint"], "auto_single_candidate"
        if not candidates:
            return None, "concretizer"
        options = []
        for candidate in candidates:
            options.append(
                {
                    key: candidate[key]
                    for key in ("id", "constraint", "source", "prefix", "hashes")
                }
                | {
                    "implies": (
                        "the concretizer's current choice"
                        if candidate["hashes"][0] == concretized.hash[:7]
                        else "re-resolves dependents against this MPI"
                    )
                }
            )
        issue = make_issue(
            kind="ambiguous_provider",
            package=package,
            subject="mpi",
            scope="site",
            question="Which MPI should packages use on this site?",
            candidates=options,
            evidence=[
                "spack find --format '{name}@{version} {hash} {prefix}' "
                + " ".join(MPI_PROVIDERS),
                f"spack spec -I -l (concretizer picked {concretized.name}/{concretized.hash})",
            ],
            answerable=True,
        )
        answer = self.context.answer_for(issue)
        if answer is None:
            self.context.raise_issue(issue)
            return None, f"needs_decision:{issue['issue_id']}"
        self.decisions.record(issue, answer)
        return str(answer["constraint"]), "decision"

    def _concretize_issue(
        self, kind: str, package: str, tokens: Sequence[str], output: str
    ) -> Dict[str, Any]:
        excerpt = _error_excerpt(output)
        spec = shlex.join(tokens)
        if kind == "variant_conflict":
            return make_issue(
                kind=kind,
                package=package,
                subject=spec,
                scope="package",
                question=(
                    f"Spack reports a conflict for {package} ({spec}). "
                    "Which constraint should be added to resolve it?"
                ),
                detail=excerpt,
                evidence=[f"spack spec -I -l {spec}"],
                answerable=True,
                hint="answer with a spec constraint (e.g. '~cuda') appended to the query",
            )
        if kind == "no_recipe":
            return make_issue(
                kind=kind,
                package=package,
                subject=spec,
                scope="package",
                question=f"Spack has no recipe for {package} ({spec}).",
                detail=excerpt,
                evidence=[f"spack spec -I -l {spec}"],
                answerable=False,
                hint="add a recipe repo through install_repos or change install_query",
            )
        return make_issue(
            kind="concretize_failed",
            package=package,
            subject=spec,
            scope="package",
            question=f"Spack could not concretize {package} ({spec}).",
            detail=excerpt,
            evidence=[f"spack spec -I -l {spec}"],
            answerable=True,
            hint="answer with a spec constraint appended to the query",
        )

    def install_package(self, package: str, entry: Dict[str, Any]) -> Dict[str, Any]:
        """Build only the missing nodes of a resolved entry, then re-verify."""
        tokens = shlex.split(entry["spec"])
        missing_before = [item["name"] for item in entry["missing"]]
        completed = self.runner.run(["install", "--fail-fast", *tokens], stream=True)
        if completed.returncode != 0:
            entry["status"] = "failed"
            entry["error"] = _error_excerpt(completed.stdout or "")
            return entry
        verify = self._spec(tokens)
        nodes = parse_spec_tree(verify.stdout) if verify.returncode == 0 else []
        if not nodes:
            entry["status"] = "failed"
            entry["error"] = "could not re-verify the installed spec"
            return entry
        self._fill_nodes(entry, nodes, tokens)
        entry["built"] = missing_before
        if entry["status"] == "satisfied":
            entry["status"] = "installed"
        else:
            entry["status"] = "failed"
            entry["error"] = "spack install succeeded but nodes are still missing"
        return entry


def _package_query(pkg_def: Mapping[str, Any]) -> str:
    config = pkg_def.get("config", {}) or {}
    return str(config.get("install_query") or config.get("install") or "").strip()


def _package_repos(pkg_def: Mapping[str, Any]) -> List[str]:
    config = pkg_def.get("config", {}) or {}
    repos = config.get("install_repos") or []
    if isinstance(repos, str):
        repos = [repos]
    return [str(repo) for repo in repos]


def run_spack_install(
    pkg_list: Sequence[Mapping[str, Any]],
    context: InstallContext,
    runner: Optional[SpackRunner] = None,
) -> Dict[str, Any]:
    """Plan and, in install mode, build every Spack package in ``pkg_list``.

    Returns the ``jarvis.install-record.v1`` document (also written to
    ``install-record.json``). Issues are appended to ``context.issues``; the
    caller decides whether to raise :class:`InstallDecisionNeeded`.
    """
    planner = SpackPlanner(context, runner)
    record: Dict[str, Any] = {
        "schema_version": RECORD_SCHEMA,
        "mode": context.mode,
        "spack_root": spack_root(),
        "spack_config": os.environ.get("JARVIS_SPACK_CONFIG") or None,
        "decisions": str(context.decisions_path),
        "started_at": _utc_now(),
        "packages": {},
    }
    packages = [
        (str(pkg_def.get("pkg_id") or pkg_def.get("pkg_name") or "pkg"), pkg_def)
        for pkg_def in pkg_list
    ]
    total = len(packages)
    for index, (package, pkg_def) in enumerate(packages):
        query = _package_query(pkg_def)
        context.report_progress(
            phase="resolving", current_package=package, packages_done=index
        )
        if not query:
            record["packages"][package] = {"status": "no_query"}
            continue
        planner.ensure_repos(_package_repos(pkg_def))
        entry = planner.plan_package(package, query)
        if entry["status"] == "missing":
            if context.mode == "check":
                context.raise_issue(
                    make_issue(
                        kind="not_installed",
                        package=package,
                        subject=entry["spec"],
                        scope="package",
                        question=(
                            f"{package} needs {len(entry['missing'])} Spack "
                            "package(s) built before it can run."
                        ),
                        candidates=[],
                        detail=", ".join(
                            f"{item['name']}/{item['hash']}" for item in entry["missing"]
                        ),
                        answerable=False,
                        hint="run `jarvis ppl install` (e.g. as a 1-node +submit job)",
                    )
                )
            else:
                context.report_progress(
                    phase="building",
                    current_package=package,
                    packages_done=index,
                    missing=[item["name"] for item in entry["missing"]][:32],
                )
                entry = planner.install_package(package, entry)
        record["packages"][package] = entry
        context.report_progress(
            phase="resolving",
            current_package=None,
            packages_done=index + 1,
            missing=[item["name"] for item in entry.get("missing", [])][:32],
        )
    record["finished_at"] = _utc_now()
    record["packages_total"] = total
    record["issues"] = [issue["issue_id"] for issue in context.issues]
    record["used_answers"] = list(context.used_answers)
    _atomic_write_text(
        context.record_path, json.dumps(record, indent=2, sort_keys=True) + "\n"
    )
    return record
