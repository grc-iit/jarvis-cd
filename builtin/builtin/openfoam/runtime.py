"""Process-output bridge for the multi-process OpenFOAM package."""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path


def _is_executable_file(path: Path) -> bool:
    try:
        if not path.is_file() or path.parent.name != "bin":
            return False
        return os.name == "nt" or bool(path.stat().st_mode & 0o111)
    except OSError:
        return False


def resolve_openfoam_environment(
    environment: Mapping[str, str],
    *,
    executable: Path | None = None,
) -> dict[str, str]:
    """Return an OpenFOAM environment with a verified global configuration root."""

    resolved = dict(environment)
    candidates: list[Path] = []
    configured_etc = resolved.get("FOAM_ETC")
    if configured_etc:
        candidates.append(Path(configured_etc))
    project_dir = resolved.get("WM_PROJECT_DIR")
    if project_dir:
        candidates.append(Path(project_dir) / "etc")

    executable_path = executable
    if executable_path is None:
        located = shutil.which("decomposePar", path=resolved.get("PATH"))
        if located:
            executable_path = Path(located)
    if executable_path is not None:
        canonical = executable_path.resolve()
        candidates.extend(parent / "etc" for parent in canonical.parents)

    for candidate in candidates:
        canonical_candidate = candidate.resolve()
        if (canonical_candidate / "controlDict").is_file():
            resolved["FOAM_ETC"] = str(canonical_candidate)
            resolved["WM_PROJECT_DIR"] = str(canonical_candidate.parent)
            if executable_path is not None:
                resolved["FOAM_APPBIN"] = str(executable_path.resolve().parent)
            return resolved

    raise RuntimeError(
        "OpenFOAM runtime does not expose a global etc/controlDict through "
        "FOAM_ETC, WM_PROJECT_DIR, or the decomposePar installation prefix"
    )


def resolve_openfoam_executable(environment: Mapping[str, str], name: str) -> Path:
    """Resolve one OpenFOAM executable from PATH or the verified project tree."""

    located = shutil.which(name, path=environment.get("PATH"))
    if located:
        return Path(located).resolve()

    application_bin = environment.get("FOAM_APPBIN")
    if application_bin:
        candidate = Path(application_bin) / name
        if _is_executable_file(candidate):
            return candidate.resolve()

    project_dir = environment.get("WM_PROJECT_DIR")
    if project_dir:
        project = Path(project_dir).resolve()
        try:
            matches = sorted(
                (
                    path.resolve()
                    for path in project.rglob(name)
                    if _is_executable_file(path)
                ),
                key=lambda path: path.as_posix(),
            )
        except OSError as error:
            raise RuntimeError(
                f"OpenFOAM executable search failed for {name}"
            ) from error
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            options = environment.get("WM_OPTIONS")
            selected = [path for path in matches if options and options in path.parts]
            if len(selected) == 1:
                return selected[0]
            raise RuntimeError(f"OpenFOAM executable is ambiguous: {name}")

    raise RuntimeError(f"OpenFOAM executable is unavailable: {name}")


class DeferredRuntimeCallback:
    """Forward lines while deferring successful finalization to the final marker."""

    def __init__(
        self,
        delegate: Callable[[str, str], None] | None,
        *,
        terminal: bool,
    ) -> None:
        self._delegate = delegate
        self._terminal = terminal

    def __call__(self, stream_name: str, line: str) -> None:
        """Forward captured OpenFOAM output."""

        if self._delegate is not None:
            self._delegate(stream_name, line)

    def finalize_process(self, return_code: int) -> None:
        """Finalize only a failure or the declared terminal command."""

        if self._delegate is not None and (self._terminal or return_code != 0):
            finalizer = getattr(self._delegate, "finalize_process", None)
            if callable(finalizer):
                finalizer(return_code)

    def reconcile_process_exit(self, return_code: int) -> None:
        """Forward effective failure reconciliation unchanged."""

        if self._delegate is not None:
            reconciler = getattr(self._delegate, "reconcile_process_exit", None)
            if callable(reconciler):
                reconciler(return_code)
