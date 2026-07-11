"""
Job scheduler integration for Jarvis-CD.

Provides a base ``Scheduler`` class plus concrete backends (SLURM, PBS, ...)
that turn a pipeline's ``scheduler:`` YAML block into a submittable job
script. The generated script is responsible for:

  1. Constructing a hostfile from the scheduler's allocation
     (e.g. ``$SLURM_JOB_NODELIST``) and writing it to the location the
     pipeline expects (defaults to ``${SHARED_DIR}/hostfile.txt``).
  2. Running the pipeline once the hostfile is in place.

The hostfile path is exported back onto the pipeline so every package
that resolves ``self.hostfile`` sees the same file the job script will
populate.
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path
from typing import Any, Dict, List, Optional


def make_scheduler(spec: Dict[str, Any], pipeline_shared_dir: Path,
                   pipeline_yaml: Optional[str] = None,
                   pipeline_name: Optional[str] = None) -> "Scheduler":
    """Factory: build a Scheduler from a parsed YAML dict.

    :param spec: ``scheduler:`` block from the pipeline YAML
    :param pipeline_shared_dir: pipeline-level shared directory
        (used to default the hostfile path and to drop the job script)
    :param pipeline_yaml: path to the pipeline YAML the job should run
        (None when invoked from a pre-loaded current pipeline)
    :param pipeline_name: pipeline name (used for ``jarvis ppl run`` when
        no YAML path is supplied)
    """
    name = (spec or {}).get('name')
    if not name:
        raise ValueError("scheduler.name is required (e.g. 'slurm')")

    name = str(name).strip().lower()
    cls = _SCHEDULERS.get(name)
    if cls is None:
        supported = ', '.join(sorted(_SCHEDULERS))
        raise ValueError(
            f"Unsupported scheduler '{name}'. Supported: {supported}")
    return cls(spec, pipeline_shared_dir,
               pipeline_yaml=pipeline_yaml,
               pipeline_name=pipeline_name)


class Scheduler:
    """Base class for batch-scheduler integrations."""

    #: scheduler name as it appears in YAML (``slurm``, ``pbs``, ...)
    NAME: str = ""

    #: filename of the generated submission script inside the pipeline
    #: shared directory
    SCRIPT_NAME: str = "submit.sh"

    def __init__(self, spec: Dict[str, Any], pipeline_shared_dir: Path,
                 pipeline_yaml: Optional[str] = None,
                 pipeline_name: Optional[str] = None):
        self.spec = dict(spec or {})
        self.shared_dir = Path(pipeline_shared_dir)
        self.pipeline_yaml = pipeline_yaml
        self.pipeline_name = pipeline_name

        default_hostfile = str(self.shared_dir / 'hostfile.txt')
        hostfile = self.spec.get('hostfile') or default_hostfile
        self.hostfile = os.path.expandvars(hostfile)

    @property
    def script_path(self) -> Path:
        return self.shared_dir / self.SCRIPT_NAME

    def render(self) -> str:
        """Render the submission script as a string."""
        raise NotImplementedError

    def write_script(self) -> Path:
        """Render the submission script and write it to the shared dir."""
        self.shared_dir.mkdir(parents=True, exist_ok=True)
        path = self.script_path
        path.write_text(self.render())
        path.chmod(0o755)
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
        if self.pipeline_yaml:
            return f"jarvis ppl run yaml {shlex.quote(self.pipeline_yaml)}"
        if self.pipeline_name:
            return (f"jarvis cd {shlex.quote(self.pipeline_name)} && "
                    f"jarvis ppl run")
        return "jarvis ppl run"


class SlurmScheduler(Scheduler):
    """SLURM (sbatch) backend."""

    NAME = "slurm"
    SCRIPT_NAME = "submit.slurm"

    #: keys in the scheduler block we consume directly; everything else is
    #: treated as a long-form ``--<key>=<value>`` sbatch directive so users
    #: can pass arbitrary SBATCH flags from YAML without us hard-coding
    #: every one.
    _STRUCTURAL_KEYS = {
        'name', 'hostfile', 'suffix', 'sbatch_args', 'pre_cmds', 'post_cmds',
    }

    #: explicit short-key -> SBATCH long-flag map. Anything not in here
    #: that isn't structural is passed as ``--<key>=<value>`` with
    #: underscores rewritten to dashes.
    _EXPLICIT_DIRECTIVES = {
        'job_name':        '--job-name',
        'nodes':           '--nodes',
        'ntasks':          '--ntasks',
        'ntasks_per_node': '--ntasks-per-node',
        'cpus_per_task':   '--cpus-per-task',
        'mem':             '--mem',
        'time':            '--time',
        'partition':       '--partition',
        'account':         '--account',
        'qos':             '--qos',
        'output':          '--output',
        'error':           '--error',
        'gres':            '--gres',
        'gpus':            '--gpus',
        'gpus_per_node':   '--gpus-per-node',
        'constraint':      '--constraint',
        'reservation':     '--reservation',
        'exclusive':       '--exclusive',
        'mail_user':       '--mail-user',
        'mail_type':       '--mail-type',
    }

    _PARSABLE_SUBMISSION = re.compile(
        r"^(?P<job_id>[0-9]+)(?:;(?P<cluster>[A-Za-z0-9_.-]+))?$"
    )

    def _directives(self) -> List[str]:
        lines: List[str] = []
        for key, value in self.spec.items():
            if key in self._STRUCTURAL_KEYS:
                continue
            if value is None or value is False:
                continue
            flag = self._EXPLICIT_DIRECTIVES.get(
                key, f"--{key.replace('_', '-')}")
            if value is True:
                lines.append(f"#SBATCH {flag}")
            else:
                lines.append(f"#SBATCH {flag}={value}")
        for raw in self.spec.get('sbatch_args', []) or []:
            lines.append(f"#SBATCH {raw}")
        return lines

    def render(self) -> str:
        directives = "\n".join(self._directives())
        pre = "\n".join(self.spec.get('pre_cmds', []) or [])
        post = "\n".join(self.spec.get('post_cmds', []) or [])
        hostfile = shlex.quote(self.hostfile)
        invocation = self._pipeline_invocation()

        # Build hostfile from the allocation. scontrol expands the
        # compact nodelist into one host per line; that's exactly what
        # jarvis.util.hostfile.Hostfile consumes. When `suffix:` is set,
        # append it to each hostname — used to redirect traffic onto a
        # secondary NIC (e.g. `ares-comp-1` -> `ares-comp-1-40g` for the
        # 40GbE interface).
        suffix = self.spec.get('suffix')
        if suffix:
            hostnames_cmd = (
                f"scontrol show hostnames \"$SLURM_JOB_NODELIST\" "
                f"| awk -v s={shlex.quote(str(suffix))} '{{print $0 s}}'")
        else:
            hostnames_cmd = 'scontrol show hostnames "$SLURM_JOB_NODELIST"'
        hostfile_block = (
            f'mkdir -p "$(dirname {hostfile})"\n'
            f'{hostnames_cmd} > {hostfile}\n'
            f'echo "Wrote hostfile: {hostfile}"\n'
            f'cat {hostfile}\n'
        )

        script = (
            "#!/bin/bash\n"
            f"{directives}\n"
            "\n"
            "set -euo pipefail\n"
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
            'provider': self.NAME,
            'scheduler_job_id': match.group('job_id'),
            'scheduler_cluster': match.group('cluster'),
            'identity_source': 'scheduler_submit_api',
        }


_SCHEDULERS: Dict[str, type] = {
    SlurmScheduler.NAME: SlurmScheduler,
}
