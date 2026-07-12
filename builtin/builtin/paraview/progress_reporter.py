"""Python 3.10-compatible structured reporter for standalone pvbatch scripts.

This file intentionally imports no JARVIS modules. ParaView bundles its own
Python runtime, which can be older than the Python used to run JARVIS itself.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from typing import Any, Dict, Optional, TextIO

PROGRESS_SCHEMA_VERSION = "jarvis.progress.v1"
PROGRESS_LINE_PREFIX = "JARVIS_PROGRESS "
PROGRESS_PATH_ENV = "JARVIS_PROGRESS_PATH"
PROGRESS_TRANSPORT_ENV = "JARVIS_PROGRESS_TRANSPORT"
EXECUTION_ID_ENV = "JARVIS_EXECUTION_ID"
PACKAGE_NAME_ENV = "JARVIS_PACKAGE_NAME"
PACKAGE_ID_ENV = "JARVIS_PACKAGE_ID"
MAX_PROGRESS_EVENT_BYTES = 64 * 1024
_MPI_RANK_ENV = (
    "OMPI_COMM_WORLD_RANK",
    "PMI_RANK",
    "PMIX_RANK",
    "SLURM_PROCID",
    "MV2_COMM_WORLD_RANK",
)


class ParaViewProgressReporter:
    """Report completed ParaView units without estimating percentages."""

    def __init__(
        self,
        package_name: Optional[str] = None,
        package_id: Optional[str] = None,
        execution_id: Optional[str] = None,
        path: Optional[str] = None,
        stream: Optional[TextIO] = None,
        rank: Optional[int] = None,
    ) -> None:
        self.package_name = package_name or os.environ.get(PACKAGE_NAME_ENV, "")
        self.package_id = package_id or os.environ.get(PACKAGE_ID_ENV, "")
        self.execution_id = execution_id or os.environ.get(EXECUTION_ID_ENV, "")
        configured_path = (
            path if path is not None else os.environ.get(PROGRESS_PATH_ENV)
        )
        transport = os.environ.get(PROGRESS_TRANSPORT_ENV)
        if configured_path and transport != "stdout":
            raise ValueError(
                "standalone pvbatch progress must use stdout so JARVIS owns "
                "sidecar validation and persistence"
            )
        self.stream = stream or sys.stdout
        self.rank = _progress_rank(rank)
        self.enabled = self.rank is None or self.rank == 0
        self.sequence = 0
        if not self.package_name or not self.package_id or not self.execution_id:
            raise ValueError(
                "ParaView progress requires JARVIS_PACKAGE_NAME, "
                "JARVIS_PACKAGE_ID, and JARVIS_EXECUTION_ID"
            )

    def frame_completed(
        self,
        completed_frames: int,
        total_frames: Optional[int] = None,
        timestep: Optional[float] = None,
        output_path: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Report a frame after the script's render/write call has returned."""
        metadata: Dict[str, Any] = {
            "progress_kind": "pvbatch_completed_unit",
            "renderer": "paraview",
            "completion_signal": "render_returned",
            "completed_after_render": True,
        }
        if timestep is not None:
            if not math.isfinite(timestep):
                raise ValueError("ParaView timestep must be finite")
            metadata["timestep"] = timestep
        if output_path:
            metadata["output_path"] = output_path
        return self._completed_unit(
            label="frame",
            completed=completed_frames,
            total=total_frames,
            metadata=metadata,
        )

    def timestep_completed(
        self,
        completed_timesteps: int,
        total_timesteps: Optional[int] = None,
        timestep: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """Report a timestep after the script's pipeline update has returned."""
        metadata: Dict[str, Any] = {
            "progress_kind": "pvbatch_completed_unit",
            "renderer": "paraview",
            "completion_signal": "pipeline_update_returned",
            "completed_after_update": True,
        }
        if timestep is not None:
            if not math.isfinite(timestep):
                raise ValueError("ParaView timestep must be finite")
            metadata["timestep"] = timestep
        return self._completed_unit(
            label="timestep",
            completed=completed_timesteps,
            total=total_timesteps,
            metadata=metadata,
        )

    def _completed_unit(
        self,
        label: str,
        completed: int,
        total: Optional[int],
        metadata: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        if isinstance(completed, bool) or completed < 1:
            raise ValueError("completed ParaView units must be a positive integer")
        if total is not None:
            if isinstance(total, bool) or total < 1:
                raise ValueError("total ParaView units must be a positive integer")
            if completed > total:
                raise ValueError("completed ParaView units cannot exceed total")
        return self._emit(
            label=label,
            state="completed"
            if total is not None and completed == total
            else "running",
            current=float(completed),
            total=float(total) if total is not None else None,
            unit=label,
            message=(
                f"ParaView completed {label} {completed}"
                if total is None
                else f"ParaView completed {label} {completed} of {total}"
            ),
            metadata=metadata,
        )

    def _emit(
        self,
        label: str,
        state: str,
        current: Optional[float],
        total: Optional[float],
        unit: Optional[str],
        message: str,
        metadata: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        if not self.enabled:
            return None
        self.sequence += 1
        event: Dict[str, Any] = {
            "schema_version": PROGRESS_SCHEMA_VERSION,
            "package_name": self.package_name,
            "package_id": self.package_id,
            "execution_id": self.execution_id,
            "label": label,
            "state": state,
            "sequence": self.sequence,
            "observed_at_epoch": time.time(),
            "determinate": total is not None,
            "message": message,
            "metadata": metadata,
        }
        if current is not None:
            event["current"] = current
        if total is not None:
            event["total"] = total
        if unit is not None:
            event["unit"] = unit
        payload = json.dumps(
            event,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        encoded = (payload + "\n").encode("utf-8")
        if len(encoded) > MAX_PROGRESS_EVENT_BYTES:
            raise ValueError("ParaView progress event exceeds maximum encoded size")
        self.stream.write(PROGRESS_LINE_PREFIX + payload + "\n")
        self.stream.flush()
        return event


def _progress_rank(explicit_rank: Optional[int]) -> Optional[int]:
    """Resolve one non-negative MPI rank without guessing across conflicts."""
    if explicit_rank is not None:
        if (
            isinstance(explicit_rank, bool)
            or not isinstance(explicit_rank, int)
            or explicit_rank < 0
        ):
            raise ValueError("ParaView progress rank must be a non-negative integer")
        return explicit_rank
    observed = set()
    for name in _MPI_RANK_ENV:
        value = os.environ.get(name)
        if value is None:
            continue
        try:
            parsed = int(value, 10)
        except ValueError as exc:
            raise ValueError(f"invalid ParaView MPI rank in {name}: {value!r}") from exc
        if parsed < 0:
            raise ValueError(f"invalid ParaView MPI rank in {name}: {value!r}")
        observed.add(parsed)
    if len(observed) > 1:
        raise ValueError("conflicting ParaView MPI rank environment values")
    return next(iter(observed)) if observed else None
