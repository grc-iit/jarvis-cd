"""Artifact semantics owned by the builtin ADIOS2 PDF Calc package."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from jarvis_cd.artifacts import (
    ArtifactLocation,
    ArtifactObservation,
    ArtifactOwnership,
    ArtifactRole,
    ArtifactState,
    ArtifactStructure,
    new_artifact_id,
)
from jarvis_cd.progress import LineBuffer

_WRITER_RE = re.compile(
    r"^PDF analysis writes using engine type:\s*(?P<engine>[A-Za-z0-9_+-]+)\s*$"
)


@dataclass
class Adios2PdfCalcArtifactAdapter:
    """Track the configured PDF analysis dataset through process completion."""

    output_path: PurePosixPath
    engine: str
    bins: int
    artifact_id: str = field(default_factory=new_artifact_id)
    _lines: LineBuffer = field(default_factory=LineBuffer)
    _announced: bool = False
    _terminal: bool = False

    def observe_artifacts(self, text: str) -> list[ArtifactObservation]:
        """Announce output only after native PDF Calc writer initialization."""
        observations: list[ArtifactObservation] = []
        for line in self._lines.feed(text, finalize=False):
            match = _WRITER_RE.fullmatch(line.strip())
            if match is None or self._terminal or self._announced:
                continue
            if match.group("engine").casefold() != self.engine.casefold():
                raise ValueError(
                    "PDF Calc reported an engine that differs from configuration"
                )
            self._announced = True
            observations.append(self._observation(ArtifactState.PRODUCING))
        return observations

    def finalize_artifacts(self) -> list[ArtifactObservation]:
        """Finalize a successful stream for callers without process status."""
        return self.finalize_artifacts_for_exit(0)

    def finalize_artifacts_for_exit(
        self,
        return_code: int,
    ) -> list[ArtifactObservation]:
        """Finalize or mark incomplete from authoritative process status."""
        if self._terminal:
            return []
        self._lines.feed("", finalize=True)
        self._terminal = True
        state = (
            ArtifactState.FINALIZED if return_code == 0 else ArtifactState.INCOMPLETE
        )
        return [self._observation(state, return_code=return_code)]

    def reset_artifacts(self) -> None:
        """Reset parsing for a fresh PDF Calc execution."""
        self._lines.reset()
        self._announced = False
        self._terminal = False

    def _observation(
        self,
        state: ArtifactState,
        *,
        return_code: int | None = None,
    ) -> ArtifactObservation:
        metadata: dict[str, str | int] = {
            "application": "gray_scott",
            "analysis": "probability_density",
            "engine": self.engine,
            "bins": self.bins,
        }
        if return_code is not None:
            metadata["return_code"] = return_code
        if state is ArtifactState.FINALIZED:
            message = "Gray-Scott PDF analysis finalized"
        elif state is ArtifactState.INCOMPLETE:
            message = "Gray-Scott PDF analysis is incomplete after process failure"
        else:
            message = "Gray-Scott PDF analysis is being produced"
        return ArtifactObservation(
            artifact_id=self.artifact_id,
            logical_name="gray-scott-pdf-analysis",
            kind="analysis_dataset",
            role=ArtifactRole.OUTPUT,
            structure=ArtifactStructure.COLLECTION,
            ownership=ArtifactOwnership.SHARED,
            state=state,
            location=ArtifactLocation.cluster_path(self.output_path),
            media_type="application/x-adios2-bp",
            format=f"adios2-{self.engine.casefold()}-pdf",
            message=message,
            metadata=metadata,
        )


def adapter_from_package(
    package: dict[str, Any],
) -> Adios2PdfCalcArtifactAdapter | None:
    """Create artifact semantics only for builtin ADIOS2 PDF Calc."""
    if package.get("pkg_type") != "builtin.adios2_pdf_calc":
        return None
    output_path = _absolute_posix_path(package.get("output_file"))
    bins = package.get("nbins", 1000)
    if isinstance(bins, bool) or not isinstance(bins, int) or bins <= 0:
        raise ValueError("PDF Calc artifact bins must be a positive integer")
    engine = str(package.get("engine") or "bp5").casefold()
    return Adios2PdfCalcArtifactAdapter(output_path, engine, bins)


def _absolute_posix_path(value: object) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise ValueError("PDF Calc artifact output must be an absolute path")
    path = PurePosixPath(value)
    if not path.is_absolute() or path.as_posix() != value or ".." in path.parts:
        raise ValueError("PDF Calc artifact output must be a normalized absolute path")
    return path
