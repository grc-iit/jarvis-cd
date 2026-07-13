"""
Job scheduler integration for Jarvis-CD.

Provides a base ``Scheduler`` class plus concrete backends (SLURM, PBS, ...)
that turn a pipeline's ``scheduler:`` YAML block into a submittable job
script. The generated script is responsible for:

  1. Constructing a hostfile from the scheduler's allocation
     (e.g. ``$SLURM_JOB_NODELIST``) and writing it to the execution-scoped
     location the pipeline expects.
  2. Running the pipeline's isolated execution snapshot once the hostfile is
     in place.

The hostfile path is exported back onto the pipeline so every package
that resolves ``self.hostfile`` sees the same file the job script will
populate.
"""

from __future__ import annotations

import os
import re
import shlex
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional


_DIRECTIVE_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_HOST_SUFFIX = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


def _validated_single_line(value: Any, *, field: str, limit: int = 4096) -> str:
    """Return one bounded printable scheduler value."""
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > limit:
        raise ValueError(f"scheduler.{field} must be a non-empty bounded string")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"scheduler.{field} cannot contain control characters")
    return value


def make_scheduler(
    spec: Dict[str, Any],
    pipeline_shared_dir: Path,
    pipeline_yaml: Optional[str] = None,
    pipeline_name: Optional[str] = None,
    pipeline_snapshot_dir: Optional[Path] = None,
    jarvis_root: Optional[Path] = None,
) -> "Scheduler":
    """Factory: build a Scheduler from a parsed YAML dict.

    :param spec: ``scheduler:`` block from the pipeline YAML
    :param pipeline_shared_dir: pipeline-level shared directory
        (used to default the hostfile path and to drop the job script)
    :param pipeline_yaml: path to the pipeline YAML the job should run
        (None when invoked from a pre-loaded current pipeline)
    :param pipeline_name: pipeline name (used for ``jarvis ppl run`` when
        no YAML path is supplied)
    :param pipeline_snapshot_dir: immutable execution-scoped pipeline input
        directory. When provided, the scheduler runs that snapshot rather than
        reloading mutable named-pipeline state.
    :param jarvis_root: configuration root whose repository selection must be
        retained by a scheduled snapshot.
    """
    name = (spec or {}).get("name")
    if not name:
        raise ValueError("scheduler.name is required (e.g. 'slurm')")

    name = str(name).strip().lower()
    cls = _SCHEDULERS.get(name)
    if cls is None:
        supported = ", ".join(sorted(_SCHEDULERS))
        raise ValueError(f"Unsupported scheduler '{name}'. Supported: {supported}")
    return cls(
        spec,
        pipeline_shared_dir,
        pipeline_yaml=pipeline_yaml,
        pipeline_name=pipeline_name,
        pipeline_snapshot_dir=pipeline_snapshot_dir,
        jarvis_root=jarvis_root,
    )


class Scheduler:
    """Base class for batch-scheduler integrations."""

    #: scheduler name as it appears in YAML (``slurm``, ``pbs``, ...)
    NAME: str = ""

    #: filename of the generated submission script inside the pipeline
    #: shared directory
    SCRIPT_NAME: str = "submit.sh"

    def __init__(
        self,
        spec: Dict[str, Any],
        pipeline_shared_dir: Path,
        pipeline_yaml: Optional[str] = None,
        pipeline_name: Optional[str] = None,
        pipeline_snapshot_dir: Optional[Path] = None,
        jarvis_root: Optional[Path] = None,
    ):
        self.spec = dict(spec or {})
        self.shared_dir = Path(pipeline_shared_dir)
        self.pipeline_yaml = pipeline_yaml
        self.pipeline_name = pipeline_name
        self.pipeline_snapshot_dir = (
            Path(pipeline_snapshot_dir) if pipeline_snapshot_dir is not None else None
        )
        self.jarvis_root = Path(jarvis_root) if jarvis_root is not None else None

        default_hostfile = str(self.shared_dir / "hostfile.txt")
        hostfile = self.spec.get("hostfile") or default_hostfile
        validated_hostfile = _validated_single_line(
            hostfile,
            field="hostfile",
        )
        self.hostfile = os.path.expandvars(validated_hostfile)
        _validated_single_line(self.hostfile, field="hostfile")

    @property
    def script_path(self) -> Path:
        return self.shared_dir / self.SCRIPT_NAME

    def render(self) -> str:
        """Render the submission script as a string."""
        raise NotImplementedError

    def write_script(self) -> Path:
        """Durably write the execution-scoped submission script."""
        self.shared_dir.mkdir(parents=True, exist_ok=True)
        path = self.script_path
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.shared_dir,
            prefix=f".{path.name}.",
            suffix=".tmp",
            text=True,
        )
        temporary_path = Path(temporary_name)
        descriptor_owned = True
        try:
            try:
                stream = os.fdopen(
                    descriptor,
                    "w",
                    encoding="utf-8",
                    newline="\n",
                )
            except BaseException:
                os.close(descriptor)
                descriptor_owned = False
                raise
            descriptor_owned = False
            with stream:
                stream.write(self.render())
                stream.flush()
                os.fsync(stream.fileno())
            temporary_path.chmod(0o700)
            os.replace(temporary_path, path)
            if os.name != "nt":
                directory = os.open(
                    self.shared_dir,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                )
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            if descriptor_owned:
                os.close(descriptor)
            temporary_path.unlink(missing_ok=True)
        return path

    def submit_command(self, wait: bool = False) -> List[str]:
        """Argv used to submit the rendered script.

        :param wait: when True, ask the scheduler to block until the job
            finishes (e.g. ``sbatch --wait``). Backends that have no
            equivalent ignore the flag.
        """
        raise NotImplementedError

    def parse_submission_output(self, stdout: str) -> Dict[str, Any]:
        """Parse provider-owned submission output into stable metadata.

        Scheduler implementations must reject output that cannot be tied to a
        submission made by their own command.  Callers must never infer job
        identities from arbitrary application stdout.
        """
        raise NotImplementedError

    def _pipeline_invocation(self) -> str:
        """Shell snippet that runs the pipeline once the hostfile is in
        place. Always points jarvis at the persisted YAML when one was
        supplied, falling back to ``jarvis ppl run`` against the
        currently-loaded pipeline otherwise.
        """
        jarvis_cli = f"{shlex.quote(sys.executable)} -m jarvis_cd.core.cli"
        if self.pipeline_snapshot_dir is not None:
            snapshot = self.pipeline_snapshot_dir
            snapshot_yaml = snapshot / "pipeline.yaml"
            environment = [
                "env",
                "JARVIS_PIPELINE_SNAPSHOT_DIR=" + shlex.quote(str(snapshot)),
            ]
            if self.jarvis_root is not None:
                environment.append("JARVIS_ROOT=" + shlex.quote(str(self.jarvis_root)))
            return (
                " ".join(environment)
                + f" {jarvis_cli} ppl run yaml {shlex.quote(str(snapshot_yaml))}"
            )
        if self.pipeline_yaml:
            return f"{jarvis_cli} ppl run yaml {shlex.quote(self.pipeline_yaml)}"
        if self.pipeline_name:
            return (
                f"{jarvis_cli} cd {shlex.quote(self.pipeline_name)} && "
                f"{jarvis_cli} ppl run"
            )
        return f"{jarvis_cli} ppl run"

    def _execution_lifecycle_block(self) -> str:
        """Render an EXIT trap that durably finalizes scheduler script state."""
        finalizer = ""
        if self.pipeline_snapshot_dir is not None:
            execution_root = self.pipeline_snapshot_dir.parent
            execution_id = execution_root.name
            python_executable = shlex.quote(sys.executable)
            finalizer = (
                f"    if ! {python_executable} -m jarvis_cd.core.execution finalize "
                f"--execution-root {shlex.quote(str(execution_root))} "
                f"--execution-id {shlex.quote(execution_id)} "
                '--return-code "$jarvis_exit_code"; then\n'
                '        echo "Could not persist JARVIS execution state" >&2\n'
                '        if [ "$jarvis_exit_code" -eq 0 ]; then\n'
                "            jarvis_exit_code=1\n"
                "        fi\n"
                "    fi\n"
            )
        return (
            "jarvis_hostfile_tmp=\n"
            "jarvis_finalize_execution() {\n"
            "    jarvis_exit_code=$?\n"
            "    trap - EXIT\n"
            '    if [ -n "${jarvis_hostfile_tmp:-}" ]; then\n'
            '        if ! rm -f -- "$jarvis_hostfile_tmp"; then\n'
            '            echo "Could not remove temporary JARVIS hostfile" >&2\n'
            '            if [ "$jarvis_exit_code" -eq 0 ]; then\n'
            "                jarvis_exit_code=1\n"
            "            fi\n"
            "        fi\n"
            "    fi\n"
            f"{finalizer}"
            '    exit "$jarvis_exit_code"\n'
            "}\n"
            "trap jarvis_finalize_execution EXIT\n"
        )

    def _execution_activation_block(self) -> str:
        """Render provider-specific runtime identity activation when available."""
        return ""


class SlurmScheduler(Scheduler):
    """SLURM (sbatch) backend."""

    NAME = "slurm"
    SCRIPT_NAME = "submit.slurm"

    #: keys in the scheduler block we consume directly; everything else is
    #: treated as a long-form ``--<key>=<value>`` sbatch directive so users
    #: can pass arbitrary SBATCH flags from YAML without us hard-coding
    #: every one.
    _STRUCTURAL_KEYS = {
        "name",
        "hostfile",
        "suffix",
        "sbatch_args",
        "pre_cmds",
        "post_cmds",
    }

    #: explicit short-key -> SBATCH long-flag map. Anything not in here
    #: that isn't structural is passed as ``--<key>=<value>`` with
    #: underscores rewritten to dashes.
    _EXPLICIT_DIRECTIVES = {
        "job_name": "--job-name",
        "nodes": "--nodes",
        "ntasks": "--ntasks",
        "ntasks_per_node": "--ntasks-per-node",
        "cpus_per_task": "--cpus-per-task",
        "mem": "--mem",
        "time": "--time",
        "partition": "--partition",
        "account": "--account",
        "qos": "--qos",
        "output": "--output",
        "error": "--error",
        "gres": "--gres",
        "gpus": "--gpus",
        "gpus_per_node": "--gpus-per-node",
        "constraint": "--constraint",
        "reservation": "--reservation",
        "exclusive": "--exclusive",
        "mail_user": "--mail-user",
        "mail_type": "--mail-type",
    }

    _PARSABLE_SUBMISSION = re.compile(
        r"^(?P<job_id>[0-9]+)(?:;(?P<cluster>[A-Za-z0-9_.-]+))?$"
    )

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._validate_spec()

    def _validate_spec(self) -> None:
        """Reject directive and hook text that can alter script structure."""
        if len(self.spec) > 128:
            raise ValueError("scheduler contains too many fields")
        for key, value in self.spec.items():
            if not isinstance(key, str) or _DIRECTIVE_KEY.fullmatch(key) is None:
                raise ValueError(f"invalid scheduler directive key: {key!r}")
            if key in {"sbatch_args", "pre_cmds", "post_cmds"}:
                if not isinstance(value, list) or len(value) > 64:
                    raise ValueError(f"scheduler.{key} must be a bounded list")
                for entry in value:
                    rendered = _validated_single_line(entry, field=key)
                    if key == "sbatch_args" and (
                        not rendered.startswith("--")
                        or any(character.isspace() for character in rendered)
                        or "#" in rendered
                    ):
                        raise ValueError(
                            "scheduler.sbatch_args entries must be single --flag tokens"
                        )
                continue
            if value is None or isinstance(value, bool):
                continue
            if isinstance(value, int):
                if value < 0:
                    raise ValueError(f"scheduler.{key} cannot be negative")
                continue
            rendered = _validated_single_line(value, field=key)
            if key != "hostfile" and (
                any(character.isspace() for character in rendered) or "#" in rendered
            ):
                raise ValueError(
                    f"scheduler.{key} must be one printable directive token"
                )
        suffix = self.spec.get("suffix")
        if suffix is not None and _HOST_SUFFIX.fullmatch(str(suffix)) is None:
            raise ValueError("scheduler.suffix is not a valid hostname suffix")

    def _directives(self) -> List[str]:
        lines: List[str] = []
        for key, value in self.spec.items():
            if key in self._STRUCTURAL_KEYS:
                continue
            if value is None or value is False:
                continue
            flag = self._EXPLICIT_DIRECTIVES.get(key, f"--{key.replace('_', '-')}")
            if value is True:
                lines.append(f"#SBATCH {flag}")
            else:
                lines.append(f"#SBATCH {flag}={value}")
        for raw in self.spec.get("sbatch_args", []) or []:
            lines.append(f"#SBATCH {raw}")
        return lines

    def render(self) -> str:
        directives = "\n".join(self._directives())
        pre = "\n".join(self.spec.get("pre_cmds", []) or [])
        post = "\n".join(self.spec.get("post_cmds", []) or [])
        hostfile = shlex.quote(self.hostfile)
        python_executable = shlex.quote(sys.executable)
        invocation = self._pipeline_invocation()
        lifecycle = self._execution_lifecycle_block()
        activation = self._execution_activation_block()

        # Build hostfile from the allocation. scontrol expands the
        # compact nodelist into one host per line; that's exactly what
        # jarvis.util.hostfile.Hostfile consumes. When `suffix:` is set,
        # append it to each hostname — used to redirect traffic onto a
        # secondary NIC (e.g. `ares-comp-1` -> `ares-comp-1-40g` for the
        # 40GbE interface).
        suffix = self.spec.get("suffix")
        if suffix:
            hostnames_cmd = (
                f'scontrol show hostnames "$SLURM_JOB_NODELIST" '
                f"| awk -v s={shlex.quote(str(suffix))} '{{print $0 s}}'"
            )
        else:
            hostnames_cmd = 'scontrol show hostnames "$SLURM_JOB_NODELIST"'
        hostfile_block = (
            f"jarvis_hostfile={hostfile}\n"
            'jarvis_hostfile_dir=$(dirname -- "$jarvis_hostfile")\n'
            "jarvis_hostfile_name=${jarvis_hostfile##*/}\n"
            'mkdir -p -- "$jarvis_hostfile_dir"\n'
            "jarvis_hostfile_tmp=$(mktemp -- "
            '"$jarvis_hostfile_dir/.${jarvis_hostfile_name}.jarvis.$$.XXXXXX")\n'
            f'if ! {{ {hostnames_cmd}; }} > "$jarvis_hostfile_tmp"; then\n'
            '    echo "Could not expand the SLURM allocation hostfile" >&2\n'
            "    exit 1\n"
            "fi\n"
            'if [ ! -s "$jarvis_hostfile_tmp" ]; then\n'
            '    echo "SLURM allocation produced an empty hostfile" >&2\n'
            "    exit 1\n"
            "fi\n"
            f'{python_executable} - "$jarvis_hostfile_tmp" '
            "\"$jarvis_hostfile\" <<'PY'\n"
            "import errno\n"
            "import os\n"
            "import sys\n"
            "\n"
            "temporary_path, final_path = sys.argv[1:]\n"
            "with open(temporary_path, 'rb') as stream:\n"
            "    os.fsync(stream.fileno())\n"
            "os.replace(temporary_path, final_path)\n"
            "directory_path = os.path.dirname(final_path) or '.'\n"
            "unsupported = {errno.EBADF, errno.EINVAL}\n"
            "for name in ('ENOTSUP', 'EOPNOTSUPP'):\n"
            "    value = getattr(errno, name, None)\n"
            "    if value is not None:\n"
            "        unsupported.add(value)\n"
            "try:\n"
            "    directory = os.open(\n"
            "        directory_path,\n"
            "        os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0),\n"
            "    )\n"
            "except OSError as error:\n"
            "    if error.errno not in unsupported:\n"
            "        raise\n"
            "else:\n"
            "    try:\n"
            "        os.fsync(directory)\n"
            "    except OSError as error:\n"
            "        if error.errno not in unsupported:\n"
            "            raise\n"
            "    finally:\n"
            "        os.close(directory)\n"
            "PY\n"
            "jarvis_hostfile_tmp=\n"
            'echo "Wrote hostfile: $jarvis_hostfile"\n'
            'cat -- "$jarvis_hostfile"\n'
        )

        script = (
            "#!/bin/bash\n"
            f"{directives}\n"
            "\n"
            "set -euo pipefail\n"
            "\n"
            f"{lifecycle}"
            f"{activation}"
            "\n"
            "# --- Pre-run hooks ---\n"
            f"{pre}\n"
            "\n"
            "# --- Build hostfile from SLURM allocation ---\n"
            f"{hostfile_block}"
            "\n"
            "# --- Run the Jarvis pipeline ---\n"
            f"{invocation}\n"
            "\n"
            "# --- Post-run hooks ---\n"
            f"{post}\n"
        )
        return script

    def _execution_activation_block(self) -> str:
        """Bind the allocation's trusted SLURM identity before user hooks."""
        if self.pipeline_snapshot_dir is None:
            return ""
        execution_root = self.pipeline_snapshot_dir.parent
        execution_id = execution_root.name
        python_executable = shlex.quote(sys.executable)
        return (
            "jarvis_scheduler_cluster_args=()\n"
            'if [ -n "${SLURM_CLUSTER_NAME:-}" ]; then\n'
            '    jarvis_scheduler_cluster_args=(--cluster "$SLURM_CLUSTER_NAME")\n'
            "fi\n"
            f"{python_executable} -m jarvis_cd.core.execution activate "
            f"--execution-root {shlex.quote(str(execution_root))} "
            f"--execution-id {shlex.quote(execution_id)} "
            f"--provider {shlex.quote(self.NAME)} "
            '--native-id "$SLURM_JOB_ID" "${jarvis_scheduler_cluster_args[@]}"\n'
        )

    def submit_command(self, wait: bool = False) -> List[str]:
        # ``--parsable`` is the provider boundary: stdout is a stable
        # ``job_id[;cluster]`` record rather than human-oriented text.
        cmd = ["sbatch", "--parsable"]
        if wait:
            cmd.append("--wait")
        cmd.append(str(self.script_path))
        return cmd

    def parse_submission_output(self, stdout: str) -> Dict[str, Any]:
        """Parse the exact record produced by ``sbatch --parsable``."""
        value = stdout.strip()
        match = self._PARSABLE_SUBMISSION.fullmatch(value)
        if match is None:
            raise ValueError(
                "SLURM submission did not return a valid "
                "`sbatch --parsable` job identity"
            )
        return {
            "provider": self.NAME,
            "scheduler_job_id": match.group("job_id"),
            "scheduler_cluster": match.group("cluster"),
            "identity_source": "scheduler_submit_api",
        }


_SCHEDULERS: Dict[str, type] = {
    SlurmScheduler.NAME: SlurmScheduler,
}
