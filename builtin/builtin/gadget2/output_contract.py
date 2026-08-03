"""Parse confined output declarations from Gadget2 parameter files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

_REQUIRED_FIELDS = (
    "OutputDir",
    "EnergyFile",
    "InfoFile",
    "SnapshotFileBase",
)
_OPTIONAL_FIELDS = ("CpuFile", "TimingsFile")


@dataclass(frozen=True, slots=True)
class Gadget2OutputContract:
    """Output paths declared by one Gadget2 parameter file.

    Every path is relative to the root into which the input bundle is staged.
    """

    output_dir: PurePosixPath
    energy_file: PurePosixPath
    info_file: PurePosixPath
    snapshot_file_base: PurePosixPath
    cpu_file: PurePosixPath | None = None
    timings_file: PurePosixPath | None = None


def parse_gadget2_output_contract(
    text: str,
    *,
    parameter_path: PurePosixPath,
) -> Gadget2OutputContract:
    """Return the exact confined outputs declared by a parameter file.

    :param text: UTF-8 decoded Gadget2 parameter contents.
    :param parameter_path: Parameter path relative to the staged bundle root.
    :raises ValueError: If a required declaration is absent, duplicated, or unsafe.
    """

    parameter_path = _confined_path(parameter_path.as_posix(), field="parameter_path")
    values: dict[str, list[str]] = {
        name: [] for name in (*_REQUIRED_FIELDS, *_OPTIONAL_FIELDS)
    }
    for raw_line in text.splitlines():
        statement = raw_line.partition("%")[0].partition(";")[0].strip()
        if not statement:
            continue
        tokens = statement.split()
        field = tokens[0]
        if field not in values:
            continue
        if len(tokens) != 2:
            raise ValueError(f"Gadget2 {field} must contain one path token")
        values[field].append(tokens[1])

    for field in _REQUIRED_FIELDS:
        if len(values[field]) != 1:
            raise ValueError(
                f"Gadget2 parameter file must declare {field} exactly once"
            )
    for field in _OPTIONAL_FIELDS:
        if len(values[field]) > 1:
            raise ValueError(f"Gadget2 parameter file may declare {field} at most once")

    parameter_dir = parameter_path.parent
    output_dir = _join_confined(
        parameter_dir,
        values["OutputDir"][0],
        field="OutputDir",
    )

    def product(field: str) -> PurePosixPath:
        return _join_confined(output_dir, values[field][0], field=field)

    def optional_product(field: str) -> PurePosixPath | None:
        if not values[field]:
            return None
        return _join_confined(output_dir, values[field][0], field=field)

    return Gadget2OutputContract(
        output_dir=output_dir,
        energy_file=product("EnergyFile"),
        info_file=product("InfoFile"),
        snapshot_file_base=product("SnapshotFileBase"),
        cpu_file=optional_product("CpuFile"),
        timings_file=optional_product("TimingsFile"),
    )


def _join_confined(
    base: PurePosixPath,
    value: str,
    *,
    field: str,
) -> PurePosixPath:
    """Join one untrusted relative token below a previously confined base."""

    relative = _confined_path(value, field=field)
    joined = base / relative
    try:
        joined.relative_to(base)
    except ValueError as exc:
        raise ValueError(f"Gadget2 {field} escapes its declared output root") from exc
    return joined


def _confined_path(value: str, *, field: str) -> PurePosixPath:
    """Parse one bounded printable relative POSIX path."""

    if (
        not value
        or len(value) > 1024
        or "\\" in value
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError(f"Gadget2 {field} must use a confined POSIX path")
    path = PurePosixPath(value)
    if (
        not path.parts
        or path.is_absolute()
        or ".." in path.parts
        or (path.parts and ":" in path.parts[0])
    ):
        raise ValueError(f"Gadget2 {field} must use a confined POSIX path")
    return path
