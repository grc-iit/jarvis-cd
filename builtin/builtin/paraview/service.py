"""Real ParaView backend for the generic JARVIS HTTP/SSE service."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import math
import os
import re
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

MAX_NODES = 32
MAX_REPRESENTATIONS = 32
MAX_MEASUREMENTS = 32
MAX_TIMESTEPS_PER_COMMAND = 32
MAX_STORED_MEASUREMENT_SAMPLES = 128
MAX_CONTOUR_VALUES = 64
MAX_ARTIFACTS = 128
MAX_DISCOVERED_ARRAYS = 256
# Worst-case cleanup owns one node plus display/scalar-bar/PWF/LUT per actor.
MAX_RETIRED_PROXIES = MAX_NODES + 4 * MAX_REPRESENTATIONS
MAX_SELECTION_ELEMENTS = 100_000_000_000
MAX_SELECTION_RESULTS = 256
MAX_VIEWPORT_SELECTION_SOURCE_ELEMENTS = 10_000_000
MAX_EXPORTED_ARTIFACT_BYTES = 128 * 1024 * 1024
LIVE_VIEW_SIZE = (960, 540)
ARTIFACT_PREFIX = "JARVIS_ARTIFACT "
DEFAULT_COLORMAP_PRESET = "Cool to Warm"
DISTRIBUTION_HISTOGRAM_BINS = 128
DISTRIBUTION_PERCENTILES = (0.0, 1.0, 5.0, 50.0, 95.0, 99.0, 100.0)
SUPPORTED_TRANSFER_SCALES = frozenset({"linear", "log"})
ROOT_NODE_ID = "node_root"
ROOT_REPRESENTATION_ID = "rep_root"
REPRESENTATION_PROXY_GROUP = "representations"


class ParaViewBackend:
    """Drive an explicit, branching ParaView scene from versioned commands."""

    def __init__(
        self,
        *,
        descriptor: Mapping[str, Any],
        output_dir: Path,
        service_instance_id: str,
        execution_id: str,
        package_name: str,
        package_id: str,
    ) -> None:
        """Open one descriptor and create only the stable root scene objects."""
        try:
            self.servermanager = importlib.import_module("paraview.servermanager")
            self.simple = importlib.import_module("paraview.simple")
            self.vtk = importlib.import_module("paraview.vtk")
        except ImportError as exc:
            raise RuntimeError(
                "ParaView service mode requires pvpython with paraview.simple"
            ) from exc
        self.descriptor = _validate_descriptor(descriptor)
        if _descriptor_topology(self.descriptor.get("kind")) == "table":
            raise ValueError(
                "ParaView scene-v2 does not support table or chart topology"
            )
        self.output_dir = output_dir.resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            self.output_dir.chmod(0o700)
        self.service_instance_id = service_instance_id
        self.execution_id = _bounded_text(execution_id, "execution_id", maximum=256)
        self.package_name = _bounded_text(package_name, "package_name", maximum=256)
        self.package_id = _bounded_text(package_id, "package_id", maximum=256)
        _recover_artifact_transactions()
        locations = [member["location"] for member in self.descriptor["members"]]
        missing = [location for location in locations if not Path(location).exists()]
        if missing:
            raise FileNotFoundError(
                "dataset descriptor member does not exist: " + missing[0]
            )
        source_input: object = locations[0] if len(locations) == 1 else locations
        self.reader = self.simple.OpenDataFile(source_input)
        if self.reader is None:
            raise RuntimeError("ParaView could not open the dataset descriptor members")
        self.reader.UpdatePipeline()
        self.view = self.simple.GetActiveViewOrCreate("RenderView")
        self.view.ViewSize = list(LIVE_VIEW_SIZE)
        self._reader_timesteps = self._discover_reader_timesteps()
        self._timesteps = self._resolve_timesteps(self._reader_timesteps)
        self._timestep_index = 0
        if self._reader_timesteps:
            initial_reader_time = self._reader_timesteps[0]
            self.view.ViewTime = initial_reader_time
            self.reader.UpdatePipeline(initial_reader_time)
        else:
            self.reader.UpdatePipeline()
        self._node_proxies: Dict[str, Any] = {ROOT_NODE_ID: self.reader}
        self._nodes: Dict[str, Dict[str, Any]] = {}
        root_output = self._output_summary(
            self.reader,
            topology=_descriptor_topology(self.descriptor.get("kind")),
        )
        self._nodes[ROOT_NODE_ID] = {
            "node_id": ROOT_NODE_ID,
            "kind": "reader",
            "input_node_ids": [],
            "filter": None,
            "output": root_output,
        }
        root_display = self.simple.Show(self.reader, self.view)
        root_representation_type = (
            "points" if root_output["topology"] == "points" else "surface"
        )
        self._representation_displays: Dict[str, Any] = {
            ROOT_REPRESENTATION_ID: root_display
        }
        self._representation_transfer_proxies: Dict[str, Tuple[Any, ...]] = {}
        self._representations: Dict[str, Dict[str, Any]] = {
            ROOT_REPRESENTATION_ID: {
                "representation_id": ROOT_REPRESENTATION_ID,
                "node_id": ROOT_NODE_ID,
                "type": root_representation_type,
                "visible": True,
                "opacity": 1.0,
                "point_size_px": (
                    3.0 if root_representation_type == "points" else None
                ),
                "color": {"mode": "solid", "rgb": [0.8, 0.8, 0.8]},
            }
        }
        self._measurements: Dict[str, Dict[str, Any]] = {}
        self._selection: Optional[Dict[str, Any]] = None
        self._artifacts: List[Dict[str, Any]] = []
        self._transaction_open = False
        self._pending_deletes: List[Any] = []
        self._retired_proxies: List[Any] = []
        self._staged_artifacts: Dict[str, Dict[str, Any]] = {}
        self._apply_representation_record(self._representations[ROOT_REPRESENTATION_ID])
        self.simple.ResetCamera(self.view)
        self.simple.Render(self.view)
        self._dataset_arrays = _json_copy_list(root_output["arrays"])
        bounds = root_output["bounds"]
        self._dataset_bounds = None if bounds is None else tuple(bounds)
        self._dataset_timesteps = list(self._timesteps)

    def dataset_state(self) -> Dict[str, Any]:
        """Return immutable descriptor identity and root discovery facts."""
        return {
            "descriptor": _json_copy(self.descriptor),
            "discovery": {
                "arrays": _json_copy_list(self._dataset_arrays),
                "bounds": (
                    None if self._dataset_bounds is None else list(self._dataset_bounds)
                ),
                "timestep_values": list(self._dataset_timesteps),
            },
        }

    def pipeline_state(self) -> Dict[str, Any]:
        """Return the complete explicit scene without global active aliases."""
        current_value = (
            self._timesteps[self._timestep_index] if self._timesteps else None
        )
        return {
            "timestep": {
                "index": self._timestep_index,
                "value": current_value,
                "count": len(self._timesteps),
            },
            "nodes": _json_copy_list(list(self._nodes.values())),
            "representations": _json_copy_list(list(self._representations.values())),
            "measurements": _json_copy_list(list(self._measurements.values())),
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
        """Apply one command transactionally, including direct backend callers."""
        handlers = {
            "set_timestep": self._set_timestep,
            "measure_field": self._measure_field,
            "create_filter": self._create_filter,
            "set_representation": self._set_representation,
            "remove_scene_object": self._remove_scene_object,
            "fit_camera": self._fit_camera,
            "set_camera": self._set_camera,
            "inspect_selection": self._inspect_selection,
            "export_artifact": self._export_artifact,
        }
        handler = handlers.get(operation)
        if handler is None:
            raise CommandError("unsupported_operation", "operation is not supported")
        owns_transaction = not self._transaction_open
        checkpoint: Optional[Dict[str, Any]] = None
        if owns_transaction:
            checkpoint = self.begin_command()
        try:
            result = handler(arguments, command_id)
            if owns_transaction and checkpoint is not None:
                self.commit_command(checkpoint)
        except Exception as exc:
            if owns_transaction and checkpoint is not None and self._transaction_open:
                try:
                    self.rollback_command(checkpoint)
                except Exception as rollback_error:
                    raise RuntimeError(
                        "ParaView command failed and its direct backend transaction "
                        "could not be restored: " + str(rollback_error)
                    ) from exc
            raise
        return result

    def render_png(self) -> bytes:
        """Render the current explicit scene to one bounded PNG frame."""
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
        index = self._validate_timestep_index(arguments.get("index"))
        self._update_time(index)
        refreshed_nodes = self._refreshed_node_records()
        refreshed_representations = self._refreshed_representation_records(
            refreshed_nodes
        )
        self._nodes = refreshed_nodes
        self._representations = refreshed_representations
        self._timestep_index = index
        self._selection = None
        for record in self._representations.values():
            self._apply_representation_record(record)
        self.simple.Render(self.view)
        value = self._timesteps[index] if self._timesteps else None
        return {"timestep": {"index": index, "value": value}}

    def _measure_field(
        self,
        arguments: Mapping[str, Any],
        command_id: str,
    ) -> Dict[str, Any]:
        _require_fields(
            arguments,
            {"node_id", "name", "association", "timestep_indices"},
            "measure_field",
        )
        if len(self._measurements) >= MAX_MEASUREMENTS:
            raise CommandError(
                "measurement_limit",
                "the service measurement limit was reached",
                details={"maximum": MAX_MEASUREMENTS},
            )
        node_id = self._known_node_id(arguments.get("node_id"))
        name = _bounded_text(arguments.get("name"), "name", maximum=512)
        association = _association(arguments.get("association"))
        indices = self._timestep_indices(
            arguments.get("timestep_indices"),
            nullable=False,
        )
        if indices is None:
            raise RuntimeError("validated measurement timesteps are missing")
        stored_samples = sum(
            len(measurement["samples"]) for measurement in self._measurements.values()
        )
        if stored_samples + len(indices) > (MAX_STORED_MEASUREMENT_SAMPLES):
            raise CommandError(
                "measurement_sample_limit",
                "the cumulative stored measurement sample limit was reached",
                details={"maximum": MAX_STORED_MEASUREMENT_SAMPLES},
            )
        initial_array = self._find_array(
            self._nodes[node_id]["output"],
            name=name,
            association=association,
        )
        components = initial_array["components"]
        value_mode = "scalar" if components == 1 else "magnitude"
        measurement_id = _deterministic_id("mea", command_id)
        previous_index = self._timestep_index
        previous_camera = self._camera_state()
        previous_active = self._active_source()
        samples: List[Dict[str, Any]] = []
        measurement_error: Optional[Exception] = None
        try:
            for index in indices:
                self._update_time(index)
                source = self._node_proxies[node_id]
                arrays = self._discover_arrays(source)
                current_array = self._find_array(
                    {"arrays": arrays},
                    name=name,
                    association=association,
                )
                if current_array["components"] != components:
                    raise CommandError(
                        "field_shape_changed",
                        "measured field component count changes across timesteps",
                    )
                observed_range = self._array_range(
                    source,
                    name,
                    association,
                    components,
                )
                tuple_count = self._array_tuple_count(
                    source,
                    name,
                    association,
                    components,
                )
                distribution = self._measure_distribution(
                    source,
                    name=name,
                    association=association,
                    components=components,
                    observed_range=observed_range,
                    tuple_count=tuple_count,
                )
                samples.append(
                    {
                        "timestep_index": index,
                        "timestep_value": (
                            self._timesteps[index] if self._timesteps else None
                        ),
                        "observed_range": observed_range,
                        "tuple_count": tuple_count,
                        "distribution": distribution,
                    }
                )
        except Exception as exc:
            measurement_error = exc
        try:
            self._update_time(previous_index)
            for record in self._representations.values():
                self._apply_representation_record(record)
            _apply_camera_state(self.view, previous_camera)
            self._set_active_source(previous_active)
            self.simple.Render(self.view)
        except Exception as restore_error:
            if measurement_error is not None:
                raise RuntimeError(
                    "measure_field failed and the original temporal scene could not "
                    "be restored: " + str(restore_error)
                ) from measurement_error
            raise RuntimeError(
                "measure_field could not restore the original temporal scene: "
                + str(restore_error)
            ) from restore_error
        if measurement_error is not None:
            raise measurement_error
        aggregate = _aggregate_measurement_samples(samples)
        measurement = {
            "measurement_id": measurement_id,
            "node_id": node_id,
            "field": {
                "name": name,
                "association": association,
                "components": components,
                "units": initial_array.get("units"),
            },
            "value_mode": value_mode,
            "timestep_indices": indices,
            "samples": samples,
            "aggregate": aggregate,
        }
        self._measurements[measurement_id] = measurement
        return {"measurement": _json_copy(measurement)}

    def _create_filter(
        self,
        arguments: Mapping[str, Any],
        command_id: str,
    ) -> Dict[str, Any]:
        _require_fields(
            arguments,
            {"input_node_id", "type", "parameters"},
            "create_filter",
        )
        if len(self._nodes) >= MAX_NODES:
            raise CommandError(
                "node_limit",
                "the service scene-node limit was reached",
                details={"maximum": MAX_NODES},
            )
        input_node_id = self._known_node_id(arguments.get("input_node_id"))
        filter_type = _bounded_text(arguments.get("type"), "type", maximum=64)
        parameters = arguments.get("parameters")
        if not isinstance(parameters, dict):
            raise CommandError(
                "invalid_arguments", "filter parameters must be an object"
            )
        parsed = self._validate_filter_parameters(
            input_node_id,
            filter_type,
            parameters,
        )
        node_id = _deterministic_id("node", command_id)
        source = self._node_proxies[input_node_id]
        previous_active = self._active_source()
        proxy: Any = None
        try:
            if filter_type == "slice":
                proxy = self.simple.Slice(Input=source)
                proxy.SliceType.Origin = parsed["origin"]
                proxy.SliceType.Normal = parsed["normal"]
            elif filter_type == "clip":
                proxy = self.simple.Clip(Input=source)
                proxy.ClipType.Origin = parsed["origin"]
                proxy.ClipType.Normal = parsed["normal"]
            elif filter_type == "threshold":
                proxy = self.simple.Threshold(Input=source)
                proxy.Scalars = [
                    "POINTS" if parsed["association"] == "point" else "CELLS",
                    parsed["name"],
                ]
                proxy.LowerThreshold = parsed["lower"]
                proxy.UpperThreshold = parsed["upper"]
            elif filter_type == "contour":
                proxy = self.simple.Contour(Input=source)
                proxy.ContourBy = ["POINTS", parsed["name"]]
                proxy.Isosurfaces = list(parsed["isovalues"])
                if hasattr(proxy, "ComputeScalars"):
                    proxy.ComputeScalars = 1
            else:  # Validation above makes this unreachable.
                raise RuntimeError("validated filter type is unsupported")
            if self._reader_timesteps:
                proxy.UpdatePipeline(self._reader_timesteps[self._timestep_index])
            else:
                proxy.UpdatePipeline()
            topology = (
                "surface"
                if filter_type in {"slice", "contour"}
                else self._nodes[input_node_id]["output"]["topology"]
            )
            output = self._output_summary(proxy, topology=topology)
        except Exception:
            if proxy is not None:
                try:
                    self.simple.Delete(proxy)
                finally:
                    self._set_active_source(previous_active)
            raise
        self._set_active_source(previous_active)
        record = {
            "node_id": node_id,
            "kind": filter_type,
            "input_node_ids": [input_node_id],
            "filter": {"type": filter_type, "parameters": parsed},
            "output": output,
        }
        self._node_proxies[node_id] = proxy
        self._nodes[node_id] = record
        return {"node": _json_copy(record)}

    def _validate_filter_parameters(
        self,
        input_node_id: str,
        filter_type: str,
        parameters: Mapping[str, Any],
    ) -> Dict[str, Any]:
        """Validate one filter fully before constructing a ParaView proxy."""
        if filter_type in {"slice", "clip"}:
            _require_fields(parameters, {"origin", "normal"}, filter_type)
            return {
                "origin": _vector3(parameters.get("origin"), "origin"),
                "normal": _nonzero_vector3(parameters.get("normal"), "normal"),
            }
        if filter_type == "threshold":
            _require_fields(
                parameters,
                {"name", "association", "lower", "upper"},
                "threshold",
            )
            name = _bounded_text(parameters.get("name"), "name", maximum=512)
            association = _association(parameters.get("association"))
            array = self._find_array(
                self._nodes[input_node_id]["output"],
                name=name,
                association=association,
            )
            if array["components"] != 1:
                raise CommandError(
                    "unsupported_field_shape",
                    "threshold requires an explicit scalar field",
                )
            lower = _finite_number(parameters.get("lower"), "lower")
            upper = _finite_number(parameters.get("upper"), "upper")
            if lower > upper:
                raise CommandError(
                    "invalid_arguments", "threshold lower cannot exceed upper"
                )
            return {
                "name": name,
                "association": association,
                "lower": lower,
                "upper": upper,
            }
        if filter_type == "contour":
            _require_fields(
                parameters,
                {"name", "association", "isovalues"},
                "contour",
            )
            name = _bounded_text(parameters.get("name"), "name", maximum=512)
            association = _association(parameters.get("association"))
            if association != "point":
                raise CommandError(
                    "unsupported_contour_field",
                    "ParaView contour requires a point-centered scalar field",
                )
            array = self._find_array(
                self._nodes[input_node_id]["output"],
                name=name,
                association=association,
            )
            if array["components"] != 1:
                raise CommandError(
                    "unsupported_contour_field",
                    "ParaView contour requires a single-component field",
                )
            raw_values = parameters.get("isovalues")
            if not isinstance(raw_values, list) or not 1 <= len(raw_values) <= (
                MAX_CONTOUR_VALUES
            ):
                raise CommandError(
                    "invalid_arguments",
                    "contour isovalues must be a nonempty bounded list",
                    details={"maximum": MAX_CONTOUR_VALUES},
                )
            values = [_finite_number(value, "isovalues") for value in raw_values]
            if len(set(values)) != len(values):
                raise CommandError(
                    "invalid_arguments", "contour isovalues must be unique"
                )
            return {
                "name": name,
                "association": association,
                "isovalues": values,
            }
        raise CommandError(
            "unsupported_filter",
            "filter type must be slice, clip, threshold, or contour",
        )

    def _set_representation(
        self,
        arguments: Mapping[str, Any],
        command_id: str,
    ) -> Dict[str, Any]:
        _require_fields(
            arguments,
            {
                "representation_id",
                "node_id",
                "type",
                "visible",
                "opacity",
                "point_size_px",
                "color",
            },
            "set_representation",
        )
        node_id = self._known_node_id(arguments.get("node_id"))
        requested_id = arguments.get("representation_id")
        creating = requested_id is None
        if creating:
            if len(self._representations) >= MAX_REPRESENTATIONS:
                raise CommandError(
                    "representation_limit",
                    "the service representation limit was reached",
                    details={"maximum": MAX_REPRESENTATIONS},
                )
            representation_id = _deterministic_id("rep", command_id)
        else:
            representation_id = self._known_representation_id(requested_id)
            if self._representations[representation_id]["node_id"] != node_id:
                raise CommandError(
                    "representation_node_mismatch",
                    "an existing representation cannot be retargeted to another node",
                )
        representation_type = arguments.get("type")
        if representation_type not in {"surface", "points"}:
            raise CommandError(
                "invalid_arguments", "representation type must be surface or points"
            )
        visible = arguments.get("visible")
        if not isinstance(visible, bool):
            raise CommandError("invalid_arguments", "visible must be boolean")
        opacity = _finite_number(arguments.get("opacity"), "opacity")
        if not 0.0 <= opacity <= 1.0:
            raise CommandError(
                "invalid_arguments", "opacity must be between zero and one"
            )
        point_size_value = arguments.get("point_size_px")
        if representation_type == "points":
            point_size_px = _finite_number(point_size_value, "point_size_px")
            if not 1.0 <= point_size_px <= 64.0:
                raise CommandError(
                    "invalid_arguments",
                    "point_size_px must be between 1 and 64 pixels",
                )
        else:
            if point_size_value is not None:
                raise CommandError(
                    "invalid_arguments", "surface point_size_px must be null"
                )
            point_size_px = None
        color = self._resolve_color(node_id, arguments.get("color"))
        if not visible and color["mode"] == "field":
            color["scalar_bar"] = {
                "visible": False,
                "embedded_in_frame": False,
            }
        record = {
            "representation_id": representation_id,
            "node_id": node_id,
            "type": representation_type,
            "visible": visible,
            "opacity": opacity,
            "point_size_px": point_size_px,
            "color": color,
        }
        previous_active = self._active_source()
        if creating:
            display: Any = None
            try:
                creator = getattr(self.servermanager, "CreateRepresentation", None)
                if not callable(creator):
                    raise RuntimeError(
                        "ParaView runtime cannot create an independent representation"
                    )
                display = creator(self._node_proxies[node_id], self.view)
                if display is None:
                    raise RuntimeError(
                        "ParaView returned no independent representation proxy"
                    )
                if any(
                    _same_proxy(display, existing)
                    for existing in self._representation_displays.values()
                ):
                    raise RuntimeError(
                        "ParaView reused an existing representation proxy for a new "
                        "scene actor"
                    )
                self._register_representation_display(
                    representation_id,
                    display,
                )
            except Exception:
                if display is not None:
                    try:
                        self.simple.Delete(display)
                    except Exception:
                        pass
                self._set_active_source(previous_active)
                raise
            self._representation_displays[representation_id] = display
            self._representations[representation_id] = record
        else:
            self._representations[representation_id] = record
        self._apply_representation_record(record)
        self._set_active_source(previous_active)
        self.simple.Render(self.view)
        return {"representation": _json_copy(record)}

    def _register_representation_display(
        self,
        representation_id: str,
        display: Any,
    ) -> None:
        """Register an independent display so ParaView owns and deletes it exactly."""
        manager_factory = getattr(self.servermanager, "ProxyManager", None)
        if not callable(manager_factory):
            raise RuntimeError("ParaView proxy manager is unavailable")
        manager = manager_factory()
        getter = getattr(manager, "GetProxy", None)
        register = getattr(manager, "RegisterProxy", None)
        if not callable(getter) or not callable(register):
            raise RuntimeError("ParaView proxy manager cannot register representations")
        registration_name = f"jarvis-{self.service_instance_id}-{representation_id}"
        if getter(REPRESENTATION_PROXY_GROUP, registration_name) is not None:
            raise RuntimeError("ParaView representation registration already exists")
        register(REPRESENTATION_PROXY_GROUP, registration_name, display)
        registered = getter(REPRESENTATION_PROXY_GROUP, registration_name)
        if registered is None or not _same_proxy(registered, display):
            raise RuntimeError("ParaView representation registration was not retained")

    def _resolve_color(self, node_id: str, value: object) -> Dict[str, Any]:
        """Resolve one validated color request into authoritative actor state."""
        if not isinstance(value, dict):
            raise CommandError("invalid_arguments", "color must be an object")
        mode = value.get("mode")
        if mode == "solid":
            _require_fields(value, {"mode", "rgb"}, "solid color")
            rgb = _rgb(value.get("rgb"))
            return {"mode": "solid", "rgb": rgb}
        if mode != "field":
            raise CommandError("invalid_arguments", "color mode must be solid or field")
        _require_fields(
            value,
            {
                "mode",
                "field",
                "preset",
                "invert",
                "scale",
                "range_policy",
                "scalar_bar_visible",
            },
            "field color",
        )
        selector = value.get("field")
        if not isinstance(selector, dict):
            raise CommandError("invalid_arguments", "field must be an object")
        _require_fields(selector, {"name", "association"}, "field")
        name = _bounded_text(selector.get("name"), "name", maximum=512)
        association = _association(selector.get("association"))
        array = self._find_array(
            self._nodes[node_id]["output"],
            name=name,
            association=association,
        )
        source = self._node_proxies[node_id]
        components = array["components"]
        observed_range = self._array_range(
            source,
            name,
            association,
            components,
        )
        tuple_count = self._array_tuple_count(
            source,
            name,
            association,
            components,
        )
        requested_preset = value.get("preset")
        preset = (
            DEFAULT_COLORMAP_PRESET
            if requested_preset is None
            else _bounded_text(requested_preset, "preset", maximum=256)
        )
        invert = value.get("invert")
        scalar_bar_visible = value.get("scalar_bar_visible")
        if not isinstance(invert, bool) or not isinstance(scalar_bar_visible, bool):
            raise CommandError(
                "invalid_arguments", "invert and scalar_bar_visible must be boolean"
            )
        scale = _transfer_scale(value.get("scale"))
        policy = self._representation_range_policy(
            node_id=node_id,
            name=name,
            association=association,
            value=value.get("range_policy"),
        )
        transfer_range = self._resolve_transfer_range(
            observed_range=observed_range,
            policy=policy,
        )
        _validate_scale_range(scale, transfer_range)
        return {
            "mode": "field",
            "field": {
                "name": name,
                "association": association,
                "components": components,
                "units": array.get("units"),
            },
            "observation": {
                "observed_range": observed_range,
                "tuple_count": tuple_count,
                "value_mode": "scalar" if components == 1 else "magnitude",
            },
            "preset": preset,
            "invert": invert,
            "scale": scale,
            "range_policy": policy,
            "transfer_range": transfer_range,
            "scalar_bar": {
                "visible": scalar_bar_visible,
                "embedded_in_frame": scalar_bar_visible,
            },
            "supported_scales": sorted(SUPPORTED_TRANSFER_SCALES),
        }

    def _representation_range_policy(
        self,
        *,
        node_id: str,
        name: str,
        association: str,
        value: object,
    ) -> Dict[str, Any]:
        """Validate a reusable actor transfer-range policy."""
        if not isinstance(value, dict):
            raise CommandError("invalid_arguments", "range_policy must be an object")
        mode = value.get("mode")
        if mode == "full":
            _require_fields(value, {"mode", "timestep_behavior"}, "full policy")
            if value.get("timestep_behavior") != "recompute":
                raise CommandError(
                    "invalid_arguments", "full range policy must recompute"
                )
            return {"mode": "full", "timestep_behavior": "recompute"}
        if mode == "fixed":
            _require_fields(
                value,
                {"mode", "range", "timestep_behavior"},
                "fixed policy",
            )
            if value.get("timestep_behavior") != "freeze":
                raise CommandError(
                    "invalid_arguments", "fixed range policy must remain frozen"
                )
            raw_range = value.get("range")
            if not isinstance(raw_range, list) or len(raw_range) != 2:
                raise CommandError(
                    "invalid_arguments", "fixed range must contain two numbers"
                )
            parsed = [
                _finite_number(raw_range[0], "range"),
                _finite_number(raw_range[1], "range"),
            ]
            if parsed[0] >= parsed[1]:
                raise CommandError(
                    "invalid_arguments", "fixed transfer range must be increasing"
                )
            return {
                "mode": "fixed",
                "range": parsed,
                "timestep_behavior": "freeze",
            }
        if mode == "measurement_percentile":
            _require_fields(
                value,
                {
                    "mode",
                    "measurement_id",
                    "lower_percentile",
                    "upper_percentile",
                    "timestep_behavior",
                },
                "measurement percentile policy",
            )
            measurement_id = self._known_measurement_id(value.get("measurement_id"))
            measurement = self._measurements[measurement_id]
            field = measurement["field"]
            if (
                measurement["node_id"] != node_id
                or field["name"] != name
                or field["association"] != association
            ):
                raise CommandError(
                    "measurement_field_mismatch",
                    "range measurement must target the representation node and field",
                )
            lower = _finite_number(value.get("lower_percentile"), "lower_percentile")
            upper = _finite_number(value.get("upper_percentile"), "upper_percentile")
            if not 0.0 <= lower < upper <= 100.0:
                raise CommandError(
                    "invalid_arguments",
                    "measurement percentiles must satisfy 0 <= lower < upper <= 100",
                )
            if value.get("timestep_behavior") != "freeze":
                raise CommandError(
                    "invalid_arguments",
                    "measurement-backed percentile ranges must remain frozen",
                )
            return {
                "mode": "measurement_percentile",
                "measurement_id": measurement_id,
                "lower_percentile": lower,
                "upper_percentile": upper,
                "timestep_behavior": "freeze",
            }
        raise CommandError(
            "invalid_arguments",
            "range_policy must be full, fixed, or measurement_percentile",
        )

    def _resolve_transfer_range(
        self,
        *,
        observed_range: Sequence[float],
        policy: Mapping[str, Any],
    ) -> List[float]:
        mode = policy["mode"]
        if mode == "full":
            return [float(observed_range[0]), float(observed_range[1])]
        if mode == "fixed":
            return [float(value) for value in policy["range"]]
        measurement = self._measurements[cast(str, policy["measurement_id"])]
        distribution = measurement["aggregate"]["distribution"]
        if distribution.get("status") != "available":
            raise CommandError(
                "distribution_unavailable",
                "measurement-backed range requires an available aggregate histogram",
            )
        histogram = distribution["histogram"]
        edges = [float(value) for value in histogram["bin_edges"]]
        counts = [float(value) for value in histogram["counts"]]
        resolved = [
            _histogram_percentile(
                edges,
                counts,
                float(policy["lower_percentile"]),
            ),
            _histogram_percentile(
                edges,
                counts,
                float(policy["upper_percentile"]),
            ),
        ]
        if resolved[0] >= resolved[1]:
            raise CommandError(
                "degenerate_transfer_range",
                "measurement percentiles do not produce an increasing range",
            )
        return resolved

    def _remove_scene_object(
        self,
        arguments: Mapping[str, Any],
        _command_id: str,
    ) -> Dict[str, Any]:
        _require_fields(arguments, {"object_id"}, "remove_scene_object")
        object_id = _bounded_text(arguments.get("object_id"), "object_id", maximum=256)
        if len(self._retired_proxies) >= MAX_RETIRED_PROXIES:
            raise CommandError(
                "cleanup_backlog",
                "ParaView proxy cleanup backlog reached its bounded limit",
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            )
        if object_id in self._representations:
            if object_id == ROOT_REPRESENTATION_ID:
                raise CommandError(
                    "root_scene_object",
                    "the stable root representation cannot be removed",
                )
            display = self._representation_displays[object_id]
            scalar_bar_setter = getattr(display, "SetScalarBarVisibility", None)
            if not callable(scalar_bar_setter):
                raise RuntimeError("ParaView display cannot control its scalar bar")
            scalar_bar_setter(self.view, False)
            display.Visibility = 0
            self.simple.Render(self.view)
            transfer_proxies = self._representation_transfer_proxies.pop(
                object_id,
                (),
            )
            self._retire_replaced_transfer_proxies(transfer_proxies, ())
            self._queue_proxy_delete(display)
            del self._representation_displays[object_id]
            del self._representations[object_id]
            if (
                self._selection is not None
                and self._selection.get("representation_id") == object_id
            ):
                self._selection = None
            return {"removed": {"object_id": object_id, "kind": "representation"}}
        if object_id in self._measurements:
            dependent = sorted(
                representation_id
                for representation_id, record in self._representations.items()
                if record["color"].get("mode") == "field"
                and record["color"]["range_policy"].get("measurement_id") == object_id
            )
            if dependent:
                raise CommandError(
                    "scene_dependency",
                    "measurement is referenced by a representation range policy",
                    status=HTTPStatus.CONFLICT,
                    details={"representation_ids": dependent},
                )
            del self._measurements[object_id]
            return {"removed": {"object_id": object_id, "kind": "measurement"}}
        if object_id in self._nodes:
            if object_id == ROOT_NODE_ID:
                raise CommandError(
                    "root_scene_object", "the stable root node cannot be removed"
                )
            children = sorted(
                node_id
                for node_id, record in self._nodes.items()
                if object_id in record["input_node_ids"]
            )
            representations = sorted(
                representation_id
                for representation_id, record in self._representations.items()
                if record["node_id"] == object_id
            )
            measurements = sorted(
                measurement_id
                for measurement_id, record in self._measurements.items()
                if record["node_id"] == object_id
            )
            if children or representations or measurements:
                raise CommandError(
                    "scene_dependency",
                    "node cannot be removed while scene objects depend on it",
                    status=HTTPStatus.CONFLICT,
                    details={
                        "node_ids": children,
                        "representation_ids": representations,
                        "measurement_ids": measurements,
                    },
                )
            self._queue_proxy_delete(self._node_proxies[object_id])
            del self._node_proxies[object_id]
            del self._nodes[object_id]
            return {"removed": {"object_id": object_id, "kind": "node"}}
        raise CommandError("scene_object_not_found", "scene object does not exist")

    def _fit_camera(
        self,
        arguments: Mapping[str, Any],
        _command_id: str,
    ) -> Dict[str, Any]:
        _require_fields(
            arguments,
            {"representation_ids", "timestep_indices", "padding"},
            "fit_camera",
        )
        representation_ids = self._representation_id_list(
            arguments.get("representation_ids"),
            visible=True,
        )
        requested_indices = self._timestep_indices(
            arguments.get("timestep_indices"),
            nullable=True,
        )
        indices = (
            [self._timestep_index] if requested_indices is None else requested_indices
        )
        padding = _finite_number(arguments.get("padding"), "padding")
        if not 1.0 <= padding <= 4.0:
            raise CommandError(
                "invalid_arguments", "camera padding must be between 1 and 4"
            )
        previous_index = self._timestep_index
        previous_active = self._active_source()
        bounds: Optional[List[float]] = None
        fit_error: Optional[Exception] = None
        try:
            for index in indices:
                self._update_time(index)
                for representation_id in representation_ids:
                    node_id = self._representations[representation_id]["node_id"]
                    candidate = self._discover_bounds(self._node_proxies[node_id])
                    if candidate is not None:
                        bounds = _union_bounds(bounds, list(candidate))
        except Exception as exc:
            fit_error = exc
        try:
            self._update_time(previous_index)
            self._set_active_source(previous_active)
        except Exception as restore_error:
            if fit_error is not None:
                raise RuntimeError(
                    "fit_camera failed and the original timestep could not be "
                    "restored: " + str(restore_error)
                ) from fit_error
            raise RuntimeError(
                "fit_camera could not restore the original timestep: "
                + str(restore_error)
            ) from restore_error
        if fit_error is not None:
            raise fit_error
        if bounds is None:
            raise CommandError(
                "fit_bounds_unavailable",
                "selected representations expose no finite bounds",
            )
        resetter = getattr(self.view, "ResetCamera", None)
        if not callable(resetter):
            raise RuntimeError("ParaView render view cannot reset to explicit bounds")
        resetter(bounds)
        candidate_camera = self._camera_state()
        if candidate_camera["projection"] == "parallel":
            candidate_camera["parallel_scale"] *= padding
        else:
            focal_point = candidate_camera["focal_point"]
            position = candidate_camera["position"]
            candidate_camera["position"] = [
                focal_point[index] + (position[index] - focal_point[index]) * padding
                for index in range(3)
            ]
        _validate_camera_geometry(candidate_camera)
        _apply_camera_state(self.view, candidate_camera)
        self.simple.Render(self.view)
        return {
            "camera": self._camera_state(),
            "bounds": bounds,
            "representation_ids": representation_ids,
            "timestep_indices": indices,
        }

    def _set_camera(
        self,
        arguments: Mapping[str, Any],
        _command_id: str,
    ) -> Dict[str, Any]:
        allowed = {
            "position",
            "focal_point",
            "view_up",
            "parallel_scale",
            "projection",
            "view_angle",
        }
        if not arguments or set(arguments) - allowed:
            raise CommandError(
                "invalid_arguments",
                "set_camera accepts a nonempty subset of position, focal_point, "
                "view_up, parallel_scale, projection, and view_angle",
            )
        candidate = self._camera_state()
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
                    "invalid_arguments", "parallel_scale must be positive"
                )
            candidate["parallel_scale"] = scale
        if "projection" in arguments:
            projection = arguments["projection"]
            if projection not in {"perspective", "parallel"}:
                raise CommandError(
                    "invalid_arguments",
                    "projection must be perspective or parallel",
                )
            candidate["projection"] = projection
        if "view_angle" in arguments:
            view_angle = _finite_number(arguments["view_angle"], "view_angle")
            if not 0 < view_angle < 180:
                raise CommandError(
                    "invalid_arguments", "view_angle must be between 0 and 180 degrees"
                )
            candidate["view_angle"] = view_angle
        _validate_camera_geometry(candidate)
        _apply_camera_state(self.view, candidate)
        self.simple.Render(self.view)
        return {"camera": self._camera_state()}

    def _inspect_selection(
        self,
        arguments: Mapping[str, Any],
        _command_id: str,
    ) -> Dict[str, Any]:
        if set(arguments) == {"representation_id", "element"}:
            representation_id = self._known_representation_id(
                arguments.get("representation_id")
            )
            return self._inspect_element_selection(
                representation_id,
                arguments.get("element"),
            )
        if set(arguments) == {"representation_id", "viewport"}:
            representation_id = self._known_representation_id(
                arguments.get("representation_id")
            )
            return self._inspect_viewport_selection(
                representation_id,
                arguments.get("viewport"),
            )
        raise CommandError(
            "invalid_arguments",
            "inspect_selection requires representation_id and exactly one of "
            "element or viewport",
        )

    def _inspect_element_selection(
        self,
        representation_id: str,
        value: object,
    ) -> Dict[str, Any]:
        """Inspect one process-zero element without creating a ParaView selection."""
        if not isinstance(value, dict):
            raise CommandError("invalid_arguments", "element must be an object")
        _require_fields(value, {"association", "index"}, "element selection")
        association = _association(value.get("association"))
        index = _bounded_int(
            value.get("index"),
            "index",
            minimum=0,
            maximum=MAX_SELECTION_ELEMENTS,
        )
        representation = self._representations[representation_id]
        if not representation["visible"]:
            raise CommandError(
                "representation_not_visible",
                "selection requires a visible representation",
            )
        node_id = representation["node_id"]
        source = self._node_proxies[node_id]
        raw_data_type = self._nodes[node_id]["output"]["raw_data_type"]
        if _ambiguous_element_data_type(raw_data_type):
            raise CommandError(
                "ambiguous_element_selection",
                "bare element indexes are ambiguous for composite ParaView data",
                details={
                    "raw_data_type": (
                        raw_data_type[:256] if isinstance(raw_data_type, str) else None
                    )
                },
            )
        information = source.GetDataInformation()
        count = (
            information.GetNumberOfPoints()
            if association == "point"
            else information.GetNumberOfCells()
        )
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise RuntimeError("ParaView element count is unavailable")
        if index >= count:
            raise CommandError(
                "selection_out_of_range",
                "selection index exceeds the real ParaView element count",
            )
        self._selection = {
            "selector": "element",
            "representation_id": representation_id,
            "node_id": node_id,
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
        return {"selection": _json_copy(self._selection)}

    def _inspect_viewport_selection(
        self,
        representation_id: str,
        value: object,
    ) -> Dict[str, Any]:
        """Use ParaView's real visible-cell picker for one explicit actor."""
        representation = self._representations[representation_id]
        if not representation["visible"]:
            raise CommandError(
                "representation_not_visible",
                "selection requires a visible representation",
            )
        node_id = representation["node_id"]
        source = self._node_proxies[node_id]
        display = self._representation_displays[representation_id]
        viewport = _viewport(value)
        pixel_rectangle = _viewport_pixel_rectangle(viewport, LIVE_VIEW_SIZE)
        association = "point" if representation["type"] == "points" else "cell"
        information = source.GetDataInformation()
        source_element_count = _optional_nonnegative_count(
            information,
            "GetNumberOfPoints" if association == "point" else "GetNumberOfCells",
        )
        unsupported_reason: Optional[str] = None
        if source_element_count is None:
            unsupported_reason = "paraview_selection_source_count_unavailable"
        elif source_element_count > MAX_VIEWPORT_SELECTION_SOURCE_ELEMENTS:
            unsupported_reason = (
                "paraview_selection_source_exceeds_materialization_limit"
            )
        if unsupported_reason is not None:
            self._selection = {
                "selector": "viewport",
                "representation_id": representation_id,
                "node_id": node_id,
                "status": "unsupported",
                "association": association,
                "viewport": viewport,
                "pixel_rectangle": pixel_rectangle,
                "selected_count": None,
                "returned_count": 0,
                "truncated": False,
                "ids": [],
                "reason": unsupported_reason,
            }
            return {"selection": _json_copy(self._selection)}
        selected_representations = self.vtk.vtkCollection()
        selection_sources = self.vtk.vtkCollection()
        try:
            self.simple.Render(self.view)
            picker = (
                self.view.SelectSurfacePoints
                if association == "point"
                else self.view.SelectSurfaceCells
            )
            picker(
                pixel_rectangle,
                selected_representations,
                selection_sources,
                0,
            )
            ids, selected_count, unsupported_reason = (
                _surface_selection_ids_for_representation(
                    selected_representations=selected_representations,
                    selection_sources=selection_sources,
                    target_representation=display,
                    target_source=source,
                    servermanager=self.servermanager,
                    limit=MAX_SELECTION_RESULTS,
                )
            )
        except (AttributeError, RuntimeError, TypeError, ValueError):
            ids = []
            selected_count = None
            unsupported_reason = "paraview_surface_selection_unavailable"
        try:
            self.simple.ClearSelection(source)
            self.simple.Render(self.view)
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            raise RuntimeError(
                "ParaView could not clear the viewport selection highlight"
            ) from exc
        status = (
            "unsupported"
            if unsupported_reason is not None
            else "empty"
            if selected_count == 0
            else "selected"
        )
        self._selection = {
            "selector": "viewport",
            "representation_id": representation_id,
            "node_id": node_id,
            "status": status,
            "association": association,
            "viewport": viewport,
            "pixel_rectangle": pixel_rectangle,
            "selected_count": selected_count,
            "returned_count": len(ids),
            "truncated": selected_count is not None and selected_count > len(ids),
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
            {"filename", "width", "height", "representation_ids"},
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
        representation_ids = self._representation_id_list(
            arguments.get("representation_ids"),
            visible=True,
        )
        visible_ids = sorted(
            representation_id
            for representation_id, record in self._representations.items()
            if record["visible"]
        )
        if representation_ids != visible_ids:
            raise CommandError(
                "visible_scene_mismatch",
                "export representation_ids must equal the current visible scene",
                details={"visible_representation_ids": visible_ids},
            )
        candidate_path = self.output_dir / filename
        output_parent = candidate_path.parent.resolve()
        if (
            output_parent != self.output_dir
            and self.output_dir not in output_parent.parents
        ):
            raise CommandError("invalid_arguments", "artifact path escapes output root")
        output_path = output_parent / candidate_path.name
        scene_digest = self._scene_digest()
        artifact_id = _deterministic_id(
            "art",
            self.service_instance_id + "\x00" + command_id,
        )
        replay = _find_artifact_event(artifact_id)
        if replay is not None:
            _validate_artifact_replay(
                replay,
                logical_name=filename.as_posix(),
                output_path=output_path,
                service_instance_id=self.service_instance_id,
                command_id=command_id,
                representation_ids=representation_ids,
                scene_digest=scene_digest,
                execution_id=self.execution_id,
                package_name=self.package_name,
                package_id=self.package_id,
            )
            if not any(
                artifact["artifact_id"] == artifact_id for artifact in self._artifacts
            ):
                self._artifacts.append(replay)
            return {"artifact": _json_copy(replay)}
        staged_path, payload = _stage_png(
            simple=self.simple,
            view=self.view,
            output_dir=output_path.parent,
            width=width,
            height=height,
        )
        digest = hashlib.sha256(payload).hexdigest()
        try:
            artifact = _prepare_artifact_event(
                artifact_id=artifact_id,
                logical_name=filename.as_posix(),
                path=output_path,
                size_bytes=len(payload),
                sha256=digest,
                service_instance_id=self.service_instance_id,
                command_id=command_id,
                representation_ids=representation_ids,
                scene_digest=scene_digest,
            )
        except Exception:
            staged_path.unlink(missing_ok=True)
            raise
        self._staged_artifacts[artifact_id] = {
            "event": artifact,
            "staged_path": staged_path,
            "output_path": output_path,
            "command_id": command_id,
            "representation_ids": list(representation_ids),
            "scene_digest": scene_digest,
        }
        self._artifacts.append(artifact)
        return {"artifact": _json_copy(artifact)}

    def begin_command(self) -> Dict[str, Any]:
        """Capture a reversible scene checkpoint for the controller boundary."""
        if self._transaction_open:
            raise RuntimeError("ParaView command transactions cannot be nested")
        if self._pending_deletes:
            raise RuntimeError("ParaView has uncommitted scene cleanup")
        if self._staged_artifacts:
            raise RuntimeError("ParaView has uncommitted artifact publication")
        self._transaction_open = True
        return {
            "nodes": _json_copy_list(list(self._nodes.values())),
            "node_proxies": dict(self._node_proxies),
            "representations": _json_copy_list(list(self._representations.values())),
            "representation_displays": dict(self._representation_displays),
            "representation_transfer_proxies": {
                representation_id: tuple(proxies)
                for representation_id, proxies in (
                    self._representation_transfer_proxies.items()
                )
            },
            "measurements": _json_copy_list(list(self._measurements.values())),
            "selection": _json_copy_optional(self._selection),
            "artifacts": _json_copy_list(self._artifacts),
            "timestep_index": self._timestep_index,
            "camera": self._camera_state(),
            "active_source": self._active_source(),
        }

    def commit_command(self, checkpoint: object) -> None:
        """Commit one command and retire cleanup without ambiguous failure."""
        del checkpoint
        if not self._transaction_open:
            raise RuntimeError("no ParaView command transaction is active")
        self._validate_staged_artifact_identities()
        _commit_staged_artifacts(self._staged_artifacts)
        self._staged_artifacts = {}
        self._retired_proxies.extend(self._pending_deletes)
        self._pending_deletes = []
        self._transaction_open = False
        self._drain_retired_proxies()

    def _validate_staged_artifact_identities(self) -> None:
        """Bind every pending publication to this exact runtime and scene."""
        if not self._staged_artifacts:
            return
        visible_ids = sorted(
            representation_id
            for representation_id, record in self._representations.items()
            if record["visible"]
        )
        current_scene_digest = self._scene_digest()
        for staged in self._staged_artifacts.values():
            event = cast(Mapping[str, Any], staged["event"])
            metadata = event.get("metadata")
            expected_ids = staged.get("representation_ids")
            matching_state_events = [
                candidate
                for candidate in self._artifacts
                if candidate.get("artifact_id") == event.get("artifact_id")
            ]
            if (
                event.get("execution_id") != self.execution_id
                or event.get("package_name") != self.package_name
                or event.get("package_id") != self.package_id
                or not isinstance(metadata, dict)
                or metadata.get("service_instance_id") != self.service_instance_id
                or metadata.get("command_id") != staged.get("command_id")
                or metadata.get("representation_ids") != expected_ids
                or expected_ids != visible_ids
                or metadata.get("scene_digest") != staged.get("scene_digest")
                or staged.get("scene_digest") != current_scene_digest
                or matching_state_events != [event]
            ):
                raise RuntimeError(
                    "ParaView artifact identity does not match the execution-owned scene"
                )

    def _drain_retired_proxies(self) -> None:
        """Best-effort disposal after semantic commit; retain failures for retry."""
        remaining: List[Any] = []
        for proxy in self._retired_proxies:
            try:
                self.simple.Delete(proxy)
            except Exception:
                remaining.append(proxy)
        self._retired_proxies = remaining

    def _queue_proxy_delete(self, proxy: Any) -> None:
        """Queue one unique proxy while keeping failed cleanup memory bounded."""
        if any(_same_proxy(proxy, pending) for pending in self._pending_deletes):
            return
        if (
            len(self._retired_proxies) + len(self._pending_deletes)
            >= MAX_RETIRED_PROXIES
        ):
            raise CommandError(
                "cleanup_backlog",
                "ParaView proxy cleanup backlog reached its bounded limit",
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            )
        self._pending_deletes.append(proxy)

    def rollback_command(self, checkpoint: object) -> None:
        """Restore an exact scene checkpoint after any post-execute failure."""
        if not self._transaction_open:
            raise RuntimeError("no ParaView command transaction is active")
        if not isinstance(checkpoint, dict):
            raise RuntimeError("ParaView command checkpoint is invalid")
        rollback_errors: List[str] = []
        old_displays = cast(Dict[str, Any], checkpoint["representation_displays"])
        old_transfer_proxies = cast(
            Dict[str, Tuple[Any, ...]],
            checkpoint["representation_transfer_proxies"],
        )
        old_proxies = cast(Dict[str, Any], checkpoint["node_proxies"])
        new_transfer_proxies: List[Any] = []
        representations_with_new_scalar_bars: set[str] = set()
        for representation_id, proxies in self._representation_transfer_proxies.items():
            representation_has_new_proxy = False
            for proxy in proxies:
                existed_before = any(
                    _same_proxy(proxy, old_proxy)
                    for old_proxies_for_representation in (
                        old_transfer_proxies.values()
                    )
                    for old_proxy in old_proxies_for_representation
                )
                already_queued = any(
                    _same_proxy(proxy, queued) for queued in new_transfer_proxies
                )
                if not existed_before and not already_queued:
                    new_transfer_proxies.append(proxy)
                    representation_has_new_proxy = True
            if representation_has_new_proxy and len(proxies) == 3:
                representations_with_new_scalar_bars.add(representation_id)
        for representation_id in sorted(representations_with_new_scalar_bars):
            display = self._representation_displays.get(representation_id)
            scalar_bar_setter = getattr(display, "SetScalarBarVisibility", None)
            if not callable(scalar_bar_setter):
                rollback_errors.append(
                    f"hide scalar bar for representation {representation_id}: "
                    "display cannot control its scalar bar"
                )
                continue
            try:
                scalar_bar_setter(self.view, False)
            except Exception as exc:
                rollback_errors.append(
                    f"hide scalar bar for representation {representation_id}: {exc}"
                )
        for proxy in new_transfer_proxies:
            try:
                self.simple.Delete(proxy)
            except Exception as exc:
                rollback_errors.append(f"delete transfer proxy: {exc}")
        for representation_id, display in list(self._representation_displays.items()):
            if representation_id in old_displays:
                continue
            try:
                display.Visibility = 0
                self.simple.Delete(display)
            except Exception as exc:
                rollback_errors.append(
                    f"delete representation {representation_id}: {exc}"
                )
        for node_id, proxy in reversed(list(self._node_proxies.items())):
            if node_id in old_proxies:
                continue
            try:
                self.simple.Delete(proxy)
            except Exception as exc:
                rollback_errors.append(f"delete node {node_id}: {exc}")
        old_artifacts = cast(List[Dict[str, Any]], checkpoint["artifacts"])
        for staged in self._staged_artifacts.values():
            try:
                cast(Path, staged["staged_path"]).unlink(missing_ok=True)
            except Exception as exc:
                rollback_errors.append(f"remove staged artifact: {exc}")
        self._staged_artifacts = {}
        self._node_proxies = old_proxies
        self._nodes = {
            record["node_id"]: record
            for record in cast(List[Dict[str, Any]], checkpoint["nodes"])
        }
        self._representation_displays = old_displays
        self._representation_transfer_proxies = old_transfer_proxies
        self._representations = {
            record["representation_id"]: record
            for record in cast(List[Dict[str, Any]], checkpoint["representations"])
        }
        self._measurements = {
            record["measurement_id"]: record
            for record in cast(List[Dict[str, Any]], checkpoint["measurements"])
        }
        self._selection = cast(Optional[Dict[str, Any]], checkpoint["selection"])
        self._artifacts = old_artifacts
        self._pending_deletes = []
        try:
            self._update_time(cast(int, checkpoint["timestep_index"]))
            self._timestep_index = cast(int, checkpoint["timestep_index"])
            for record in self._representations.values():
                self._apply_representation_record(record)
            _apply_camera_state(
                self.view,
                cast(Mapping[str, Any], checkpoint["camera"]),
            )
            self._set_active_source(checkpoint["active_source"])
            self.simple.Render(self.view)
        except Exception as exc:
            rollback_errors.append(f"restore rendered scene: {exc}")
        self._transaction_open = False
        if rollback_errors:
            raise RuntimeError(
                "ParaView command rollback was incomplete: "
                + "; ".join(rollback_errors)
            )

    def _validate_timestep_index(self, value: object) -> int:
        index = _bounded_int(value, "index", minimum=0)
        if not self._timesteps:
            if index != 0:
                raise CommandError(
                    "timestep_out_of_range",
                    "static datasets only expose timestep index 0",
                )
            return index
        if index >= len(self._timesteps):
            raise CommandError(
                "timestep_out_of_range",
                "timestep index exceeds the discovered series",
            )
        return index

    def _timestep_indices(
        self,
        value: object,
        *,
        nullable: bool,
    ) -> Optional[List[int]]:
        if nullable and value is None:
            return None
        if not isinstance(value, list) or not 1 <= len(value) <= (
            MAX_TIMESTEPS_PER_COMMAND
        ):
            raise CommandError(
                "invalid_arguments",
                "timestep_indices must be a nonempty bounded list",
                details={"maximum": MAX_TIMESTEPS_PER_COMMAND},
            )
        indices = [self._validate_timestep_index(item) for item in value]
        if len(set(indices)) != len(indices):
            raise CommandError("invalid_arguments", "timestep_indices must be unique")
        return indices

    def _update_time(self, index: int) -> None:
        """Update the complete node graph at one validated timestep."""
        self._validate_timestep_index(index)
        if self._reader_timesteps:
            reader_value = self._reader_timesteps[index]
            self.view.ViewTime = reader_value
            for proxy in self._node_proxies.values():
                proxy.UpdatePipeline(reader_value)
        else:
            for proxy in self._node_proxies.values():
                proxy.UpdatePipeline()

    def _refreshed_node_records(self) -> Dict[str, Dict[str, Any]]:
        refreshed: Dict[str, Dict[str, Any]] = {}
        for node_id, record in self._nodes.items():
            refreshed_record = _json_copy(record)
            refreshed_record["output"] = self._output_summary(
                self._node_proxies[node_id],
                topology=record["output"]["topology"],
            )
            refreshed[node_id] = refreshed_record
        return refreshed

    def _refreshed_representation_records(
        self,
        nodes: Mapping[str, Mapping[str, Any]],
    ) -> Dict[str, Dict[str, Any]]:
        refreshed: Dict[str, Dict[str, Any]] = {}
        for representation_id, record in self._representations.items():
            candidate = _json_copy(record)
            color = candidate["color"]
            if color["mode"] == "field":
                node_id = candidate["node_id"]
                field = color["field"]
                array = self._find_array(
                    nodes[node_id]["output"],
                    name=field["name"],
                    association=field["association"],
                )
                if array["components"] != field["components"]:
                    raise CommandError(
                        "field_shape_changed",
                        "representation field component count changed at timestep",
                    )
                source = self._node_proxies[node_id]
                observed_range = self._array_range(
                    source,
                    field["name"],
                    field["association"],
                    field["components"],
                )
                tuple_count = self._array_tuple_count(
                    source,
                    field["name"],
                    field["association"],
                    field["components"],
                )
                color["observation"] = {
                    "observed_range": observed_range,
                    "tuple_count": tuple_count,
                    "value_mode": (
                        "scalar" if field["components"] == 1 else "magnitude"
                    ),
                }
                if color["range_policy"]["mode"] == "full":
                    color["transfer_range"] = list(observed_range)
                _validate_scale_range(color["scale"], color["transfer_range"])
            refreshed[representation_id] = candidate
        return refreshed

    def _apply_representation_record(self, record: Mapping[str, Any]) -> None:
        """Apply one actor while owning its private color and opacity proxies."""
        representation_id = cast(str, record["representation_id"])
        display = self._representation_displays[representation_id]
        display.Visibility = 1 if record["visible"] else 0
        display.Opacity = float(record["opacity"])
        if record["type"] == "surface":
            display.Representation = "Surface"
        else:
            display.Representation = "Points"
            display.PointSize = float(record["point_size_px"])
        scalar_bar_setter = getattr(display, "SetScalarBarVisibility", None)
        if not callable(scalar_bar_setter):
            raise RuntimeError("ParaView display cannot control its scalar bar")
        scalar_bar_setter(self.view, False)
        color = cast(Mapping[str, Any], record["color"])
        if color["mode"] == "solid":
            self.simple.ColorBy(display, None)
            self._replace_representation_transfer_proxies(representation_id, ())
            rgb = list(color["rgb"])
            display.DiffuseColor = rgb
            if hasattr(display, "AmbientColor"):
                display.AmbientColor = rgb
            return
        field = cast(Mapping[str, Any], color["field"])
        paraview_association = "POINTS" if field["association"] == "point" else "CELLS"
        color_field: Tuple[str, ...] = (
            (paraview_association, cast(str, field["name"]))
            if field["components"] == 1
            else (
                paraview_association,
                cast(str, field["name"]),
                "Magnitude",
            )
        )
        self.simple.ColorBy(display, color_field, separate=True)
        previous_proxies = self._representation_transfer_proxies.get(
            representation_id,
            (),
        )
        lookup = self.simple.GetColorTransferFunction(
            field["name"],
            display,
            separate=True,
        )
        candidate_proxies: Tuple[Any, ...] = (lookup,)
        self._representation_transfer_proxies[representation_id] = candidate_proxies
        opacity_getter = getattr(self.simple, "GetOpacityTransferFunction", None)
        if not callable(opacity_getter):
            raise RuntimeError(
                "ParaView runtime cannot create a representation-private opacity "
                "transfer function"
            )
        opacity = opacity_getter(
            field["name"],
            display,
            separate=True,
        )
        candidate_proxies = (opacity, lookup)
        self._representation_transfer_proxies[representation_id] = candidate_proxies
        scalar_bar_getter = getattr(self.simple, "GetScalarBar", None)
        if not callable(scalar_bar_getter):
            raise RuntimeError(
                "ParaView runtime cannot resolve a representation-private scalar bar"
            )
        scalar_bar = scalar_bar_getter(lookup, self.view)
        if scalar_bar is None:
            raise RuntimeError(
                "ParaView runtime returned no representation-private scalar bar"
            )
        candidate_proxies = (scalar_bar, opacity, lookup)
        self._representation_transfer_proxies[representation_id] = candidate_proxies
        scalar_bar_setter(self.view, False)
        if not lookup.ApplyPreset(color["preset"], True):
            raise CommandError(
                "preset_not_found", "ParaView color preset was not found"
            )
        lookup.RescaleTransferFunction(*color["transfer_range"])
        opacity_rescale = getattr(opacity, "RescaleTransferFunction", None)
        if not callable(opacity_rescale):
            raise RuntimeError("ParaView opacity transfer function cannot be rescaled")
        opacity_rescale(*color["transfer_range"])
        self._set_lookup_scale(lookup, color["scale"]["mode"])
        if color["invert"]:
            lookup.InvertTransferFunction()
        self._retire_replaced_transfer_proxies(
            previous_proxies,
            candidate_proxies,
        )
        scalar_bar_setter(
            self.view,
            bool(record["visible"]) and color["scalar_bar"]["visible"],
        )

    def _replace_representation_transfer_proxies(
        self,
        representation_id: str,
        candidate_proxies: Tuple[Any, ...],
    ) -> None:
        """Install exact transfer ownership and queue proxies no longer referenced."""
        previous_proxies = self._representation_transfer_proxies.get(
            representation_id,
            (),
        )
        if candidate_proxies:
            self._representation_transfer_proxies[representation_id] = candidate_proxies
        else:
            self._representation_transfer_proxies.pop(representation_id, None)
        self._retire_replaced_transfer_proxies(
            previous_proxies,
            candidate_proxies,
        )

    def _retire_replaced_transfer_proxies(
        self,
        previous_proxies: Tuple[Any, ...],
        candidate_proxies: Tuple[Any, ...],
    ) -> None:
        """Retire only transfer proxies no current representation still owns."""
        for previous in previous_proxies:
            if any(_same_proxy(previous, current) for current in candidate_proxies):
                continue
            if any(
                _same_proxy(previous, owned)
                for proxies in self._representation_transfer_proxies.values()
                for owned in proxies
            ):
                continue
            self._queue_proxy_delete(previous)

    def _output_summary(
        self,
        source: Any,
        *,
        topology: str,
    ) -> Dict[str, Any]:
        """Describe one live node output without inferring scientific meaning."""
        information = source.GetDataInformation()
        raw_data_type: Optional[str] = None
        for getter_name in ("GetDataClassName", "GetDataSetTypeAsString"):
            getter = getattr(information, getter_name, None)
            if not callable(getter):
                continue
            try:
                candidate = getter()
            except (AttributeError, RuntimeError, TypeError, ValueError):
                continue
            if isinstance(candidate, str) and candidate:
                raw_data_type = candidate
                break
        point_count = _optional_nonnegative_count(
            information,
            "GetNumberOfPoints",
        )
        cell_count = _optional_nonnegative_count(
            information,
            "GetNumberOfCells",
        )
        bounds = self._discover_bounds(source)
        if point_count == 0 and cell_count == 0:
            bounds = None
        return {
            "topology": topology,
            "raw_data_type": raw_data_type,
            "bounds": None if bounds is None else list(bounds),
            "point_count": point_count,
            "cell_count": cell_count,
            "arrays": self._discover_arrays(source),
        }

    def _discover_arrays(self, source: Any) -> List[Dict[str, Any]]:
        information = source.GetDataInformation()
        arrays: List[Dict[str, Any]] = []
        attribute_sets = (
            ("point", information.GetPointDataInformation()),
            ("cell", information.GetCellDataInformation()),
        )
        counts: Dict[str, int] = {}
        for association, attribute_information in attribute_sets:
            count = attribute_information.GetNumberOfArrays()
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise RuntimeError("ParaView array count is invalid")
            counts[association] = count
        if sum(counts.values()) > MAX_DISCOVERED_ARRAYS:
            raise RuntimeError(
                f"ParaView point and cell array count exceeds {MAX_DISCOVERED_ARRAYS}"
            )
        for association, attribute_information in attribute_sets:
            for index in range(counts[association]):
                array = attribute_information.GetArrayInformation(index)
                if array is None:
                    continue
                name = array.GetName()
                if not isinstance(name, str) or not name:
                    continue
                components = array.GetNumberOfComponents()
                if (
                    isinstance(components, bool)
                    or not isinstance(components, int)
                    or not 1 <= components <= 256
                ):
                    raise RuntimeError("ParaView array component count is invalid")
                arrays.append(
                    {
                        "name": name,
                        "association": association,
                        "components": components,
                        "units": None,
                    }
                )
        return arrays

    def _find_array(
        self,
        output: Mapping[str, Any],
        *,
        name: str,
        association: str,
    ) -> Dict[str, Any]:
        for array in output["arrays"]:
            if array["name"] == name and array["association"] == association:
                return cast(Dict[str, Any], array)
        raise CommandError(
            "field_not_found", "field is not present in the selected node output"
        )

    def _array_information(
        self,
        source: Any,
        name: str,
        association: str,
        components: int,
    ) -> Any:
        information = source.GetDataInformation()
        point_attributes = information.GetPointDataInformation()
        cell_attributes = information.GetCellDataInformation()
        attributes_by_association = {
            "point": point_attributes,
            "cell": cell_attributes,
        }
        counts: Dict[str, int] = {}
        for current_association, attributes in attributes_by_association.items():
            count = attributes.GetNumberOfArrays()
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise RuntimeError("ParaView array count is invalid")
            counts[current_association] = count
        if sum(counts.values()) > MAX_DISCOVERED_ARRAYS:
            raise RuntimeError(
                f"ParaView point and cell array count exceeds {MAX_DISCOVERED_ARRAYS}"
            )
        attributes = attributes_by_association[association]
        for index in range(counts[association]):
            candidate = attributes.GetArrayInformation(index)
            if candidate is not None and str(candidate.GetName()) == name:
                current_components = int(candidate.GetNumberOfComponents())
                if current_components != components:
                    raise CommandError(
                        "field_shape_changed",
                        "field component count changed in current ParaView data",
                    )
                return candidate
        raise CommandError(
            "field_not_found", "field is absent from current ParaView array information"
        )

    def _array_range(
        self,
        source: Any,
        name: str,
        association: str,
        components: int,
    ) -> List[float]:
        information = self._array_information(
            source,
            name,
            association,
            components,
        )
        component = 0 if components == 1 else -1
        for method_name in ("GetComponentFiniteRange", "GetComponentRange"):
            method = getattr(information, method_name, None)
            if not callable(method):
                continue
            try:
                values = cast(Sequence[Any], method(component))
                scalar_range = [float(values[0]), float(values[1])]
            except (IndexError, TypeError, ValueError, OverflowError):
                continue
            if (
                all(math.isfinite(value) for value in scalar_range)
                and scalar_range[0] <= scalar_range[1]
            ):
                return scalar_range
        raise CommandError(
            "field_range_unavailable",
            "ParaView did not expose a finite field range",
        )

    def _array_tuple_count(
        self,
        source: Any,
        name: str,
        association: str,
        components: int,
    ) -> Optional[int]:
        information = self._array_information(
            source,
            name,
            association,
            components,
        )
        getter = getattr(information, "GetNumberOfTuples", None)
        if not callable(getter):
            return None
        try:
            value = getter()
        except (AttributeError, RuntimeError, TypeError, ValueError, OverflowError):
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value

    def _measure_distribution(
        self,
        source: Any,
        *,
        name: str,
        association: str,
        components: int,
        observed_range: Sequence[float],
        tuple_count: Optional[int],
    ) -> Dict[str, Any]:
        """Measure one node through ParaView's bounded Histogram filter."""
        previous_active = self._active_source()
        histogram: Any = None
        try:
            histogram = self.simple.Histogram(Input=source)
            histogram.SelectInputArray = [
                "POINTS" if association == "point" else "CELLS",
                name,
            ]
            histogram.Component = 0 if components == 1 else components
            histogram.BinCount = DISTRIBUTION_HISTOGRAM_BINS
            histogram.CalculateAverages = 0
            histogram.Normalize = 0
            histogram.CenterBinsAroundMinAndMax = 0
            histogram.UpdatePipeline()
            table = self.servermanager.Fetch(histogram)
            rows = _histogram_rows(table, maximum=DISTRIBUTION_HISTOGRAM_BINS)
            return _histogram_evidence(
                rows,
                observed_range=observed_range,
                tuple_count=tuple_count,
                method="paraview.histogram-filter",
            )
        except (AttributeError, RuntimeError, TypeError, ValueError, OverflowError):
            return {
                "status": "unavailable",
                "reason": "paraview_histogram_filter_unavailable",
            }
        finally:
            if histogram is not None:
                self.simple.Delete(histogram)
            self._set_active_source(previous_active)

    def _set_lookup_scale(self, lookup: Any, mode: str) -> None:
        current_log = bool(getattr(lookup, "UseLogScale", False))
        if mode == "log":
            if not current_log:
                mapper = getattr(lookup, "MapControlPointsToLogSpace", None)
                if not callable(mapper) or mapper() is False:
                    raise RuntimeError(
                        "ParaView could not map transfer points to native log scale"
                    )
            lookup.UseLogScale = 1
            return
        if current_log:
            mapper = getattr(lookup, "MapControlPointsToLinearSpace", None)
            if not callable(mapper) or mapper() is False:
                raise RuntimeError(
                    "ParaView could not map transfer points back to linear scale"
                )
        lookup.UseLogScale = 0

    def _discover_bounds(self, source: Any) -> Optional[Tuple[float, ...]]:
        information = source.GetDataInformation()
        try:
            bounds = tuple(float(value) for value in information.GetBounds())
        except (AttributeError, RuntimeError, TypeError, ValueError, OverflowError):
            return None
        if len(bounds) != 6 or not all(math.isfinite(value) for value in bounds):
            return None
        if any(bounds[index] > bounds[index + 1] for index in (0, 2, 4)):
            return None
        return bounds

    def _discover_reader_timesteps(self) -> List[float]:
        values = getattr(self.reader, "TimestepValues", None)
        if values is None:
            return []
        discovered = [float(value) for value in values]
        if len(discovered) > 512:
            raise RuntimeError("ParaView reader timestep count exceeds 512")
        if any(not math.isfinite(value) for value in discovered):
            raise RuntimeError("ParaView reader exposed non-finite timesteps")
        return discovered

    def _resolve_timesteps(self, reader_values: Sequence[float]) -> List[float]:
        member_values = [
            member.get("timestep") for member in self.descriptor["members"]
        ]
        descriptor_has_time = any(value is not None for value in member_values)
        if descriptor_has_time:
            if any(value is None for value in member_values):
                raise RuntimeError(
                    "dataset descriptor cannot mix timed and untimed members"
                )
            physical = [float(cast(float, value)) for value in member_values]
            if (
                len(physical) > 1
                and len(reader_values) != len(physical)
                or len(physical) == 1
                and reader_values
                and len(reader_values) != 1
            ):
                raise RuntimeError(
                    "ParaView reader timestep count differs from descriptor members"
                )
            return physical
        return list(reader_values)

    def _known_node_id(self, value: object) -> str:
        node_id = _bounded_text(value, "node_id", maximum=256)
        if node_id not in self._nodes:
            raise CommandError("node_not_found", "scene node does not exist")
        return node_id

    def _known_representation_id(self, value: object) -> str:
        representation_id = _bounded_text(
            value,
            "representation_id",
            maximum=256,
        )
        if representation_id not in self._representations:
            raise CommandError(
                "representation_not_found", "scene representation does not exist"
            )
        return representation_id

    def _known_measurement_id(self, value: object) -> str:
        measurement_id = _bounded_text(
            value,
            "measurement_id",
            maximum=256,
        )
        if measurement_id not in self._measurements:
            raise CommandError(
                "measurement_not_found", "scene measurement does not exist"
            )
        return measurement_id

    def _representation_id_list(
        self,
        value: object,
        *,
        visible: bool,
    ) -> List[str]:
        if not isinstance(value, list) or not 1 <= len(value) <= MAX_REPRESENTATIONS:
            raise CommandError(
                "invalid_arguments",
                "representation_ids must be a nonempty bounded list",
                details={"maximum": MAX_REPRESENTATIONS},
            )
        ids = [self._known_representation_id(item) for item in value]
        if ids != sorted(ids) or len(set(ids)) != len(ids):
            raise CommandError(
                "invalid_arguments",
                "representation_ids must be unique and lexically sorted",
            )
        if visible:
            hidden = [
                representation_id
                for representation_id in ids
                if not self._representations[representation_id]["visible"]
            ]
            if hidden:
                raise CommandError(
                    "representation_not_visible",
                    "operation requires visible representations",
                    details={"representation_ids": hidden},
                )
        return ids

    def _camera_state(self) -> Dict[str, Any]:
        state = {
            "position": [float(value) for value in self.view.CameraPosition],
            "focal_point": [float(value) for value in self.view.CameraFocalPoint],
            "view_up": [float(value) for value in self.view.CameraViewUp],
            "parallel_scale": float(self.view.CameraParallelScale),
            "projection": (
                "parallel"
                if bool(self.view.CameraParallelProjection)
                else "perspective"
            ),
            "view_angle": float(self.view.CameraViewAngle),
        }
        _validate_camera_geometry(state)
        return state

    def _active_source(self) -> Any:
        getter = getattr(self.simple, "GetActiveSource", None)
        return getter() if callable(getter) else None

    def _set_active_source(self, source: Any) -> None:
        setter = getattr(self.simple, "SetActiveSource", None)
        if callable(setter):
            setter(source)

    def _scene_digest(self) -> str:
        scene = self.pipeline_state()
        del scene["artifacts"]
        payload = json.dumps(
            scene,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(payload).hexdigest()


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the real ParaView service until a scheduler or operator stops it."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--descriptor", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--bind-host", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--execution-id", required=True)
    parser.add_argument("--service-instance-id", required=True)
    parser.add_argument("--authorization-file", required=True)
    args = parser.parse_args(argv)
    bound_execution_id = os.environ.get("JARVIS_EXECUTION_ID")
    package_name = os.environ.get("JARVIS_PACKAGE_NAME")
    package_id = os.environ.get("JARVIS_PACKAGE_ID")
    if bound_execution_id != args.execution_id:
        raise RuntimeError("CLI execution identity does not match JARVIS_EXECUTION_ID")
    if not package_name or not package_id:
        raise RuntimeError("ParaView service package identity bindings are missing")
    bearer_token = _read_authorization_token(Path(args.authorization_file))
    descriptor_path = Path(args.descriptor)
    descriptor = _load_json_file(descriptor_path)
    backend = ParaViewBackend(
        descriptor=descriptor,
        output_dir=Path(args.output_dir),
        service_instance_id=args.service_instance_id,
        execution_id=args.execution_id,
        package_name=package_name,
        package_id=package_id,
    )
    controller = ServiceStateController(
        backend=backend,
        execution_id=args.execution_id,
        package_name=package_name,
        package_id=package_id,
        service_instance_id=args.service_instance_id,
    )
    server = create_server(
        args.bind_host,
        args.port,
        controller,
        bearer_token,
    )

    def stop(_signum: int, _frame: object) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
    return 0


def _read_authorization_token(path: Path) -> str:
    """Read one private bearer capability without exposing it in process logs."""
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise ValueError("authorization file must be a regular absolute file")
    metadata = path.stat()
    if metadata.st_size > 128:
        raise ValueError("authorization file exceeds its bounded size")
    if os.name != "nt" and stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ValueError("authorization file must not grant group or other access")
    token = path.read_text(encoding="ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{64}", token):
        raise ValueError(
            "authorization token must be 64 lowercase hexadecimal characters"
        )
    return token


def _deterministic_id(prefix: str, seed: str) -> str:
    """Return one bounded deterministic service-object identifier."""
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}_{digest}"


def _descriptor_topology(value: object) -> str:
    """Use only the descriptor's authoritative semantic dataset kind."""
    if not isinstance(value, str):
        return "unknown"
    tokens = set(re.findall(r"[a-z0-9]+", value.casefold()))
    if tokens & {"point", "points", "particle", "particles"}:
        return "points"
    if tokens & {"surface", "mesh"}:
        return "surface"
    if tokens & {"volume", "volumetric"}:
        return "volume"
    if tokens & {"table", "tabular"}:
        return "table"
    if tokens & {"composite", "multiblock"}:
        return "composite"
    return "unknown"


def _ambiguous_element_data_type(value: object) -> bool:
    """Return whether one process-local index lacks a unique composite identity."""
    if not isinstance(value, str):
        return False
    lowered = value.casefold()
    return any(
        token in lowered
        for token in (
            "composite",
            "multiblock",
            "partitioned",
            "hierarchical",
            "amr",
        )
    )


def _optional_nonnegative_count(value: Any, getter_name: str) -> Optional[int]:
    getter = getattr(value, getter_name, None)
    if not callable(getter):
        return None
    try:
        result = getter()
    except (AttributeError, RuntimeError, TypeError, ValueError, OverflowError):
        return None
    if isinstance(result, bool) or not isinstance(result, int) or result < 0:
        return None
    return result


def _rgb(value: object) -> List[float]:
    if not isinstance(value, list) or len(value) != 3:
        raise CommandError("invalid_arguments", "rgb must contain three values")
    parsed = [_finite_number(component, "rgb") for component in value]
    if any(component < 0.0 or component > 1.0 for component in parsed):
        raise CommandError("invalid_arguments", "rgb values must be between 0 and 1")
    return parsed


def _union_bounds(
    current: Optional[List[float]],
    candidate: Sequence[float],
) -> List[float]:
    if len(candidate) != 6 or not all(
        math.isfinite(float(value)) for value in candidate
    ):
        raise ValueError("bounds must contain six finite values")
    rendered = [float(value) for value in candidate]
    if any(rendered[index] > rendered[index + 1] for index in (0, 2, 4)):
        raise ValueError("bounds must be ordered")
    if current is None:
        return rendered
    return [
        min(current[0], rendered[0]),
        max(current[1], rendered[1]),
        min(current[2], rendered[2]),
        max(current[3], rendered[3]),
        min(current[4], rendered[4]),
        max(current[5], rendered[5]),
    ]


def _aggregate_measurement_samples(
    samples: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Combine bounded per-timestep histograms without claiming raw quantiles."""
    if not samples:
        raise ValueError("measurement samples cannot be empty")
    observed_range = [
        min(float(sample["observed_range"][0]) for sample in samples),
        max(float(sample["observed_range"][1]) for sample in samples),
    ]
    tuple_counts = [sample["tuple_count"] for sample in samples]
    tuple_count: Optional[int] = (
        sum(cast(List[int], tuple_counts))
        if all(
            isinstance(value, int) and not isinstance(value, bool)
            for value in tuple_counts
        )
        else None
    )
    distributions = [sample["distribution"] for sample in samples]
    if any(distribution.get("status") != "available" for distribution in distributions):
        return {
            "observed_range": observed_range,
            "tuple_count": tuple_count,
            "distribution": {
                "status": "unavailable",
                "reason": "one_or_more_timestep_histograms_unavailable",
            },
        }
    lower, upper = observed_range
    if lower == upper:
        edges = [lower, upper]
        counts = [
            sum(float(distribution["finite_count"]) for distribution in distributions)
        ]
    else:
        edges = [
            lower + (upper - lower) * index / DISTRIBUTION_HISTOGRAM_BINS
            for index in range(DISTRIBUTION_HISTOGRAM_BINS + 1)
        ]
        counts = [0.0] * DISTRIBUTION_HISTOGRAM_BINS
        for distribution in distributions:
            histogram = distribution["histogram"]
            _rebin_histogram(
                [float(value) for value in histogram["bin_edges"]],
                [float(value) for value in histogram["counts"]],
                target_edges=edges,
                target_counts=counts,
            )
    total = sum(counts)
    if total <= 0 or not math.isfinite(total):
        raise ValueError("aggregate histogram contains no finite observations")
    rendered_counts: List[Any] = [
        int(value) if value.is_integer() else value for value in counts
    ]
    finite_count: Any = int(total) if total.is_integer() else total
    nonfinite_count: Optional[int] = None
    if (
        tuple_count is not None
        and isinstance(finite_count, int)
        and finite_count <= tuple_count
    ):
        nonfinite_count = tuple_count - finite_count
    distribution = {
        "status": "available",
        "method": "aggregate-of-paraview-histogram-filter",
        "bin_count": len(counts),
        "finite_count": finite_count,
        "nonfinite_count": nonfinite_count,
        "estimator": "uniform-within-source-and-aggregate-bins",
        "histogram": {"bin_edges": edges, "counts": rendered_counts},
        "percentiles": [
            {
                "percentile": percentile,
                "value": _histogram_percentile(edges, counts, percentile),
            }
            for percentile in DISTRIBUTION_PERCENTILES
        ],
        "log_scale_eligible": lower > 0 and lower < upper,
    }
    return {
        "observed_range": observed_range,
        "tuple_count": tuple_count,
        "distribution": distribution,
    }


def _rebin_histogram(
    source_edges: Sequence[float],
    source_counts: Sequence[float],
    *,
    target_edges: Sequence[float],
    target_counts: List[float],
) -> None:
    """Conservatively redistribute source-bin counts by uniform overlap."""
    if len(source_edges) != len(source_counts) + 1:
        raise ValueError("source histogram shape is invalid")
    for index, count in enumerate(source_counts):
        left = source_edges[index]
        right = source_edges[index + 1]
        if count == 0:
            continue
        if right == left:
            target_index = min(
                len(target_counts) - 1,
                max(0, _histogram_target_index(target_edges, left)),
            )
            target_counts[target_index] += count
            continue
        for target_index in range(len(target_counts)):
            overlap = max(
                0.0,
                min(right, target_edges[target_index + 1])
                - max(left, target_edges[target_index]),
            )
            if overlap:
                target_counts[target_index] += count * overlap / (right - left)


def _histogram_target_index(edges: Sequence[float], value: float) -> int:
    for index in range(len(edges) - 1):
        if value <= edges[index + 1]:
            return index
    return len(edges) - 2


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
    dataset_id = value.get("dataset_id")
    if (
        not isinstance(dataset_id, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", dataset_id) is None
    ):
        raise ValueError("dataset descriptor dataset_id is invalid")
    _descriptor_text(value.get("kind"), "kind", maximum=256)
    _descriptor_text(value.get("format"), "format", maximum=256)
    members = value.get("members")
    if not isinstance(members, list) or not 1 <= len(members) <= 512:
        raise ValueError("dataset descriptor requires 1-512 members")
    locations: List[str] = []
    for expected_index, member in enumerate(members):
        if (
            not isinstance(member, dict)
            or set(member)
            - {
                "index",
                "location",
                "timestep",
            }
            or not {"index", "location"} <= set(member)
        ):
            raise ValueError("dataset member schema is invalid")
        if member.get("index") != expected_index:
            raise ValueError("dataset member indexes must be contiguous")
        location = cast(str, member.get("location"))
        _descriptor_text(location, "member location", maximum=4096)
        _normalized_absolute_path(location)
        locations.append(location)
        timestep = member.get("timestep")
        if timestep is not None and (
            isinstance(timestep, bool)
            or not isinstance(timestep, (int, float))
            or not math.isfinite(float(timestep))
        ):
            raise ValueError("dataset member timestep must be a finite number")
    if len(locations) != len(set(locations)):
        raise ValueError("dataset member locations must be unique")
    arrays = value.get("arrays")
    if not isinstance(arrays, list) or len(arrays) > 256:
        raise ValueError("dataset descriptor arrays must be a bounded list")
    array_identities: set[Tuple[str, str]] = set()
    for array in arrays:
        if (
            not isinstance(array, dict)
            or set(array) - {"name", "association", "components", "units"}
            or not {"name", "association", "components"} <= set(array)
        ):
            raise ValueError("dataset array schema is invalid")
        name = _descriptor_text(array.get("name"), "array name", maximum=512)
        association = array.get("association")
        components = array.get("components")
        units = array.get("units")
        if association not in {"point", "cell", "field"}:
            raise ValueError("dataset array association is invalid")
        if (
            isinstance(components, bool)
            or not isinstance(components, int)
            or not 1 <= components <= 64
        ):
            raise ValueError("dataset array components must be between 1 and 64")
        if units is not None:
            _descriptor_text(units, "array units", maximum=256)
        identity = (cast(str, association), name)
        if identity in array_identities:
            raise ValueError("dataset array identities must be unique")
        array_identities.add(identity)
    bounds = value.get("bounds")
    if bounds is not None:
        if (
            not isinstance(bounds, list)
            or len(bounds) != 6
            or any(
                isinstance(item, bool)
                or not isinstance(item, (int, float))
                or not math.isfinite(float(item))
                for item in bounds
            )
            or any(bounds[index] > bounds[index + 1] for index in (0, 2, 4))
        ):
            raise ValueError("dataset descriptor bounds are invalid")
    source_artifact = value.get("source_artifact")
    if source_artifact is not None:
        if (
            not isinstance(source_artifact, dict)
            or set(source_artifact) != {"artifact_id", "sha256"}
            or not isinstance(source_artifact.get("artifact_id"), str)
            or re.fullmatch(
                r"art_[A-Za-z0-9_-]{22,86}",
                cast(str, source_artifact.get("artifact_id")),
            )
            is None
            or not isinstance(source_artifact.get("sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", cast(str, source_artifact.get("sha256")))
            is None
        ):
            raise ValueError("dataset source artifact is invalid")
    fingerprint = value.get("fingerprint")
    if (
        not isinstance(fingerprint, dict)
        or set(fingerprint) != {"algorithm", "digest"}
        or fingerprint.get("algorithm") != "sha256"
        or not isinstance(fingerprint.get("digest"), str)
        or re.fullmatch(r"[0-9a-f]{64}", cast(str, fingerprint.get("digest"))) is None
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


def _descriptor_text(value: object, label: str, *, maximum: int) -> str:
    """Validate one bounded printable descriptor string."""
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.encode("utf-8")) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"dataset descriptor {label} is invalid")
    return value


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


def _prepare_artifact_event(
    *,
    artifact_id: str,
    logical_name: str,
    path: Path,
    size_bytes: int,
    sha256: str,
    service_instance_id: str,
    command_id: str,
    representation_ids: Sequence[str],
    scene_digest: str,
    cluster_location: Optional[str] = None,
) -> Dict[str, Any]:
    """Reserve a deterministic artifact event without publishing durable state."""
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
        marker_path = _artifact_marker_path(artifact_path, artifact_id)
        marker = _read_artifact_marker(marker_path)
        marker_event: Optional[Mapping[str, Any]] = None
        if marker is not None:
            marker_event = cast(Mapping[str, Any], marker["event"])
            _validate_artifact_marker(marker, marker_event, path)
            _restore_artifact_ledger_prefix(artifact_path, marker)
            marker_stage = _validated_marker_stage_path(marker, path)
            if not path.exists() and not path.is_symlink():
                if marker_stage.is_file() and not marker_stage.is_symlink():
                    _validate_published_artifact_file(marker_event, marker_stage)
                elif marker_stage.exists() or marker_stage.is_symlink():
                    raise RuntimeError("ParaView artifact transaction stage is unsafe")
                else:
                    _remove_artifact_marker(marker_path)
                    marker = None
                    marker_event = None
        existing = _read_artifact_lines(artifact_path)
        for event in existing:
            metadata = event.get("metadata")
            if event.get("artifact_id") == artifact_id:
                _validate_artifact_event_request(
                    event,
                    logical_name=logical_name,
                    path=path,
                    size_bytes=size_bytes,
                    sha256=sha256,
                    service_instance_id=service_instance_id,
                    command_id=command_id,
                    representation_ids=representation_ids,
                    scene_digest=scene_digest,
                    cluster_location=cluster_location,
                    execution_id=cast(str, execution_id),
                    package_name=cast(str, package_name),
                    package_id=cast(str, package_id),
                )
                if marker is not None:
                    _validate_published_artifact_file(event, path)
                    _remove_artifact_marker(marker_path)
                return _json_copy(event)
            if (
                isinstance(metadata, dict)
                and metadata.get("service_instance_id") == service_instance_id
                and metadata.get("command_id") == command_id
            ):
                raise RuntimeError(
                    "ParaView command already published a different artifact ID"
                )
        sequence = 1
        if existing:
            sequence_value = existing[-1].get("sequence")
            if isinstance(sequence_value, bool) or not isinstance(sequence_value, int):
                raise RuntimeError("JARVIS artifact sidecar has an invalid sequence")
            sequence = sequence_value + 1
        if marker is not None:
            assert marker_event is not None
            _validate_artifact_event_request(
                marker_event,
                logical_name=logical_name,
                path=path,
                size_bytes=size_bytes,
                sha256=sha256,
                service_instance_id=service_instance_id,
                command_id=command_id,
                representation_ids=representation_ids,
                scene_digest=scene_digest,
                cluster_location=cluster_location,
                execution_id=cast(str, execution_id),
                package_name=cast(str, package_name),
                package_id=cast(str, package_id),
            )
            if marker_event.get("sequence") != sequence:
                raise RuntimeError(
                    "ParaView artifact transaction marker has a stale sequence"
                )
            if path.exists() or path.is_symlink():
                _validate_published_artifact_file(marker_event, path)
            return _json_copy(marker_event)
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
                "representation_ids": list(representation_ids),
                "scene_digest": scene_digest,
            },
        }
        if len(_artifact_event_payload(event)) > 64 * 1024:
            raise RuntimeError("ParaView artifact event exceeds the JARVIS limit")
    return event


def _validate_artifact_event_request(
    event: Mapping[str, Any],
    *,
    logical_name: str,
    path: Path,
    size_bytes: int,
    sha256: str,
    service_instance_id: str,
    command_id: str,
    representation_ids: Sequence[str],
    scene_digest: str,
    cluster_location: Optional[str],
    execution_id: str,
    package_name: str,
    package_id: str,
) -> None:
    """Require a published or recoverable event to match one exact retry."""
    metadata = event.get("metadata")
    if (
        event.get("logical_name") != logical_name
        or event.get("execution_id") != execution_id
        or event.get("package_name") != package_name
        or event.get("package_id") != package_id
        or event.get("location")
        != {"kind": "cluster_path", "value": cluster_location or path.as_posix()}
        or event.get("size_bytes") != size_bytes
        or event.get("checksum") != "sha256:" + sha256
        or not isinstance(metadata, dict)
        or metadata.get("service_instance_id") != service_instance_id
        or metadata.get("command_id") != command_id
        or metadata.get("representation_ids") != list(representation_ids)
        or metadata.get("scene_digest") != scene_digest
    ):
        raise RuntimeError(
            "deterministic ParaView artifact retry conflicts with durable state"
        )


def _artifact_event_payload(event: Mapping[str, Any]) -> bytes:
    """Encode one append-only JARVIS artifact event canonically."""
    return (
        json.dumps(
            event,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )


def _artifact_marker_path(artifact_path: Path, artifact_id: str) -> Path:
    """Return a private transaction marker name keyed by artifact identity."""
    key = hashlib.sha256(artifact_id.encode("utf-8")).hexdigest()
    return artifact_path.with_name(
        f".{artifact_path.name}.paraview-transaction-{key}.json"
    )


def _artifact_marker_record(
    event: Mapping[str, Any],
    output_path: Path,
    staged_path: Path,
    *,
    ledger_prefix: bytes,
    previous_sequence: int,
) -> Dict[str, Any]:
    """Build the canonical recovery record persisted before publication."""
    return {
        "schema_version": "jarvis.paraview.artifact-transaction.v1",
        "artifact_id": event.get("artifact_id"),
        "output_path": str(output_path),
        "staged_path": str(staged_path),
        "size_bytes": event.get("size_bytes"),
        "checksum": event.get("checksum"),
        "ledger_size": len(ledger_prefix),
        "ledger_sha256": hashlib.sha256(ledger_prefix).hexdigest(),
        "previous_sequence": previous_sequence,
        "event": _json_copy(event),
    }


def _artifact_marker_payload(marker: Mapping[str, Any]) -> bytes:
    return json.dumps(
        marker,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _validate_artifact_marker(
    marker: Mapping[str, Any],
    event: Mapping[str, Any],
    output_path: Path,
) -> None:
    if (
        marker.get("schema_version") != "jarvis.paraview.artifact-transaction.v1"
        or marker.get("artifact_id") != event.get("artifact_id")
        or marker.get("output_path") != str(output_path)
        or not isinstance(marker.get("staged_path"), str)
        or marker.get("size_bytes") != event.get("size_bytes")
        or marker.get("checksum") != event.get("checksum")
        or marker.get("event") != event
        or isinstance(marker.get("ledger_size"), bool)
        or not isinstance(marker.get("ledger_size"), int)
        or cast(int, marker["ledger_size"]) < 0
        or not isinstance(marker.get("ledger_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", cast(str, marker["ledger_sha256"]))
        or isinstance(marker.get("previous_sequence"), bool)
        or not isinstance(marker.get("previous_sequence"), int)
        or cast(int, marker["previous_sequence"]) < 0
        or event.get("sequence") != cast(int, marker["previous_sequence"]) + 1
    ):
        raise RuntimeError(
            "ParaView artifact transaction marker conflicts with the command"
        )
    staged_path = _validated_marker_stage_path(marker, output_path)
    if staged_path.exists() or staged_path.is_symlink():
        _validate_published_artifact_file(event, staged_path)


def _read_artifact_marker(path: Path) -> Optional[Dict[str, Any]]:
    """Read one exact private transaction marker without following symlinks."""
    if not path.exists():
        return None
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
        raise RuntimeError("ParaView artifact transaction marker is unsafe")
    information = path.stat()
    if information.st_size <= 0 or information.st_size > 128 * 1024:
        raise RuntimeError("ParaView artifact transaction marker has invalid size")
    if os.name != "nt" and information.st_mode & 0o077:
        raise RuntimeError("ParaView artifact transaction marker is not private")
    try:
        raw = path.read_bytes()
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(
            "ParaView artifact transaction marker is invalid JSON"
        ) from exc
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "artifact_id",
        "output_path",
        "staged_path",
        "size_bytes",
        "checksum",
        "ledger_size",
        "ledger_sha256",
        "previous_sequence",
        "event",
    }:
        raise RuntimeError("ParaView artifact transaction marker shape is invalid")
    marker = cast(Dict[str, Any], value)
    if (
        marker.get("schema_version") != "jarvis.paraview.artifact-transaction.v1"
        or not isinstance(marker.get("artifact_id"), str)
        or not isinstance(marker.get("output_path"), str)
        or not Path(cast(str, marker["output_path"])).is_absolute()
        or not isinstance(marker.get("staged_path"), str)
        or not Path(cast(str, marker["staged_path"])).is_absolute()
        or isinstance(marker.get("size_bytes"), bool)
        or not isinstance(marker.get("size_bytes"), int)
        or not isinstance(marker.get("checksum"), str)
        or isinstance(marker.get("ledger_size"), bool)
        or not isinstance(marker.get("ledger_size"), int)
        or cast(int, marker["ledger_size"]) < 0
        or not isinstance(marker.get("ledger_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", cast(str, marker["ledger_sha256"]))
        or isinstance(marker.get("previous_sequence"), bool)
        or not isinstance(marker.get("previous_sequence"), int)
        or cast(int, marker["previous_sequence"]) < 0
        or not isinstance(marker.get("event"), dict)
        or _artifact_marker_payload(marker) != raw
    ):
        raise RuntimeError("ParaView artifact transaction marker values are invalid")
    return marker


def _validated_marker_stage_path(marker: Mapping[str, Any], output_path: Path) -> Path:
    """Return the exact private stage bound to one transaction marker."""
    value = marker.get("staged_path")
    if not isinstance(value, str):
        raise RuntimeError("ParaView artifact marker staged path is invalid")
    staged_path = Path(value)
    if (
        not staged_path.is_absolute()
        or staged_path.parent != output_path.parent
        or not staged_path.name.startswith(".paraview-artifact.")
        or not staged_path.name.endswith(".tmp.png")
        or staged_path == output_path
    ):
        raise RuntimeError("ParaView artifact marker staged path is unsafe")
    if staged_path.is_symlink() or (staged_path.exists() and not staged_path.is_file()):
        raise RuntimeError("ParaView artifact marker staged path is unsafe")
    return staged_path


def _create_artifact_marker(path: Path, marker: Mapping[str, Any]) -> None:
    """Create and fsync one recovery marker without replacing any peer."""
    payload = _artifact_marker_payload(marker)
    if len(payload) > 128 * 1024:
        raise RuntimeError("ParaView artifact transaction marker is too large")
    descriptor = os.open(
        path,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError(
                    "short write while creating ParaView artifact transaction"
                )
            offset += written
        os.fsync(descriptor)
    except Exception:
        os.close(descriptor)
        path.unlink(missing_ok=True)
        _fsync_directory(path.parent)
        raise
    else:
        os.close(descriptor)
    _fsync_directory(path.parent)


def _restore_artifact_ledger_prefix(
    artifact_path: Path,
    marker: Mapping[str, Any],
) -> None:
    """Recover only the incomplete event tail attributable to one marker."""
    ledger_size = cast(int, marker["ledger_size"])
    expected_digest = cast(str, marker["ledger_sha256"])
    payload = artifact_path.read_bytes() if artifact_path.exists() else b""
    if len(payload) < ledger_size:
        raise RuntimeError(
            "JARVIS artifact ledger is shorter than its transaction prefix"
        )
    prefix = payload[:ledger_size]
    if hashlib.sha256(prefix).hexdigest() != expected_digest:
        raise RuntimeError(
            "JARVIS artifact ledger prefix changed during ParaView recovery"
        )
    prefix_events = _decode_artifact_lines(prefix)
    previous_sequence = cast(int, marker["previous_sequence"])
    actual_previous = 0
    if prefix_events:
        value = prefix_events[-1].get("sequence")
        if isinstance(value, bool) or not isinstance(value, int):
            raise RuntimeError("JARVIS artifact sidecar has an invalid sequence")
        actual_previous = value
    if actual_previous != previous_sequence:
        raise RuntimeError("ParaView artifact transaction prefix sequence changed")
    tail = payload[ledger_size:]
    if not tail:
        return
    event = cast(Mapping[str, Any], marker["event"])
    expected_tail = _artifact_event_payload(event)
    if tail == expected_tail:
        return
    if not expected_tail.startswith(tail):
        raise RuntimeError(
            "JARVIS artifact ledger has an unattributable transaction tail"
        )
    descriptor = os.open(
        artifact_path,
        os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.ftruncate(descriptor, ledger_size)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(artifact_path.parent)


def _remove_artifact_marker(path: Path) -> None:
    """Durably clear one completed transaction marker."""
    path.unlink(missing_ok=False)
    _fsync_directory(path.parent)


def _publish_artifact_event_locked(
    artifact_path: Path,
    event: Mapping[str, Any],
) -> None:
    """Append one event while preserving every previously durable ledger byte."""
    payload = _artifact_event_payload(event)
    if len(payload) > 64 * 1024:
        raise RuntimeError("ParaView artifact event exceeds the JARVIS limit")
    original_size = artifact_path.stat().st_size if artifact_path.exists() else 0
    descriptor: Optional[int] = None
    try:
        descriptor = _open_private_append(artifact_path)
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("short write while appending ParaView artifact event")
            offset += written
        os.fsync(descriptor)
    except Exception:
        if descriptor is not None:
            try:
                os.ftruncate(descriptor, original_size)
                os.fsync(descriptor)
            except OSError:
                pass
            os.close(descriptor)
            descriptor = None
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _commit_staged_artifacts(
    staged_artifacts: Mapping[str, Mapping[str, Any]],
) -> None:
    """Publish one validated PNG and its append-only event at semantic commit."""
    if not staged_artifacts:
        return
    if len(staged_artifacts) != 1:
        raise RuntimeError("one ParaView command may publish only one artifact")
    artifact_id, staged = next(iter(staged_artifacts.items()))
    event = staged.get("event")
    staged_path = staged.get("staged_path")
    output_path = staged.get("output_path")
    if (
        not isinstance(event, dict)
        or event.get("artifact_id") != artifact_id
        or not isinstance(staged_path, Path)
        or not isinstance(output_path, Path)
    ):
        raise RuntimeError("staged ParaView artifact record is invalid")
    artifact_path_value = os.environ.get("JARVIS_ARTIFACT_PATH")
    if not artifact_path_value:
        raise RuntimeError("JARVIS artifact bindings are missing")
    artifact_path = Path(artifact_path_value)
    if not artifact_path.is_absolute():
        raise RuntimeError("JARVIS artifact sidecar path must be absolute")
    artifact_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        artifact_path.parent.chmod(0o700)
    payload = _artifact_event_payload(event)
    if len(payload) > 64 * 1024:
        raise RuntimeError("ParaView artifact event exceeds the JARVIS limit")

    with _artifact_lock(artifact_path):
        marker_path = _artifact_marker_path(artifact_path, artifact_id)
        marker = _read_artifact_marker(marker_path)
        transaction_stage = staged_path
        if marker is not None:
            _validate_artifact_marker(marker, event, output_path)
            transaction_stage = _validated_marker_stage_path(marker, output_path)
            _restore_artifact_ledger_prefix(artifact_path, marker)
        existing = _read_artifact_lines(artifact_path)
        published = [
            candidate
            for candidate in existing
            if candidate.get("artifact_id") == artifact_id
        ]
        if published:
            if len(published) != 1 or published[0] != event:
                raise RuntimeError(
                    "deterministic ParaView artifact commit conflicts with the "
                    "published event"
                )
            _validate_published_artifact_file(event, output_path)
            if marker is not None:
                _remove_artifact_marker(marker_path)
            staged_path.unlink(missing_ok=True)
            if transaction_stage != staged_path:
                transaction_stage.unlink(missing_ok=True)
            return

        expected_sequence = 1
        if existing:
            sequence = existing[-1].get("sequence")
            if isinstance(sequence, bool) or not isinstance(sequence, int):
                raise RuntimeError("JARVIS artifact sidecar has an invalid sequence")
            expected_sequence = sequence + 1
        if event.get("sequence") != expected_sequence:
            raise RuntimeError(
                "JARVIS artifact sequence changed before ParaView command commit"
            )
        metadata = event.get("metadata")
        for candidate in existing:
            candidate_metadata = candidate.get("metadata")
            if (
                isinstance(metadata, dict)
                and isinstance(candidate_metadata, dict)
                and candidate_metadata.get("service_instance_id")
                == metadata.get("service_instance_id")
                and candidate_metadata.get("command_id") == metadata.get("command_id")
            ):
                raise RuntimeError(
                    "ParaView command already published a different artifact ID"
                )
        if marker is None:
            if output_path.exists() or output_path.is_symlink():
                raise CommandError(
                    "artifact_exists",
                    "export filename already exists without an attributable "
                    "ParaView transaction",
                    status=HTTPStatus.CONFLICT,
                )
            ledger_prefix = (
                artifact_path.read_bytes() if artifact_path.exists() else b""
            )
            marker = _artifact_marker_record(
                event,
                output_path,
                staged_path,
                ledger_prefix=ledger_prefix,
                previous_sequence=expected_sequence - 1,
            )
            _create_artifact_marker(marker_path, marker)
        else:
            _validate_artifact_marker(marker, event, output_path)

        if output_path.exists() or output_path.is_symlink():
            _validate_published_artifact_file(event, output_path)
        else:
            if not transaction_stage.is_file() or transaction_stage.is_symlink():
                raise RuntimeError("staged ParaView PNG is missing or unsafe")
            _validate_published_artifact_file(event, transaction_stage)
            os.link(transaction_stage, output_path)
            if os.name != "nt":
                output_path.chmod(0o600)
            _fsync_directory(output_path.parent)

        _publish_artifact_event_locked(artifact_path, event)
        _remove_artifact_marker(marker_path)
        staged_path.unlink(missing_ok=True)
        if transaction_stage != staged_path:
            transaction_stage.unlink(missing_ok=True)
        _fsync_directory(output_path.parent)


def _validate_published_artifact_file(
    event: Mapping[str, Any], output_path: Path
) -> None:
    """Require a durable PNG to match its exact published event."""
    if not output_path.is_file() or output_path.is_symlink():
        raise RuntimeError("published ParaView artifact file is missing or unsafe")
    payload = output_path.read_bytes()
    if (
        event.get("size_bytes") != len(payload)
        or event.get("checksum") != "sha256:" + hashlib.sha256(payload).hexdigest()
        or not payload.startswith(b"\x89PNG\r\n\x1a\n")
    ):
        raise RuntimeError("published ParaView artifact file failed validation")


def _find_artifact_event(artifact_id: str) -> Optional[Dict[str, Any]]:
    """Find a previously published deterministic event for command replay."""
    artifact_path_value = os.environ.get("JARVIS_ARTIFACT_PATH")
    if not artifact_path_value:
        raise RuntimeError("JARVIS artifact bindings are missing")
    artifact_path = Path(artifact_path_value)
    if not artifact_path.is_absolute():
        raise RuntimeError("JARVIS artifact sidecar path must be absolute")
    with _artifact_lock(artifact_path):
        marker_path = _artifact_marker_path(artifact_path, artifact_id)
        marker = _read_artifact_marker(marker_path)
        marker_stage: Optional[Path] = None
        if marker is not None:
            marker_event = cast(Mapping[str, Any], marker["event"])
            marker_output = Path(cast(str, marker["output_path"]))
            _validate_artifact_marker(marker, marker_event, marker_output)
            marker_stage = _validated_marker_stage_path(marker, marker_output)
            _restore_artifact_ledger_prefix(artifact_path, marker)
        matches = [
            event
            for event in _read_artifact_lines(artifact_path)
            if event.get("artifact_id") == artifact_id
        ]
        if len(matches) > 1:
            raise RuntimeError(
                "JARVIS artifact sidecar contains duplicate artifact IDs"
            )
        if matches:
            if marker is not None:
                output_path = Path(cast(str, marker["output_path"]))
                _validate_artifact_marker(marker, matches[0], output_path)
                _validate_published_artifact_file(matches[0], output_path)
                _remove_artifact_marker(marker_path)
                if marker_stage is not None:
                    marker_stage.unlink(missing_ok=True)
    return None if not matches else matches[0]


def _recover_artifact_transactions() -> None:
    """Finish attributable link-before-ledger transactions during startup."""
    artifact_path_value = os.environ.get("JARVIS_ARTIFACT_PATH")
    if not artifact_path_value:
        return
    artifact_path = Path(artifact_path_value)
    if not artifact_path.is_absolute():
        raise RuntimeError("JARVIS artifact sidecar path must be absolute")
    if not artifact_path.parent.exists():
        return
    pattern = f".{artifact_path.name}.paraview-transaction-*.json"
    marker_paths = sorted(artifact_path.parent.glob(pattern))
    if len(marker_paths) > MAX_ARTIFACTS:
        raise RuntimeError("too many pending ParaView artifact transactions")
    if not marker_paths:
        return
    with _artifact_lock(artifact_path):
        markers: List[Tuple[int, int, Path, Dict[str, Any]]] = []
        for marker_path in marker_paths:
            marker = _read_artifact_marker(marker_path)
            if marker is None:
                continue
            artifact_id = cast(str, marker["artifact_id"])
            if _artifact_marker_path(artifact_path, artifact_id) != marker_path:
                raise RuntimeError(
                    "ParaView artifact transaction marker name is invalid"
                )
            event = cast(Mapping[str, Any], marker["event"])
            output_path = Path(cast(str, marker["output_path"]))
            _validate_artifact_marker(marker, event, output_path)
            staged_path = _validated_marker_stage_path(marker, output_path)
            sequence = event.get("sequence")
            if isinstance(sequence, bool) or not isinstance(sequence, int):
                raise RuntimeError("ParaView artifact transaction sequence is invalid")
            markers.append(
                (cast(int, marker["ledger_size"]), sequence, marker_path, marker)
            )
        for _ledger_size, sequence, marker_path, marker in sorted(markers):
            event = cast(Mapping[str, Any], marker["event"])
            artifact_id = cast(str, marker["artifact_id"])
            output_path = Path(cast(str, marker["output_path"]))
            staged_path = _validated_marker_stage_path(marker, output_path)
            _restore_artifact_ledger_prefix(artifact_path, marker)
            existing = _read_artifact_lines(artifact_path)
            matches = [
                candidate
                for candidate in existing
                if candidate.get("artifact_id") == artifact_id
            ]
            if matches:
                if len(matches) != 1 or matches[0] != event:
                    raise RuntimeError(
                        "ParaView artifact recovery conflicts with durable history"
                    )
                _validate_published_artifact_file(event, output_path)
                _remove_artifact_marker(marker_path)
                staged_path.unlink(missing_ok=True)
                continue
            expected_sequence = 1
            if existing:
                previous = existing[-1].get("sequence")
                if isinstance(previous, bool) or not isinstance(previous, int):
                    raise RuntimeError(
                        "JARVIS artifact sidecar has an invalid sequence"
                    )
                expected_sequence = previous + 1
            if sequence != expected_sequence:
                raise RuntimeError(
                    "ParaView artifact recovery marker has a stale sequence"
                )
            if not output_path.exists() and not output_path.is_symlink():
                if staged_path.is_file() and not staged_path.is_symlink():
                    _validate_published_artifact_file(event, staged_path)
                    os.link(staged_path, output_path)
                    if os.name != "nt":
                        output_path.chmod(0o600)
                    _fsync_directory(output_path.parent)
                elif staged_path.exists() or staged_path.is_symlink():
                    raise RuntimeError("ParaView artifact recovery stage is unsafe")
                else:
                    _remove_artifact_marker(marker_path)
                    continue
            _validate_published_artifact_file(event, output_path)
            _publish_artifact_event_locked(artifact_path, event)
            _remove_artifact_marker(marker_path)
            staged_path.unlink(missing_ok=True)


def _validate_artifact_replay(
    event: Mapping[str, Any],
    *,
    logical_name: str,
    output_path: Path,
    service_instance_id: str,
    command_id: str,
    representation_ids: Sequence[str],
    scene_digest: str,
    execution_id: str,
    package_name: str,
    package_id: str,
) -> None:
    """Validate an on-disk event and PNG before returning replay success."""
    metadata = event.get("metadata")
    location = event.get("location")
    if (
        event.get("logical_name") != logical_name
        or event.get("execution_id") != execution_id
        or event.get("package_name") != package_name
        or event.get("package_id") != package_id
        or location != {"kind": "cluster_path", "value": output_path.as_posix()}
        or not isinstance(metadata, dict)
        or metadata.get("service_instance_id") != service_instance_id
        or metadata.get("command_id") != command_id
        or metadata.get("representation_ids") != list(representation_ids)
        or metadata.get("scene_digest") != scene_digest
    ):
        raise CommandError(
            "artifact_replay_conflict",
            "published artifact does not match the retried scene command",
            status=HTTPStatus.CONFLICT,
        )
    if not output_path.is_file() or output_path.is_symlink():
        raise RuntimeError("published ParaView artifact file is missing or unsafe")
    payload = output_path.read_bytes()
    expected_size = event.get("size_bytes")
    expected_checksum = event.get("checksum")
    if (
        isinstance(expected_size, bool)
        or not isinstance(expected_size, int)
        or len(payload) != expected_size
        or expected_checksum != "sha256:" + hashlib.sha256(payload).hexdigest()
    ):
        raise RuntimeError("published ParaView artifact file failed replay validation")


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
    return _decode_artifact_lines(path.read_bytes())


def _decode_artifact_lines(payload: bytes) -> List[Dict[str, Any]]:
    """Decode an exact newline-framed JARVIS artifact ledger payload."""
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


def _histogram_rows(table: Any, *, maximum: int) -> List[Tuple[float, float]]:
    """Read the bounded midpoint/count table emitted by vtkExtractHistogram."""

    if table is None:
        raise ValueError("ParaView histogram table is missing")
    row_count = table.GetNumberOfRows()
    column_count = table.GetNumberOfColumns()
    if (
        isinstance(row_count, bool)
        or not isinstance(row_count, int)
        or not 1 <= row_count <= maximum
        or isinstance(column_count, bool)
        or not isinstance(column_count, int)
        or column_count < 2
    ):
        raise ValueError("ParaView histogram table shape is invalid")
    coordinates = table.GetColumn(0)
    counts = table.GetColumn(1)
    component_getter = getattr(coordinates, "GetNumberOfComponents", None)
    if not callable(component_getter) or component_getter() != 1:
        raise ValueError(
            "ParaView histogram bin_extents must contain scalar bin midpoints"
        )
    rows: List[Tuple[float, float]] = []
    for index in range(row_count):
        coordinate = _histogram_column_number(coordinates, index)
        count = _histogram_column_number(counts, index)
        if not math.isfinite(coordinate) or not math.isfinite(count) or count < 0:
            raise ValueError("ParaView histogram contains invalid values")
        if rows and coordinate <= rows[-1][0]:
            raise ValueError("ParaView histogram coordinates are not increasing")
        rows.append((coordinate, count))
    return rows


def _histogram_column_number(column: Any, index: int) -> float:
    """Read one numeric VTK table cell without requiring NumPy."""

    tuple_getter = getattr(column, "GetTuple1", None)
    if callable(tuple_getter):
        return float(cast(Any, tuple_getter)(index))
    value_getter = getattr(column, "GetValue", None)
    if not callable(value_getter):
        raise ValueError("ParaView histogram column is unreadable")
    value = value_getter(index)
    converter = getattr(value, "ToDouble", None)
    rendered = cast(Any, converter)() if callable(converter) else cast(Any, value)
    return float(rendered)


def _histogram_evidence(
    rows: Sequence[Tuple[float, float]],
    *,
    observed_range: Sequence[float],
    tuple_count: Optional[int],
    method: str,
) -> Dict[str, Any]:
    """Summarize a bounded histogram without claiming exact raw quantiles."""

    counts = [count for _, count in rows]
    total = sum(counts)
    if not math.isfinite(total) or total <= 0:
        raise ValueError("ParaView histogram contains no finite observations")
    edges = _histogram_edges(
        [coordinate for coordinate, _ in rows],
        observed_range=observed_range,
    )
    rendered_counts: List[Any] = [
        int(value) if value.is_integer() else value for value in counts
    ]
    finite_count: Any = int(total) if total.is_integer() else total
    nonfinite_count: Optional[int] = None
    if isinstance(finite_count, int) and tuple_count is not None:
        if finite_count <= tuple_count:
            nonfinite_count = tuple_count - finite_count
    percentiles = [
        {
            "percentile": percentile,
            "value": _histogram_percentile(edges, counts, percentile),
        }
        for percentile in DISTRIBUTION_PERCENTILES
    ]
    return {
        "status": "available",
        "method": method,
        "bin_count": len(counts),
        "finite_count": finite_count,
        "nonfinite_count": nonfinite_count,
        "estimator": "uniform-within-bin",
        "histogram": {"bin_edges": edges, "counts": rendered_counts},
        "percentiles": percentiles,
        "log_scale_eligible": observed_range[0] > 0
        and observed_range[0] < observed_range[1],
    }


def _histogram_edges(
    coordinates: Sequence[float],
    *,
    observed_range: Sequence[float],
) -> List[float]:
    """Derive bounded bin edges from ParaView's ordered bin coordinates."""

    lower = float(observed_range[0])
    upper = float(observed_range[1])
    if not math.isfinite(lower) or not math.isfinite(upper) or lower > upper:
        raise ValueError("observed range is invalid")
    if len(coordinates) == 1:
        return [lower, upper]
    edges = [lower]
    for left, right in zip(coordinates, coordinates[1:]):
        midpoint = (left + right) / 2.0
        if not math.isfinite(midpoint):
            raise ValueError("ParaView histogram bin edge is invalid")
        edges.append(max(edges[-1], min(upper, midpoint)))
    edges.append(upper)
    if any(left > right for left, right in zip(edges, edges[1:])):
        raise ValueError("ParaView histogram edges are invalid")
    return edges


def _histogram_percentile(
    edges: Sequence[float],
    counts: Sequence[float],
    percentile: float,
) -> float:
    """Estimate one percentile from bounded bins using uniform interpolation."""

    if len(edges) != len(counts) + 1 or not counts:
        raise ValueError("histogram percentile inputs are invalid")
    if percentile <= 0:
        return float(edges[0])
    if percentile >= 100:
        return float(edges[-1])
    total = sum(counts)
    if total <= 0:
        raise ValueError("histogram percentile requires observations")
    target = total * percentile / 100.0
    cumulative = 0.0
    for index, count in enumerate(counts):
        next_cumulative = cumulative + count
        if count > 0 and target <= next_cumulative:
            fraction = max(0.0, min(1.0, (target - cumulative) / count))
            return float(edges[index] + fraction * (edges[index + 1] - edges[index]))
        cumulative = next_cumulative
    return float(edges[-1])


def _transfer_scale(value: object) -> Dict[str, str]:
    """Validate the explicit native transfer-function scale."""

    if value is None:
        return {"mode": "linear"}
    if not isinstance(value, dict) or set(value) != {"mode"}:
        raise CommandError(
            "invalid_arguments", "scale must contain exactly one mode field"
        )
    mode = value.get("mode")
    if mode == "symlog":
        raise CommandError(
            "unsupported_scale",
            "this ParaView runtime exposes native linear and log transfer scales; "
            "symlog is not approximated",
        )
    if mode not in SUPPORTED_TRANSFER_SCALES:
        raise CommandError("invalid_arguments", "scale mode must be linear or log")
    return {"mode": cast(str, mode)}


def _validate_scale_range(
    scale: Mapping[str, str],
    transfer_range: Sequence[float],
) -> None:
    """Reject scale/range combinations the native ParaView mapper cannot represent."""

    if len(transfer_range) != 2 or not all(
        math.isfinite(float(value)) for value in transfer_range
    ):
        raise CommandError("invalid_arguments", "transfer range must be finite")
    lower, upper = float(transfer_range[0]), float(transfer_range[1])
    if lower > upper:
        raise CommandError("invalid_arguments", "transfer range must be ordered")
    if scale["mode"] == "log" and not 0 < lower < upper:
        raise CommandError(
            "log_range_invalid",
            "log transfer scale requires an increasing strictly positive range",
        )


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
    view_up_length = math.sqrt(sum(component * component for component in view_up))
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
    if cross_length <= 1e-12 * direction_length * view_up_length:
        raise CommandError(
            "invalid_arguments",
            "camera view_up cannot be parallel to the viewing direction",
        )
    scale = _finite_number(value.get("parallel_scale"), "parallel_scale")
    if scale <= 0:
        raise CommandError("invalid_arguments", "parallel_scale must be positive")
    if value.get("projection") not in {"perspective", "parallel"}:
        raise CommandError(
            "invalid_arguments", "projection must be perspective or parallel"
        )
    view_angle = _finite_number(value.get("view_angle"), "view_angle")
    if not 0 < view_angle < 180:
        raise CommandError(
            "invalid_arguments", "view_angle must be between 0 and 180 degrees"
        )


def _apply_camera_state(view: Any, value: Mapping[str, Any]) -> None:
    """Apply one already validated complete camera state."""
    view.CameraPosition = list(value["position"])
    view.CameraFocalPoint = list(value["focal_point"])
    view.CameraViewUp = list(value["view_up"])
    view.CameraParallelScale = value["parallel_scale"]
    view.CameraParallelProjection = 1 if value["projection"] == "parallel" else 0
    view.CameraViewAngle = value["view_angle"]


def _stage_png(
    *,
    simple: Any,
    view: Any,
    output_dir: Path,
    width: int,
    height: int,
) -> Tuple[Path, bytes]:
    """Render and validate a private PNG without publishing its final name."""
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        output_dir.chmod(0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=output_dir,
        prefix=".paraview-artifact.",
        suffix=".tmp.png",
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
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
        writable = os.open(temporary, os.O_RDWR)
        try:
            os.fsync(writable)
        finally:
            os.close(writable)
        if os.name != "nt":
            temporary.chmod(0o600)
        succeeded = True
        return temporary, payload
    finally:
        if not succeeded:
            temporary.unlink(missing_ok=True)


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


def _surface_selection_ids_for_representation(
    *,
    selected_representations: Any,
    selection_sources: Any,
    target_representation: Any,
    target_source: Any,
    servermanager: Any,
    limit: int,
) -> Tuple[List[Dict[str, int]], Optional[int], Optional[str]]:
    """Read IDs only from the explicitly targeted ParaView actor and source."""
    representation_count = _collection_size(selected_representations)
    selection_count = _collection_size(selection_sources)
    if representation_count != selection_count:
        return [], None, "paraview_selection_collection_mismatch"
    if selection_count == 0:
        return [], 0, None
    target_sm_representation = getattr(
        target_representation,
        "SMProxy",
        target_representation,
    )
    target_sm_source = getattr(target_source, "SMProxy", target_source)
    returned: List[Dict[str, int]] = []
    total = 0
    matched_target = False
    for index in range(selection_count):
        representation = selected_representations.GetItemAsObject(index)
        if not _same_proxy(representation, target_sm_representation):
            continue
        if not _representation_uses_source(representation, target_sm_source):
            return [], None, "paraview_selection_representation_source_mismatch"
        matched_target = True
        raw_source = selection_sources.GetItemAsObject(index)
        xml_name = _proxy_xml_name(raw_source)
        try:
            selection_proxy = servermanager._getPyProxy(raw_source)
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return [], None, "paraview_selection_proxy_unavailable"
        values = _integer_property_values(getattr(selection_proxy, "IDs", None))
        if values is None or any(value < 0 for value in values):
            return [], None, "paraview_selection_ids_unavailable"
        if xml_name == "IDSelectionSource":
            width = 2
        elif xml_name in {
            "CompositeDataIDSelectionSource",
            "HierarchicalDataIDSelectionSource",
        }:
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
    if not matched_target:
        return [], 0, None
    return returned, total, None


def _same_proxy(left: Any, right: Any) -> bool:
    left_proxy = getattr(left, "SMProxy", left)
    right_proxy = getattr(right, "SMProxy", right)
    if left_proxy is right_proxy:
        return True
    try:
        return left_proxy.GetGlobalID() == right_proxy.GetGlobalID()
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return False


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
