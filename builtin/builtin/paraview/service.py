"""Real ParaView backend for the generic JARVIS HTTP/SSE service."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import math
import os
import secrets
import signal
import stat
import tempfile
import threading
import time
from contextlib import contextmanager
from http import HTTPStatus
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple, cast

try:
    from .service_http import CommandError, ServiceStateController, create_server
except ImportError:  # Staged pvpython scripts are executed outside a package.
    from service_http import CommandError, ServiceStateController, create_server

MAX_FILTERS = 16
MAX_ARTIFACTS = 128
MAX_SELECTION_ELEMENTS = 100_000_000_000
MAX_SELECTION_RESULTS = 256
MAX_EXPORTED_ARTIFACT_BYTES = 128 * 1024 * 1024
LIVE_VIEW_SIZE = (960, 540)
ARTIFACT_PREFIX = "JARVIS_ARTIFACT "


class ParaViewBackend:
    """Drive a real ``paraview.simple`` pipeline from semantic commands."""

    def __init__(
        self,
        *,
        descriptor: Mapping[str, Any],
        output_dir: Path,
        service_instance_id: str,
    ) -> None:
        """Open the descriptor members and initialize an automatic view."""
        try:
            servermanager = importlib.import_module("paraview.servermanager")
            simple = importlib.import_module("paraview.simple")
            vtk = importlib.import_module("paraview.vtk")
        except ImportError as exc:
            raise RuntimeError(
                "ParaView service mode requires pvpython with paraview.simple"
            ) from exc
        self.simple = simple
        self.servermanager = servermanager
        self.vtk = vtk
        self.descriptor = _validate_descriptor(descriptor)
        self.output_dir = output_dir.resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            self.output_dir.chmod(0o700)
        self.service_instance_id = service_instance_id
        locations = [member["location"] for member in self.descriptor["members"]]
        missing = [location for location in locations if not Path(location).exists()]
        if missing:
            raise FileNotFoundError(
                "dataset descriptor member does not exist: " + missing[0]
            )
        source_input: object = locations[0] if len(locations) == 1 else locations
        self.reader = simple.OpenDataFile(source_input)
        if self.reader is None:
            raise RuntimeError("ParaView could not open the dataset descriptor members")
        self.reader.UpdatePipeline()
        self.active_source = self.reader
        self.view = simple.GetActiveViewOrCreate("RenderView")
        self.view.ViewSize = list(LIVE_VIEW_SIZE)
        self.display = simple.Show(self.active_source, self.view)
        simple.ResetCamera(self.view)
        simple.Render(self.view)
        self._filters: List[Dict[str, Any]] = []
        self._active_field: Optional[Dict[str, Any]] = None
        self._colormap: Optional[Dict[str, Any]] = None
        self._selection: Optional[Dict[str, Any]] = None
        self._artifacts: List[Dict[str, Any]] = []
        self._arrays = self._discover_arrays()
        self._bounds = self._discover_bounds()
        self._timesteps = self._discover_timesteps()
        self._timestep_index = 0

    def dataset_state(self) -> Dict[str, Any]:
        """Return immutable identity plus facts discovered from ParaView."""
        return {
            "descriptor": _json_copy(self.descriptor),
            "discovery": {
                "arrays": _json_copy_list(self._arrays),
                "bounds": list(self._bounds) if self._bounds is not None else None,
                "timestep_values": list(self._timesteps),
            },
        }

    def pipeline_state(self) -> Dict[str, Any]:
        """Return the exact state currently applied to the ParaView pipeline."""
        current_value = (
            self._timesteps[self._timestep_index] if self._timesteps else None
        )
        return {
            "timestep": {
                "index": self._timestep_index,
                "value": current_value,
                "count": len(self._timesteps),
            },
            "active_field": _json_copy_optional(self._active_field),
            "filters": _json_copy_list(self._filters),
            "colormap": _json_copy_optional(self._colormap),
            "camera": self._camera_state(),
            "selection": _json_copy_optional(self._selection),
            "artifacts": _json_copy_list(self._artifacts),
        }

    def execute(
        self,
        operation: str,
        arguments: Mapping[str, Any],
        command_id: str,
    ) -> Dict[str, Any]:
        """Apply one allowlisted operation to real ParaView state."""
        handlers = {
            "set_timestep": self._set_timestep,
            "set_active_field": self._set_active_field,
            "set_camera": self._set_camera,
            "apply_filter": self._apply_filter,
            "set_colormap": self._set_colormap,
            "inspect_selection": self._inspect_selection,
            "export_artifact": self._export_artifact,
        }
        handler = handlers.get(operation)
        if handler is None:
            raise CommandError("unsupported_operation", "operation is not supported")
        return handler(arguments, command_id)

    def render_png(self) -> bytes:
        """Render current state to a bounded temporary PNG."""
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.output_dir,
            prefix=".live-frame.",
            suffix=".png",
        )
        os.close(descriptor)
        path = Path(temporary_name)
        try:
            self.simple.SaveScreenshot(
                str(path),
                self.view,
                ImageResolution=list(LIVE_VIEW_SIZE),
            )
            payload = path.read_bytes()
            if len(payload) > 32 * 1024 * 1024:
                raise RuntimeError("rendered live frame exceeds 32 MiB")
            return payload
        finally:
            path.unlink(missing_ok=True)

    def _set_timestep(
        self,
        arguments: Mapping[str, Any],
        _command_id: str,
    ) -> Dict[str, Any]:
        _require_fields(arguments, {"index"}, "set_timestep")
        index = _bounded_int(arguments.get("index"), "index", minimum=0)
        if not self._timesteps:
            if index != 0:
                raise CommandError(
                    "timestep_out_of_range",
                    "static datasets only expose timestep index 0",
                )
            value = None
        else:
            if index >= len(self._timesteps):
                raise CommandError(
                    "timestep_out_of_range",
                    "timestep index exceeds the discovered series",
                )
            value = self._timesteps[index]
            self.view.ViewTime = value
            self.reader.UpdatePipeline(value)
            self.active_source.UpdatePipeline(value)
        self._timestep_index = index
        self.simple.Render(self.view)
        return {"timestep": {"index": index, "value": value}}

    def _set_active_field(
        self,
        arguments: Mapping[str, Any],
        _command_id: str,
    ) -> Dict[str, Any]:
        _require_fields(arguments, {"name", "association"}, "set_active_field")
        name = _bounded_text(arguments.get("name"), "name", maximum=512)
        association = _association(arguments.get("association"))
        match = next(
            (
                array
                for array in self._arrays
                if array["name"] == name and array["association"] == association
            ),
            None,
        )
        if match is None:
            raise CommandError(
                "field_not_found",
                "active field is not present in discovered ParaView arrays",
            )
        paraview_association = "POINTS" if association == "point" else "CELLS"
        self.simple.ColorBy(self.display, (paraview_association, name))
        self.display.RescaleTransferFunctionToDataRange(True, False)
        active_field = {
            "name": name,
            "association": association,
            "components": match["components"],
        }
        self.simple.Render(self.view)
        self._active_field = active_field
        # A transfer-function preset is bound to the previously active array.
        # Changing arrays must not claim that preset is still applied.
        self._colormap = None
        return {"active_field": dict(self._active_field)}

    def _set_camera(
        self,
        arguments: Mapping[str, Any],
        _command_id: str,
    ) -> Dict[str, Any]:
        allowed = {"position", "focal_point", "view_up", "parallel_scale"}
        if not arguments or set(arguments) - allowed:
            raise CommandError(
                "invalid_arguments",
                "set_camera accepts position, focal_point, view_up, and parallel_scale",
            )
        previous = self._camera_state()
        candidate = _json_copy(previous)
        if "position" in arguments:
            candidate["position"] = _vector3(arguments["position"], "position")
        if "focal_point" in arguments:
            candidate["focal_point"] = _vector3(arguments["focal_point"], "focal_point")
        if "view_up" in arguments:
            candidate["view_up"] = _vector3(arguments["view_up"], "view_up")
        if "parallel_scale" in arguments:
            scale = _finite_number(arguments["parallel_scale"], "parallel_scale")
            if scale <= 0:
                raise CommandError(
                    "invalid_arguments",
                    "parallel_scale must be positive",
                )
            candidate["parallel_scale"] = scale
        _validate_camera_geometry(candidate)
        try:
            _apply_camera_state(self.view, candidate)
            self.simple.Render(self.view)
        except Exception as exc:
            try:
                _apply_camera_state(self.view, previous)
                self.simple.Render(self.view)
            except Exception as rollback_error:
                raise RuntimeError(
                    "set_camera failed and the previous camera could not be restored: "
                    + str(rollback_error)
                ) from exc
            raise
        return {"camera": self._camera_state()}

    def _apply_filter(
        self,
        arguments: Mapping[str, Any],
        command_id: str,
    ) -> Dict[str, Any]:
        _require_fields(arguments, {"type", "parameters"}, "apply_filter")
        if len(self._filters) >= MAX_FILTERS:
            raise CommandError("filter_limit", "the service filter limit was reached")
        filter_type = _bounded_text(arguments.get("type"), "type", maximum=64)
        parameters = arguments.get("parameters")
        if not isinstance(parameters, dict):
            raise CommandError(
                "invalid_arguments", "filter parameters must be an object"
            )
        origin: Optional[List[float]] = None
        normal: Optional[List[float]] = None
        threshold_name: Optional[str] = None
        threshold_association: Optional[str] = None
        threshold_lower: Optional[float] = None
        threshold_upper: Optional[float] = None
        if filter_type == "slice":
            _require_fields(parameters, {"origin", "normal"}, "slice")
            origin = _vector3(parameters["origin"], "origin")
            normal = _nonzero_vector3(parameters["normal"], "normal")
            recorded_parameters = {
                "origin": list(origin),
                "normal": list(normal),
            }
        elif filter_type == "clip":
            _require_fields(parameters, {"origin", "normal"}, "clip")
            origin = _vector3(parameters["origin"], "origin")
            normal = _nonzero_vector3(parameters["normal"], "normal")
            recorded_parameters = {
                "origin": list(origin),
                "normal": list(normal),
            }
        elif filter_type == "threshold":
            _require_fields(
                parameters,
                {"name", "association", "lower", "upper"},
                "threshold",
            )
            threshold_name = _bounded_text(parameters.get("name"), "name", maximum=512)
            threshold_association = _association(parameters.get("association"))
            threshold_lower = _finite_number(parameters.get("lower"), "lower")
            threshold_upper = _finite_number(parameters.get("upper"), "upper")
            if threshold_lower > threshold_upper:
                raise CommandError(
                    "invalid_arguments", "threshold lower cannot exceed upper"
                )
            if not any(
                array["name"] == threshold_name
                and array["association"] == threshold_association
                for array in self._arrays
            ):
                raise CommandError(
                    "field_not_found", "threshold field was not discovered"
                )
            recorded_parameters = {
                "name": threshold_name,
                "association": threshold_association,
                "lower": threshold_lower,
                "upper": threshold_upper,
            }
        else:
            raise CommandError(
                "unsupported_filter",
                "filter type must be slice, clip, or threshold",
            )
        filter_id = "flt_" + hashlib.sha256(command_id.encode("utf-8")).hexdigest()[:24]
        record = {
            "filter_id": filter_id,
            "type": filter_type,
            "parameters": recorded_parameters,
        }
        previous_source = self.active_source
        previous_camera = self._camera_state()
        proxy: Any = None
        new_display: Any = None
        try:
            if filter_type == "slice":
                assert origin is not None and normal is not None
                proxy = self.simple.Slice(Input=previous_source)
                proxy.SliceType.Origin = origin
                proxy.SliceType.Normal = normal
            elif filter_type == "clip":
                assert origin is not None and normal is not None
                proxy = self.simple.Clip(Input=previous_source)
                proxy.ClipType.Origin = origin
                proxy.ClipType.Normal = normal
            else:
                assert (
                    threshold_name is not None
                    and threshold_association is not None
                    and threshold_lower is not None
                    and threshold_upper is not None
                )
                proxy = self.simple.Threshold(Input=previous_source)
                proxy.Scalars = [
                    "POINTS" if threshold_association == "point" else "CELLS",
                    threshold_name,
                ]
                proxy.LowerThreshold = threshold_lower
                proxy.UpperThreshold = threshold_upper
            proxy.UpdatePipeline()
            # Inspect the derived output while keeping ``dataset.discovery`` bound to
            # the opened source descriptor. Filter state belongs to ``pipeline``;
            # replacing source arrays or bounds here makes dataset identity drift and
            # turns a geometric slice into an apparent dataset mutation.
            self._discover_arrays(proxy)
            self._discover_bounds(proxy)
            new_display = self.simple.Show(proxy, self.view)
            self.simple.Hide(previous_source, self.view)
            _apply_camera_state(self.view, previous_camera)
            self.simple.Render(self.view)
        except Exception as exc:
            try:
                if proxy is not None and new_display is not None:
                    self.simple.Hide(proxy, self.view)
                self.simple.Show(previous_source, self.view)
                if proxy is not None:
                    self.simple.Delete(proxy)
                _apply_camera_state(self.view, previous_camera)
                self.simple.Render(self.view)
            except Exception as rollback_error:
                raise RuntimeError(
                    "apply_filter failed and the previous pipeline could not be restored: "
                    + str(rollback_error)
                ) from exc
            raise
        self.active_source = proxy
        self.display = new_display
        self._filters.append(record)
        self._active_field = None
        self._colormap = None
        self._selection = None
        return {"filter": _json_copy(record)}

    def _set_colormap(
        self,
        arguments: Mapping[str, Any],
        _command_id: str,
    ) -> Dict[str, Any]:
        _require_fields(arguments, {"preset", "invert"}, "set_colormap")
        if self._active_field is None:
            raise CommandError(
                "active_field_required",
                "set_colormap requires an active scalar field",
            )
        preset = _bounded_text(arguments.get("preset"), "preset", maximum=256)
        invert = arguments.get("invert")
        if not isinstance(invert, bool):
            raise CommandError("invalid_arguments", "invert must be boolean")
        lookup = self.simple.GetColorTransferFunction(self._active_field["name"])
        if not lookup.ApplyPreset(preset, True):
            raise CommandError(
                "preset_not_found", "ParaView colormap preset was not found"
            )
        if invert:
            lookup.InvertTransferFunction()
        self.display.RescaleTransferFunctionToDataRange(True, False)
        self._colormap = {"preset": preset, "invert": invert}
        self.simple.Render(self.view)
        return {"colormap": dict(self._colormap)}

    def _inspect_selection(
        self,
        arguments: Mapping[str, Any],
        _command_id: str,
    ) -> Dict[str, Any]:
        if set(arguments) == {"association", "index"}:
            return self._inspect_element_selection(arguments)
        if set(arguments) == {"viewport"}:
            return self._inspect_viewport_selection(arguments.get("viewport"))
        raise CommandError(
            "invalid_arguments",
            "inspect_selection requires either association/index or viewport",
        )

    def _inspect_element_selection(
        self,
        arguments: Mapping[str, Any],
    ) -> Dict[str, Any]:
        """Select one real point or cell by its ParaView element index."""
        association = _association(arguments.get("association"))
        index = _bounded_int(
            arguments.get("index"),
            "index",
            minimum=0,
            maximum=MAX_SELECTION_ELEMENTS,
        )
        information = self.active_source.GetDataInformation()
        count = (
            information.GetNumberOfPoints()
            if association == "point"
            else information.GetNumberOfCells()
        )
        if index >= count:
            raise CommandError(
                "selection_out_of_range",
                "selection index exceeds the real ParaView element count",
            )
        field_type = "POINT" if association == "point" else "CELL"
        self.simple.SelectIDs(
            IDs=[0, index],
            FieldType=field_type,
            Source=self.active_source,
        )
        self._selection = {
            "selector": "element",
            "status": "selected",
            "association": association,
            "index": index,
            "element_count": count,
            "selected_count": 1,
            "returned_count": 1,
            "truncated": False,
            "ids": [{"process_id": 0, "element_id": index}],
            "reason": None,
        }
        self.simple.Render(self.view)
        return {"selection": dict(self._selection)}

    def _inspect_viewport_selection(self, value: object) -> Dict[str, Any]:
        """Select visible cells through ParaView's real render-view picker."""
        viewport = _viewport(value)
        pixel_rectangle = _viewport_pixel_rectangle(viewport, LIVE_VIEW_SIZE)
        selected_representations = self.vtk.vtkCollection()
        selection_sources = self.vtk.vtkCollection()
        try:
            self.simple.Render(self.view)
            self.view.SelectSurfaceCells(
                pixel_rectangle,
                selected_representations,
                selection_sources,
                0,
            )
            ids, selected_count, unsupported_reason = _surface_selection_ids(
                selected_representations=selected_representations,
                selection_sources=selection_sources,
                active_source=self.active_source,
                servermanager=self.servermanager,
                limit=MAX_SELECTION_RESULTS,
            )
            self.simple.SelectSurfaceCells(
                Rectangle=pixel_rectangle,
                View=self.view,
            )
            self.simple.Render(self.view)
        except (AttributeError, RuntimeError, TypeError, ValueError):
            ids = []
            selected_count = None
            unsupported_reason = "paraview_surface_selection_unavailable"

        if unsupported_reason is not None:
            status = "unsupported"
        elif selected_count == 0:
            status = "empty"
        else:
            status = "selected"
        self._selection = {
            "selector": "viewport",
            "status": status,
            "association": "cell",
            "viewport": viewport,
            "pixel_rectangle": pixel_rectangle,
            "selected_count": selected_count,
            "returned_count": len(ids),
            "truncated": (selected_count is not None and selected_count > len(ids)),
            "ids": ids,
            "reason": unsupported_reason,
        }
        return {"selection": _json_copy(self._selection)}

    def _export_artifact(
        self,
        arguments: Mapping[str, Any],
        command_id: str,
    ) -> Dict[str, Any]:
        _require_fields(
            arguments,
            {"filename", "width", "height"},
            "export_artifact",
        )
        filename = _safe_relative_png(arguments.get("filename"))
        width = _bounded_int(arguments.get("width"), "width", minimum=64, maximum=4096)
        height = _bounded_int(
            arguments.get("height"), "height", minimum=64, maximum=4096
        )
        if len(self._artifacts) >= MAX_ARTIFACTS:
            raise CommandError(
                "artifact_limit", "the service artifact limit was reached"
            )
        candidate_path = self.output_dir / filename
        output_parent = candidate_path.parent.resolve()
        if (
            output_parent != self.output_dir
            and self.output_dir not in output_parent.parents
        ):
            raise CommandError("invalid_arguments", "artifact path escapes output root")
        # Resolve parent directories, but preserve the leaf itself so a caller
        # cannot smuggle a symlink target through Path.resolve().
        output_path = output_parent / candidate_path.name
        payload = _write_unique_png(
            simple=self.simple,
            view=self.view,
            output_path=output_path,
            width=width,
            height=height,
        )
        digest = hashlib.sha256(payload).hexdigest()
        artifact_id = "art_" + secrets.token_urlsafe(18)
        try:
            artifact = _append_artifact(
                artifact_id=artifact_id,
                logical_name=filename.as_posix(),
                path=output_path,
                size_bytes=len(payload),
                sha256=digest,
                service_instance_id=self.service_instance_id,
                command_id=command_id,
            )
        except Exception as exc:
            try:
                output_path.unlink()
                _fsync_directory(output_path.parent)
            except OSError as cleanup_error:
                raise RuntimeError(
                    "artifact publication failed and the exported PNG could not be removed: "
                    + str(cleanup_error)
                ) from exc
            raise
        self._artifacts.append(artifact)
        return {"artifact": _json_copy(artifact)}

    def _discover_arrays(self, source: Any = None) -> List[Dict[str, Any]]:
        selected_source = self.active_source if source is None else source
        information = selected_source.GetDataInformation()
        arrays: List[Dict[str, Any]] = []
        for association, attribute_information in (
            ("point", information.GetPointDataInformation()),
            ("cell", information.GetCellDataInformation()),
        ):
            for index in range(min(attribute_information.GetNumberOfArrays(), 256)):
                array = attribute_information.GetArrayInformation(index)
                if array is None or not array.GetName():
                    continue
                arrays.append(
                    {
                        "name": str(array.GetName()),
                        "association": association,
                        "components": int(array.GetNumberOfComponents()),
                        "units": None,
                    }
                )
        return arrays

    def _discover_bounds(
        self,
        source: Any = None,
    ) -> Optional[Tuple[float, float, float, float, float, float]]:
        selected_source = self.active_source if source is None else source
        values = selected_source.GetDataInformation().GetBounds()
        if values is None or len(values) != 6:
            return None
        bounds = tuple(float(value) for value in values)
        if not all(math.isfinite(value) for value in bounds):
            return None
        return cast(Tuple[float, float, float, float, float, float], bounds)

    def _discover_timesteps(self) -> List[float]:
        values = getattr(self.reader, "TimestepValues", None)
        if values:
            return [float(value) for value in values]
        descriptor_values = [
            member.get("timestep")
            for member in self.descriptor["members"]
            if member.get("timestep") is not None
        ]
        return [float(value) for value in descriptor_values]

    def _camera_state(self) -> Dict[str, Any]:
        return {
            "position": [float(value) for value in self.view.CameraPosition],
            "focal_point": [float(value) for value in self.view.CameraFocalPoint],
            "view_up": [float(value) for value in self.view.CameraViewUp],
            "parallel_scale": float(self.view.CameraParallelScale),
        }


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the real ParaView service until a scheduler or operator stops it."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--descriptor", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--bind-host", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--execution-id", required=True)
    parser.add_argument("--service-instance-id", required=True)
    args = parser.parse_args(argv)
    descriptor_path = Path(args.descriptor)
    descriptor = _load_json_file(descriptor_path)
    backend = ParaViewBackend(
        descriptor=descriptor,
        output_dir=Path(args.output_dir),
        service_instance_id=args.service_instance_id,
    )
    controller = ServiceStateController(
        backend=backend,
        execution_id=args.execution_id,
        service_instance_id=args.service_instance_id,
    )
    server = create_server(args.bind_host, args.port, controller)

    def stop(_signum: int, _frame: object) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
    return 0


def _validate_descriptor(value: Mapping[str, Any]) -> Dict[str, Any]:
    expected = {
        "schema_version",
        "dataset_id",
        "kind",
        "format",
        "members",
        "arrays",
        "bounds",
        "fingerprint",
        "source_artifact",
    }
    if set(value) != expected or value.get("schema_version") != (
        "jarvis.dataset-descriptor.v1"
    ):
        raise ValueError("dataset descriptor does not match the JARVIS schema")
    forbidden = {
        "camera",
        "threshold",
        "filter",
        "filters",
        "colormap",
        "scene",
        "recipe",
        "active_field",
    }
    if set(value) & forbidden:
        raise ValueError("dataset descriptor cannot contain visualization choices")
    members = value.get("members")
    if not isinstance(members, list) or not 1 <= len(members) <= 512:
        raise ValueError("dataset descriptor requires 1-512 members")
    for expected_index, member in enumerate(members):
        if not isinstance(member, dict) or set(member) - {
            "index",
            "location",
            "timestep",
        }:
            raise ValueError("dataset member schema is invalid")
        if member.get("index") != expected_index:
            raise ValueError("dataset member indexes must be contiguous")
        _normalized_absolute_path(member.get("location"))
    fingerprint = value.get("fingerprint")
    if (
        not isinstance(fingerprint, dict)
        or set(fingerprint) != {"algorithm", "digest"}
        or fingerprint.get("algorithm") != "sha256"
        or not isinstance(fingerprint.get("digest"), str)
    ):
        raise ValueError("dataset descriptor fingerprint is invalid")
    intrinsic = {key: item for key, item in value.items() if key != "fingerprint"}
    calculated = hashlib.sha256(
        json.dumps(
            intrinsic,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    if fingerprint["digest"] != calculated:
        raise ValueError(
            "dataset fingerprint does not match the canonical intrinsic descriptor"
        )
    return _json_copy(value)


def _load_json_file(path: Path) -> Dict[str, Any]:
    if not path.is_file() or path.stat().st_size > 256 * 1024:
        raise ValueError("dataset descriptor file is missing or too large")
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("dataset descriptor file is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("dataset descriptor file must contain an object")
    return cast(Dict[str, Any], value)


def _append_artifact(
    *,
    artifact_id: str,
    logical_name: str,
    path: Path,
    size_bytes: int,
    sha256: str,
    service_instance_id: str,
    command_id: str,
    cluster_location: Optional[str] = None,
) -> Dict[str, Any]:
    artifact_path_value = os.environ.get("JARVIS_ARTIFACT_PATH")
    execution_id = os.environ.get("JARVIS_EXECUTION_ID")
    package_name = os.environ.get("JARVIS_PACKAGE_NAME")
    package_id = os.environ.get("JARVIS_PACKAGE_ID")
    if not all((artifact_path_value, execution_id, package_name, package_id)):
        raise RuntimeError("JARVIS artifact bindings are missing")
    artifact_path = Path(cast(str, artifact_path_value))
    if not artifact_path.is_absolute():
        raise RuntimeError("JARVIS artifact sidecar path must be absolute")
    artifact_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        artifact_path.parent.chmod(0o700)
    with _artifact_lock(artifact_path):
        existing = _read_artifact_lines(artifact_path)
        sequence = 1
        if existing:
            sequence_value = existing[-1].get("sequence")
            if isinstance(sequence_value, bool) or not isinstance(sequence_value, int):
                raise RuntimeError("JARVIS artifact sidecar has an invalid sequence")
            sequence = sequence_value + 1
        location = cluster_location or path.as_posix()
        _normalized_absolute_path(location)
        event = {
            "schema_version": "jarvis.artifact.v1",
            "package_name": package_name,
            "package_id": package_id,
            "execution_id": execution_id,
            "artifact_id": artifact_id,
            "logical_name": logical_name,
            "kind": "image",
            "role": "output",
            "structure": "file",
            "ownership": "shared",
            "state": "finalized",
            "location": {"kind": "cluster_path", "value": location},
            "media_type": "image/png",
            "format": "png",
            "size_bytes": size_bytes,
            "checksum": "sha256:" + sha256,
            "message": "ParaView service exported an image",
            "revision": 1,
            "sequence": sequence,
            "observed_at_epoch": time.time(),
            "metadata": {
                "application": "paraview",
                "service_instance_id": service_instance_id,
                "command_id": command_id,
                "generation_stage": "final",
            },
        }
        payload = (
            json.dumps(
                event,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
        if len(payload) > 64 * 1024:
            raise RuntimeError("ParaView artifact event exceeds the JARVIS limit")
        descriptor = _open_private_append(artifact_path)
        try:
            offset = 0
            while offset < len(payload):
                written = os.write(descriptor, payload[offset:])
                if written <= 0:
                    raise OSError("short write while appending ParaView artifact")
                offset += written
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return event


@contextmanager
def _artifact_lock(path: Path) -> Iterator[None]:
    lock_path = path.with_name(path.name + ".lock")
    if lock_path.is_symlink():
        raise RuntimeError("JARVIS artifact lock cannot be a symlink")
    descriptor = os.open(
        lock_path,
        os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"0")
            os.fsync(descriptor)
        if os.name == "nt":
            import msvcrt

            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _read_artifact_lines(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
        raise RuntimeError("JARVIS artifact sidecar must be a regular file")
    if path.stat().st_size > 128 * 1024 * 1024:
        raise RuntimeError("JARVIS artifact sidecar exceeds the size limit")
    payload = path.read_bytes()
    if payload and not payload.endswith(b"\n"):
        raise RuntimeError("JARVIS artifact sidecar has incomplete framing")
    values: List[Dict[str, Any]] = []
    for line in payload.splitlines():
        value = json.loads(
            line.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys
        )
        if not isinstance(value, dict):
            raise RuntimeError("JARVIS artifact sidecar line must be an object")
        values.append(cast(Dict[str, Any], value))
    return values


def _open_private_append(path: Path) -> int:
    flags = os.O_APPEND | os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    if os.name != "nt":
        os.fchmod(descriptor, 0o600)
    information = os.fstat(descriptor)
    if not stat.S_ISREG(information.st_mode):
        os.close(descriptor)
        raise RuntimeError("JARVIS artifact sidecar must be a regular file")
    return descriptor


def _require_fields(
    value: Mapping[str, Any],
    expected: set[str],
    operation: str,
) -> None:
    if set(value) != expected:
        raise CommandError(
            "invalid_arguments",
            f"{operation} arguments do not match the command contract",
        )


def _bounded_text(value: object, name: str, *, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.encode("utf-8")) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise CommandError("invalid_arguments", f"{name} must be bounded text")
    return value


def _bounded_int(
    value: object,
    name: str,
    *,
    minimum: int,
    maximum: int = 2_147_483_647,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise CommandError(
            "invalid_arguments",
            f"{name} must be an integer between {minimum} and {maximum}",
        )
    return value


def _finite_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CommandError("invalid_arguments", f"{name} must be numeric")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise CommandError("invalid_arguments", f"{name} must be finite")
    return parsed


def _vector3(value: object, name: str) -> List[float]:
    if not isinstance(value, list) or len(value) != 3:
        raise CommandError("invalid_arguments", f"{name} must contain three values")
    return [_finite_number(item, name) for item in value]


def _nonzero_vector3(value: object, name: str) -> List[float]:
    parsed = _vector3(value, name)
    if math.sqrt(sum(component * component for component in parsed)) <= 1e-12:
        raise CommandError("invalid_arguments", f"{name} cannot be a zero vector")
    return parsed


def _validate_camera_geometry(value: Mapping[str, Any]) -> None:
    """Reject camera vectors that ParaView cannot orient deterministically."""
    position = _vector3(value.get("position"), "position")
    focal_point = _vector3(value.get("focal_point"), "focal_point")
    view_up = _nonzero_vector3(value.get("view_up"), "view_up")
    direction = [focal_point[index] - position[index] for index in range(3)]
    direction_length = math.sqrt(sum(component * component for component in direction))
    if direction_length <= 1e-12:
        raise CommandError(
            "invalid_arguments",
            "camera position and focal_point must be distinct",
        )
    cross = [
        direction[1] * view_up[2] - direction[2] * view_up[1],
        direction[2] * view_up[0] - direction[0] * view_up[2],
        direction[0] * view_up[1] - direction[1] * view_up[0],
    ]
    cross_length = math.sqrt(sum(component * component for component in cross))
    if cross_length <= 1e-12 * direction_length:
        raise CommandError(
            "invalid_arguments",
            "camera view_up cannot be parallel to the viewing direction",
        )
    scale = _finite_number(value.get("parallel_scale"), "parallel_scale")
    if scale <= 0:
        raise CommandError("invalid_arguments", "parallel_scale must be positive")


def _apply_camera_state(view: Any, value: Mapping[str, Any]) -> None:
    """Apply one already validated complete camera state."""
    view.CameraPosition = list(value["position"])
    view.CameraFocalPoint = list(value["focal_point"])
    view.CameraViewUp = list(value["view_up"])
    view.CameraParallelScale = value["parallel_scale"]


def _write_unique_png(
    *,
    simple: Any,
    view: Any,
    output_path: Path,
    width: int,
    height: int,
) -> bytes:
    """Render, validate, durably publish, and never overwrite one PNG."""
    output_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        output_path.parent.chmod(0o700)
    if output_path.exists() or output_path.is_symlink():
        raise CommandError(
            "artifact_exists",
            "export filename already exists for this service instance",
            status=HTTPStatus.CONFLICT,
        )
    descriptor, temporary_name = tempfile.mkstemp(
        dir=output_path.parent,
        prefix=f".{output_path.name}.",
        suffix=".tmp.png",
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    published = False
    succeeded = False
    try:
        simple.SaveScreenshot(
            str(temporary),
            view,
            ImageResolution=[width, height],
        )
        size = temporary.stat().st_size
        if not 8 <= size <= MAX_EXPORTED_ARTIFACT_BYTES:
            raise RuntimeError(
                "ParaView export must be a non-empty bounded PNG artifact"
            )
        with temporary.open("rb") as stream:
            signature = stream.read(8)
            if signature != b"\x89PNG\r\n\x1a\n":
                raise RuntimeError("ParaView export did not produce a PNG artifact")
            stream.seek(0)
            payload = stream.read(MAX_EXPORTED_ARTIFACT_BYTES + 1)
        if len(payload) != size:
            raise RuntimeError("ParaView export changed while it was being validated")
        # Windows rejects fsync on a descriptor opened read-only. A read/write
        # descriptor is portable and does not change the validated payload.
        read_descriptor = os.open(temporary, os.O_RDWR)
        try:
            os.fsync(read_descriptor)
        finally:
            os.close(read_descriptor)
        if os.name != "nt":
            temporary.chmod(0o600)
        try:
            os.link(temporary, output_path)
        except FileExistsError as exc:
            raise CommandError(
                "artifact_exists",
                "export filename already exists for this service instance",
                status=HTTPStatus.CONFLICT,
            ) from exc
        published = True
        if os.name != "nt":
            output_path.chmod(0o600)
        _fsync_directory(output_path.parent)
        succeeded = True
        return payload
    finally:
        temporary.unlink(missing_ok=True)
        if published and not succeeded:
            output_path.unlink(missing_ok=True)
            _fsync_directory(output_path.parent)


def _fsync_directory(path: Path) -> None:
    """Persist one directory update where descriptor fsync is supported."""
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _viewport(value: object) -> Dict[str, float]:
    if not isinstance(value, dict) or set(value) != {"x0", "y0", "x1", "y1"}:
        raise CommandError(
            "invalid_arguments",
            "viewport must contain exactly x0, y0, x1, and y1",
        )
    parsed = {
        name: _finite_number(value.get(name), name) for name in ("x0", "y0", "x1", "y1")
    }
    if any(number < 0 or number > 1 for number in parsed.values()):
        raise CommandError(
            "invalid_arguments",
            "viewport coordinates must be normalized between zero and one",
        )
    if parsed["x0"] >= parsed["x1"] or parsed["y0"] >= parsed["y1"]:
        raise CommandError(
            "invalid_arguments",
            "viewport must have positive width and height",
        )
    return parsed


def _viewport_pixel_rectangle(
    viewport: Mapping[str, float],
    view_size: Tuple[int, int],
) -> List[int]:
    """Map a normalized top-left browser rectangle to ParaView bottom-left pixels."""
    width, height = view_size
    if width < 2 or height < 2:
        raise ValueError("ParaView view size must be at least two pixels per axis")
    x_max = width - 1
    y_max = height - 1
    return [
        max(0, min(x_max, math.floor(viewport["x0"] * x_max))),
        max(0, min(y_max, math.floor((1.0 - viewport["y1"]) * y_max))),
        max(0, min(x_max, math.ceil(viewport["x1"] * x_max))),
        max(0, min(y_max, math.ceil((1.0 - viewport["y0"]) * y_max))),
    ]


def _surface_selection_ids(
    *,
    selected_representations: Any,
    selection_sources: Any,
    active_source: Any,
    servermanager: Any,
    limit: int,
) -> Tuple[List[Dict[str, int]], Optional[int], Optional[str]]:
    """Read real IDs returned by ParaView's render-view surface selection."""
    representation_count = _collection_size(selected_representations)
    selection_count = _collection_size(selection_sources)
    if representation_count != selection_count:
        return [], None, "paraview_selection_collection_mismatch"
    if selection_count == 0:
        return [], 0, None
    active_sm_proxy = getattr(active_source, "SMProxy", active_source)
    returned: List[Dict[str, int]] = []
    total = 0
    matched_active_source = False
    for index in range(selection_count):
        representation = selected_representations.GetItemAsObject(index)
        if not _representation_uses_source(representation, active_sm_proxy):
            continue
        matched_active_source = True
        raw_source = selection_sources.GetItemAsObject(index)
        xml_name = _proxy_xml_name(raw_source)
        try:
            selection_proxy = servermanager._getPyProxy(raw_source)
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return [], None, "paraview_selection_proxy_unavailable"
        values = _integer_property_values(getattr(selection_proxy, "IDs", None))
        if values is None:
            return [], None, "paraview_selection_ids_unavailable"
        if xml_name == "IDSelectionSource":
            width = 2
        elif xml_name == "CompositeDataIDSelectionSource":
            width = 3
        elif xml_name == "HierarchicalDataIDSelectionSource":
            width = 3
        else:
            return [], None, "paraview_selection_source_unsupported"
        if len(values) % width:
            return [], None, "paraview_selection_ids_invalid"
        total += len(values) // width
        for offset in range(0, len(values), width):
            if len(returned) >= limit:
                continue
            group = values[offset : offset + width]
            if xml_name == "IDSelectionSource":
                returned.append({"process_id": group[0], "element_id": group[1]})
            elif xml_name == "CompositeDataIDSelectionSource":
                returned.append(
                    {
                        "block_index": group[0],
                        "process_id": group[1],
                        "element_id": group[2],
                    }
                )
            else:
                returned.append(
                    {
                        "level": group[0],
                        "hierarchy_index": group[1],
                        "element_id": group[2],
                    }
                )
    if not matched_active_source:
        return [], 0, None
    return returned, total, None


def _collection_size(value: object) -> int:
    getter = getattr(value, "GetNumberOfItems", None)
    if not callable(getter):
        raise RuntimeError("ParaView selection collection is unavailable")
    result = getter()
    if isinstance(result, bool) or not isinstance(result, int) or result < 0:
        raise RuntimeError("ParaView selection collection size is invalid")
    return result


def _representation_uses_source(
    representation: Any,
    active_sm_proxy: Any,
) -> bool:
    try:
        input_property = representation.GetProperty("Input")
        selected_source = input_property.GetProxy(0)
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return False
    if selected_source is active_sm_proxy:
        return True
    try:
        return selected_source.GetGlobalID() == active_sm_proxy.GetGlobalID()
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return False


def _proxy_xml_name(value: Any) -> str:
    try:
        name = value.GetXMLName()
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return ""
    return name if isinstance(name, str) else ""


def _integer_property_values(value: object) -> Optional[List[int]]:
    if value is None:
        return None
    getter = getattr(value, "GetData", None)
    if callable(getter):
        try:
            value = getter()
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return None
    try:
        values = list(value)  # type: ignore[arg-type]
    except TypeError:
        return None
    if any(isinstance(item, bool) or not isinstance(item, int) for item in values):
        return None
    return cast(List[int], values)


def _association(value: object) -> str:
    if value not in {"point", "cell"}:
        raise CommandError("invalid_arguments", "association must be point or cell")
    return cast(str, value)


def _safe_relative_png(value: object) -> PurePosixPath:
    rendered = _bounded_text(value, "filename", maximum=1024)
    if "\\" in rendered:
        raise CommandError("invalid_arguments", "filename must use POSIX separators")
    path = PurePosixPath(rendered)
    if path.is_absolute() or path.as_posix() != rendered or ".." in path.parts:
        raise CommandError(
            "invalid_arguments", "filename must be normalized and relative"
        )
    if path.suffix.casefold() != ".png":
        raise CommandError("invalid_arguments", "export_artifact supports PNG output")
    return path


def _normalized_absolute_path(value: object) -> PurePosixPath:
    if not isinstance(value, str) or "\\" in value:
        raise ValueError("dataset member location must use POSIX separators")
    path = PurePosixPath(value)
    if not path.is_absolute() or path.as_posix() != value or ".." in path.parts:
        raise ValueError("dataset member location must be normalized and absolute")
    return path


def _json_copy(value: Mapping[str, Any]) -> Dict[str, Any]:
    return cast(Dict[str, Any], json.loads(json.dumps(value, allow_nan=False)))


def _json_copy_list(value: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return cast(List[Dict[str, Any]], json.loads(json.dumps(value, allow_nan=False)))


def _json_copy_optional(
    value: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    return None if value is None else _json_copy(value)


def _reject_duplicate_keys(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    value: Dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key: %s" % key)
        value[key] = item
    return value


if __name__ == "__main__":
    raise SystemExit(main())
