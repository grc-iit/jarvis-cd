"""Declarative import and export contract for reusable ParaView service scenes."""

from __future__ import annotations

import copy
import json
import math
import os
import re
import stat
from pathlib import Path
from typing import Any, Mapping, Sequence, cast

SCENE_MANIFEST_SCHEMA = "jarvis.paraview.scene-manifest.v1"
SERVICE_STATE_SCHEMA = "jarvis.paraview.service-state.v2"
SCENE_ARTIFACT_MEDIA_TYPE = "application/vnd.jarvis.paraview.scene+json"
MAX_SCENE_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_SCENE_NODES = 32
MAX_SCENE_ACTORS = 32
MAX_SCENE_FIELDS = 256
MAX_CONTOUR_VALUES = 64
_SCENE_ID = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_ARTIFACT_ID = re.compile(r"^art_[A-Za-z0-9_-]{22,86}$")
_VERSION_TEXT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+:-]{0,127}$")


class SceneManifestError(ValueError):
    """A reusable scene was rejected with stable machine-readable evidence."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})

    def as_dict(self) -> dict[str, Any]:
        """Return the bounded rejection envelope emitted before service readiness."""
        return {
            "schema_version": "jarvis.paraview.scene-rejection.v1",
            "code": self.code,
            "message": str(self),
            "details": copy.deepcopy(self.details),
        }


def load_scene_manifest(path: Path) -> dict[str, Any]:
    """Load one bounded private regular JSON scene manifest."""
    if not path.is_absolute():
        raise SceneManifestError(
            "unsafe_scene_path",
            "initial_scene must resolve to an absolute materialized file",
        )
    descriptor: int | None = None
    try:
        link_metadata = path.lstat()
        if stat.S_ISLNK(link_metadata.st_mode):
            raise SceneManifestError(
                "unsafe_scene_path",
                "initial_scene must be a nonempty bounded regular file",
                details={"maximum_bytes": MAX_SCENE_MANIFEST_BYTES},
            )
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        metadata = os.fstat(descriptor)
    except SceneManifestError:
        raise
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise SceneManifestError(
            "unsafe_scene_path",
            "initial_scene is missing or unreadable",
        ) from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size < 2
        or metadata.st_size > MAX_SCENE_MANIFEST_BYTES
    ):
        if descriptor is not None:
            os.close(descriptor)
        raise SceneManifestError(
            "unsafe_scene_path",
            "initial_scene must be a nonempty bounded regular file",
            details={"maximum_bytes": MAX_SCENE_MANIFEST_BYTES},
        )
    try:
        chunks: list[bytes] = []
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
    except OSError as exc:
        raise SceneManifestError(
            "unsafe_scene_path",
            "initial_scene could not be read",
        ) from exc
    finally:
        assert descriptor is not None
        os.close(descriptor)
    if len(payload) != metadata.st_size:
        raise SceneManifestError(
            "unsafe_scene_path",
            "initial_scene changed while it was being read",
        )
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise SceneManifestError(
            "invalid_manifest",
            "initial_scene is not canonicalizable JSON",
        ) from exc
    if not isinstance(value, dict):
        raise SceneManifestError(
            "invalid_manifest",
            "initial_scene must contain one JSON object",
        )
    canonical = canonical_scene_bytes(value)
    if len(canonical) > MAX_SCENE_MANIFEST_BYTES:
        raise SceneManifestError(
            "resource_limit",
            "canonical initial_scene exceeds the service size limit",
            details={"maximum_bytes": MAX_SCENE_MANIFEST_BYTES},
        )
    return cast(dict[str, Any], value)


def validate_scene_manifest(
    value: Mapping[str, Any],
    *,
    descriptor: Mapping[str, Any],
    discovery: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate a complete manifest against one opened dataset before mutation."""
    _require_fields(
        value,
        {"schema_version", "dataset_binding", "compatibility", "source", "scene"},
        code="invalid_manifest",
        label="scene manifest",
    )
    if value.get("schema_version") != SCENE_MANIFEST_SCHEMA:
        raise SceneManifestError(
            "unsupported_scene_schema",
            "initial_scene uses an unsupported schema version",
            details={
                "expected": SCENE_MANIFEST_SCHEMA,
                "actual": value.get("schema_version"),
            },
        )
    _validate_source(value.get("source"))
    binding = _validate_dataset_binding(
        value.get("dataset_binding"),
        descriptor=descriptor,
        discovery=discovery,
    )
    compatibility = _validate_compatibility(value.get("compatibility"))
    scene = _mapping(value.get("scene"), "invalid_manifest", "scene must be an object")
    _require_fields(
        scene,
        {"timestep_index", "filters", "actors", "camera"},
        code="invalid_manifest",
        label="scene",
    )
    filters = scene.get("filters")
    actors = scene.get("actors")
    if not isinstance(filters, list) or not isinstance(actors, list):
        raise SceneManifestError(
            "invalid_manifest",
            "scene filters and actors must be lists",
        )
    requested_nodes = compatibility["resource_requirements"]["nodes"]
    requested_actors = compatibility["resource_requirements"]["actors"]
    if (
        len(filters) + 1 != requested_nodes
        or len(actors) != requested_actors
        or requested_nodes > MAX_SCENE_NODES
        or requested_actors > MAX_SCENE_ACTORS
    ):
        raise SceneManifestError(
            "resource_limit",
            "scene resource requirements disagree with bounded content",
            details={
                "maximum_nodes": MAX_SCENE_NODES,
                "maximum_actors": MAX_SCENE_ACTORS,
            },
        )
    timestep_index = _integer(
        scene.get("timestep_index"),
        "timestep_index",
        minimum=0,
    )
    timestep_count = cast(int, binding["timestep_count"])
    if (timestep_count == 0 and timestep_index != 0) or (
        timestep_count > 0 and timestep_index >= timestep_count
    ):
        raise SceneManifestError(
            "timestep_mismatch",
            "scene timestep does not exist in the opened dataset",
            details={
                "timestep_index": timestep_index,
                "timestep_count": timestep_count,
            },
        )
    nodes = _validate_filters(filters, discovery=discovery)
    _validate_actors(actors, nodes=nodes)
    _validate_camera(scene.get("camera"))
    canonical = canonical_scene_bytes(value)
    if len(canonical) > MAX_SCENE_MANIFEST_BYTES:
        raise SceneManifestError(
            "resource_limit",
            "canonical initial_scene exceeds the service size limit",
            details={"maximum_bytes": MAX_SCENE_MANIFEST_BYTES},
        )
    return json.loads(canonical.decode("utf-8"))


def build_scene_manifest(
    *,
    descriptor: Mapping[str, Any],
    pipeline: Mapping[str, Any],
    final_revision: int,
    artifact_id: str,
    jarvis_version: str,
    paraview_version: str,
    fingerprint_constraint: str,
) -> dict[str, Any]:
    """Project authoritative service state into a path-free canonical manifest."""
    if fingerprint_constraint not in {"exact", "compatible"}:
        raise ValueError("fingerprint_constraint must be exact or compatible")
    if not _ARTIFACT_ID.fullmatch(artifact_id):
        raise ValueError("scene artifact_id is invalid")
    if isinstance(final_revision, bool) or not isinstance(final_revision, int):
        raise ValueError("final_revision must be an integer")
    if final_revision < 1:
        raise ValueError("final_revision must be positive")
    nodes_value = pipeline.get("nodes")
    actors_value = pipeline.get("representations")
    timestep_value = pipeline.get("timestep")
    if (
        not isinstance(nodes_value, list)
        or not isinstance(actors_value, list)
        or not isinstance(timestep_value, dict)
        or not nodes_value
        or not actors_value
    ):
        raise ValueError("authoritative ParaView scene is incomplete")
    nodes = [cast(Mapping[str, Any], item) for item in nodes_value]
    actors = [cast(Mapping[str, Any], item) for item in actors_value]
    root = nodes[0]
    aliases: dict[str, str] = {cast(str, root["node_id"]): "root"}
    filters: list[dict[str, Any]] = []
    for index, node in enumerate(nodes[1:], start=1):
        node_id = cast(str, node["node_id"])
        alias = f"filter-{index:02d}"
        aliases[node_id] = alias
        input_ids = cast(Sequence[str], node["input_node_ids"])
        filter_record = cast(Mapping[str, Any], node["filter"])
        filters.append(
            {
                "filter_id": alias,
                "input": aliases[input_ids[0]],
                "type": filter_record["type"],
                "parameters": copy.deepcopy(filter_record["parameters"]),
            }
        )
    rendered_actors: list[dict[str, Any]] = []
    required_fields: dict[tuple[str, str], dict[str, Any]] = {}
    node_by_id = {cast(str, item["node_id"]): item for item in nodes}
    for index, actor in enumerate(actors):
        actor_id = "actor-root" if index == 0 else f"actor-{index:02d}"
        node_id = cast(str, actor["node_id"])
        color, provenance = _portable_color(actor["color"])
        field = color.get("field")
        if isinstance(field, dict):
            authoritative_node = node_by_id[node_id]
            required = _find_field(
                cast(Mapping[str, Any], authoritative_node["output"]),
                cast(str, field["name"]),
                cast(str, field["association"]),
            )
            required_fields[(required["association"], required["name"])] = {
                "name": required["name"],
                "association": required["association"],
                "components": required["components"],
                "units": required.get("units"),
            }
        rendered_actors.append(
            {
                "actor_id": actor_id,
                "node": aliases[node_id],
                "type": actor["type"],
                "visible": actor["visible"],
                "opacity": actor["opacity"],
                "point_size_px": actor["point_size_px"],
                "color": color,
                "range_provenance": provenance,
            }
        )
    for node in nodes[1:]:
        record = cast(Mapping[str, Any], node["filter"])
        parameters = cast(Mapping[str, Any], record["parameters"])
        if record["type"] not in {"threshold", "contour"}:
            continue
        parent = node_by_id[cast(Sequence[str], node["input_node_ids"])[0]]
        required = _find_field(
            cast(Mapping[str, Any], parent["output"]),
            cast(str, parameters["name"]),
            cast(str, parameters["association"]),
        )
        required_fields[(required["association"], required["name"])] = {
            "name": required["name"],
            "association": required["association"],
            "components": required["components"],
            "units": required.get("units"),
        }
    descriptor_fingerprint = copy.deepcopy(descriptor["fingerprint"])
    manifest = {
        "schema_version": SCENE_MANIFEST_SCHEMA,
        "dataset_binding": {
            "descriptor_schema": descriptor["schema_version"],
            "source_fingerprint": descriptor_fingerprint,
            "fingerprint_constraint": fingerprint_constraint,
            "kind": descriptor["kind"],
            "format": descriptor["format"],
            "topology": root["output"]["topology"],
            "bounds": copy.deepcopy(root["output"]["bounds"]),
            "timestep_count": timestep_value["count"],
            "required_fields": sorted(
                required_fields.values(),
                key=lambda item: (item["association"], item["name"]),
            ),
        },
        "compatibility": {
            "service_state_schema": SERVICE_STATE_SCHEMA,
            "required_plugins": [],
            "resource_requirements": {
                "nodes": len(nodes),
                "actors": len(actors),
            },
        },
        "source": {
            "package": "builtin.paraview",
            "jarvis_cd_version": _version(jarvis_version, "jarvis_cd_version"),
            "paraview_version": _version(paraview_version, "paraview_version"),
            "final_revision": final_revision,
            "artifact_id": artifact_id,
        },
        "scene": {
            "timestep_index": timestep_value["index"],
            "filters": filters,
            "actors": rendered_actors,
            "camera": copy.deepcopy(pipeline["camera"]),
        },
    }
    canonical = canonical_scene_bytes(manifest)
    if len(canonical) > MAX_SCENE_MANIFEST_BYTES:
        raise ValueError("canonical scene manifest exceeds the service size limit")
    return json.loads(canonical.decode("utf-8"))


def canonical_scene_bytes(value: Mapping[str, Any]) -> bytes:
    """Encode one manifest deterministically without platform-specific details."""
    try:
        payload = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SceneManifestError(
            "invalid_manifest",
            "scene manifest contains a non-JSON value",
        ) from exc
    return payload + b"\n"


def _validate_source(value: object) -> None:
    source = _mapping(value, "invalid_manifest", "source must be an object")
    _require_fields(
        source,
        {
            "package",
            "jarvis_cd_version",
            "paraview_version",
            "final_revision",
            "artifact_id",
        },
        code="invalid_manifest",
        label="source",
    )
    if source.get("package") != "builtin.paraview":
        raise SceneManifestError(
            "incompatible_runtime",
            "scene source package is not builtin.paraview",
        )
    _version(source.get("jarvis_cd_version"), "jarvis_cd_version")
    _version(source.get("paraview_version"), "paraview_version")
    _integer(source.get("final_revision"), "final_revision", minimum=1)
    artifact_id = source.get("artifact_id")
    if not isinstance(artifact_id, str) or not _ARTIFACT_ID.fullmatch(artifact_id):
        raise SceneManifestError(
            "invalid_manifest",
            "source artifact_id is invalid",
        )


def _validate_dataset_binding(
    value: object,
    *,
    descriptor: Mapping[str, Any],
    discovery: Mapping[str, Any],
) -> dict[str, Any]:
    binding = _mapping(
        value,
        "invalid_manifest",
        "dataset_binding must be an object",
    )
    _require_fields(
        binding,
        {
            "descriptor_schema",
            "source_fingerprint",
            "fingerprint_constraint",
            "kind",
            "format",
            "topology",
            "bounds",
            "timestep_count",
            "required_fields",
        },
        code="invalid_manifest",
        label="dataset_binding",
    )
    if binding.get("descriptor_schema") != "jarvis.dataset-descriptor.v1":
        raise SceneManifestError(
            "incompatible_descriptor",
            "scene requires an unsupported dataset descriptor schema",
        )
    fingerprint = _mapping(
        binding.get("source_fingerprint"),
        "invalid_manifest",
        "source_fingerprint must be an object",
    )
    if (
        set(fingerprint) != {"algorithm", "digest"}
        or fingerprint.get("algorithm") != "sha256"
        or not isinstance(fingerprint.get("digest"), str)
        or re.fullmatch(r"[0-9a-f]{64}", cast(str, fingerprint.get("digest"))) is None
    ):
        raise SceneManifestError(
            "invalid_manifest",
            "source_fingerprint is invalid",
        )
    constraint = binding.get("fingerprint_constraint")
    if constraint not in {"exact", "compatible"}:
        raise SceneManifestError(
            "invalid_manifest",
            "fingerprint_constraint must be exact or compatible",
        )
    current_fingerprint = descriptor.get("fingerprint")
    if constraint == "exact" and fingerprint != current_fingerprint:
        raise SceneManifestError(
            "descriptor_fingerprint_mismatch",
            "scene requires the exact source dataset descriptor",
            details={
                "required": fingerprint,
                "actual": current_fingerprint,
            },
        )
    for field in ("kind", "format"):
        if binding.get(field) != descriptor.get(field):
            raise SceneManifestError(
                "incompatible_descriptor",
                f"scene dataset {field} does not match the opened descriptor",
                details={
                    "field": field,
                    "required": binding.get(field),
                    "actual": descriptor.get(field),
                },
            )
    topology = discovery.get("topology")
    if binding.get("topology") != topology:
        raise SceneManifestError(
            "topology_mismatch",
            "scene topology does not match the opened dataset",
            details={"required": binding.get("topology"), "actual": topology},
        )
    bounds = binding.get("bounds")
    if bounds is not None and not _finite_bounds(bounds):
        raise SceneManifestError("invalid_manifest", "scene bounds are invalid")
    if bounds != discovery.get("bounds"):
        raise SceneManifestError(
            "bounds_mismatch",
            "scene bounds do not match the opened dataset",
            details={"required": bounds, "actual": discovery.get("bounds")},
        )
    timestep_count = _integer(
        binding.get("timestep_count"),
        "timestep_count",
        minimum=0,
    )
    if timestep_count != discovery.get("timestep_count"):
        raise SceneManifestError(
            "timestep_mismatch",
            "scene timestep count does not match the opened dataset",
            details={
                "required": timestep_count,
                "actual": discovery.get("timestep_count"),
            },
        )
    fields = binding.get("required_fields")
    if not isinstance(fields, list) or len(fields) > MAX_SCENE_FIELDS:
        raise SceneManifestError(
            "resource_limit",
            "scene required_fields exceeds the bounded limit",
            details={"maximum_fields": MAX_SCENE_FIELDS},
        )
    discovered_fields = discovery.get("arrays")
    if not isinstance(discovered_fields, list):
        raise SceneManifestError(
            "incompatible_descriptor",
            "opened dataset discovery has no field list",
        )
    identities: set[tuple[str, str]] = set()
    for field in fields:
        parsed = _field_identity(field)
        identity = (parsed["association"], parsed["name"])
        if identity in identities:
            raise SceneManifestError(
                "invalid_manifest",
                "scene required_fields contains a duplicate",
            )
        identities.add(identity)
        matches = [
            candidate
            for candidate in discovered_fields
            if isinstance(candidate, dict)
            and candidate.get("name") == parsed["name"]
            and candidate.get("association") == parsed["association"]
        ]
        if (
            len(matches) != 1
            or matches[0].get("components") != parsed["components"]
            or matches[0].get("units") != parsed["units"]
        ):
            raise SceneManifestError(
                "stale_field",
                "scene requires a field unavailable in the opened dataset",
                details={"field": parsed},
            )
    return cast(dict[str, Any], binding)


def _validate_compatibility(value: object) -> dict[str, Any]:
    compatibility = _mapping(
        value,
        "invalid_manifest",
        "compatibility must be an object",
    )
    _require_fields(
        compatibility,
        {"service_state_schema", "required_plugins", "resource_requirements"},
        code="invalid_manifest",
        label="compatibility",
    )
    if compatibility.get("service_state_schema") != SERVICE_STATE_SCHEMA:
        raise SceneManifestError(
            "incompatible_runtime",
            "scene requires an unsupported service-state contract",
        )
    plugins = compatibility.get("required_plugins")
    if not isinstance(plugins, list) or any(
        not isinstance(item, str) or not item for item in plugins
    ):
        raise SceneManifestError(
            "invalid_manifest",
            "required_plugins must be a list of plugin names",
        )
    if plugins:
        raise SceneManifestError(
            "unsupported_plugin",
            "declarative scene import does not activate ParaView plugins",
            details={"required_plugins": plugins},
        )
    requirements = _mapping(
        compatibility.get("resource_requirements"),
        "invalid_manifest",
        "resource_requirements must be an object",
    )
    _require_fields(
        requirements,
        {"nodes", "actors"},
        code="invalid_manifest",
        label="resource_requirements",
    )
    _integer(requirements.get("nodes"), "nodes", minimum=1)
    _integer(requirements.get("actors"), "actors", minimum=1)
    return cast(dict[str, Any], compatibility)


def _validate_filters(
    values: Sequence[object],
    *,
    discovery: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    if len(values) + 1 > MAX_SCENE_NODES:
        raise SceneManifestError(
            "resource_limit",
            "scene contains too many filter nodes",
            details={"maximum_nodes": MAX_SCENE_NODES},
        )
    root_arrays = discovery.get("arrays")
    if not isinstance(root_arrays, list):
        raise SceneManifestError(
            "incompatible_descriptor",
            "opened dataset discovery has no field list",
        )
    nodes: dict[str, dict[str, Any]] = {
        "root": {
            "topology": discovery.get("topology"),
            "arrays": copy.deepcopy(root_arrays),
        }
    }
    for raw in values:
        value = _mapping(raw, "invalid_filter", "filter must be an object")
        _require_fields(
            value,
            {"filter_id", "input", "type", "parameters"},
            code="invalid_filter",
            label="filter",
        )
        filter_id = _scene_id(value.get("filter_id"), "filter_id", "invalid_filter")
        input_id = _scene_id(value.get("input"), "input", "invalid_filter")
        if filter_id == "root" or filter_id in nodes:
            raise SceneManifestError(
                "invalid_filter",
                "filter_id must be unique and cannot be root",
            )
        parent = nodes.get(input_id)
        if parent is None:
            raise SceneManifestError(
                "invalid_filter",
                "filter input must reference an earlier node",
                details={"input": input_id},
            )
        filter_type = value.get("type")
        parameters = _mapping(
            value.get("parameters"),
            "invalid_filter",
            "filter parameters must be an object",
        )
        _validate_filter_parameters(filter_type, parameters, parent)
        nodes[filter_id] = {
            "topology": (
                "surface" if filter_type in {"slice", "contour"} else parent["topology"]
            ),
            "arrays": copy.deepcopy(parent["arrays"]),
        }
    return nodes


def _validate_filter_parameters(
    filter_type: object,
    parameters: Mapping[str, Any],
    parent: Mapping[str, Any],
) -> None:
    if filter_type in {"slice", "clip"}:
        _require_fields(
            parameters,
            {"origin", "normal"},
            code="invalid_filter",
            label=cast(str, filter_type),
        )
        _vector(parameters.get("origin"), "origin", length=3, code="invalid_filter")
        normal = _vector(
            parameters.get("normal"),
            "normal",
            length=3,
            code="invalid_filter",
        )
        if math.sqrt(sum(item * item for item in normal)) <= 1e-12:
            raise SceneManifestError(
                "invalid_filter",
                "filter normal cannot be a zero vector",
            )
        return
    if filter_type == "threshold":
        _require_fields(
            parameters,
            {"name", "association", "lower", "upper"},
            code="invalid_filter",
            label="threshold",
        )
        _require_node_field(parameters, parent, scalar=True)
        lower = _number(parameters.get("lower"), "lower", "invalid_filter")
        upper = _number(parameters.get("upper"), "upper", "invalid_filter")
        if lower > upper:
            raise SceneManifestError(
                "invalid_filter",
                "threshold lower cannot exceed upper",
            )
        return
    if filter_type == "contour":
        _require_fields(
            parameters,
            {"name", "association", "isovalues"},
            code="invalid_filter",
            label="contour",
        )
        if parameters.get("association") != "point":
            raise SceneManifestError(
                "invalid_filter",
                "contour requires a point-centered scalar field",
            )
        _require_node_field(parameters, parent, scalar=True)
        values = parameters.get("isovalues")
        if not isinstance(values, list) or not 1 <= len(values) <= MAX_CONTOUR_VALUES:
            raise SceneManifestError(
                "invalid_filter",
                "contour isovalues must be a nonempty bounded list",
            )
        parsed = [_number(item, "isovalues", "invalid_filter") for item in values]
        if len(set(parsed)) != len(parsed):
            raise SceneManifestError(
                "invalid_filter",
                "contour isovalues must be unique",
            )
        return
    raise SceneManifestError(
        "invalid_filter",
        "filter type must be slice, clip, threshold, or contour",
    )


def _validate_actors(
    values: Sequence[object],
    *,
    nodes: Mapping[str, Mapping[str, Any]],
) -> None:
    if not 1 <= len(values) <= MAX_SCENE_ACTORS:
        raise SceneManifestError(
            "resource_limit",
            "scene must contain a bounded nonempty actor list",
            details={"maximum_actors": MAX_SCENE_ACTORS},
        )
    identities: set[str] = set()
    root_actors = 0
    for raw in values:
        actor = _mapping(
            raw,
            "invalid_representation",
            "actor must be an object",
        )
        _require_fields(
            actor,
            {
                "actor_id",
                "node",
                "type",
                "visible",
                "opacity",
                "point_size_px",
                "color",
                "range_provenance",
            },
            code="invalid_representation",
            label="actor",
        )
        actor_id = _scene_id(
            actor.get("actor_id"),
            "actor_id",
            "invalid_representation",
        )
        if actor_id in identities:
            raise SceneManifestError(
                "invalid_representation",
                "actor_id must be unique",
            )
        identities.add(actor_id)
        node_id = _scene_id(actor.get("node"), "node", "invalid_representation")
        node = nodes.get(node_id)
        if node is None:
            raise SceneManifestError(
                "invalid_representation",
                "actor references an unknown scene node",
                details={"node": node_id},
            )
        if node_id == "root":
            root_actors += 1
        representation_type = actor.get("type")
        if representation_type not in {"surface", "points"}:
            raise SceneManifestError(
                "invalid_representation",
                "actor type must be surface or points",
            )
        if not isinstance(actor.get("visible"), bool):
            raise SceneManifestError(
                "invalid_representation",
                "actor visible must be boolean",
            )
        opacity = _number(
            actor.get("opacity"),
            "opacity",
            "invalid_representation",
        )
        if not 0 <= opacity <= 1:
            raise SceneManifestError(
                "invalid_representation",
                "actor opacity must be between zero and one",
            )
        point_size = actor.get("point_size_px")
        if representation_type == "surface" and point_size is not None:
            raise SceneManifestError(
                "invalid_representation",
                "surface actor point_size_px must be null",
            )
        if representation_type == "points":
            parsed_size = _number(
                point_size,
                "point_size_px",
                "invalid_representation",
            )
            if not 1 <= parsed_size <= 64:
                raise SceneManifestError(
                    "invalid_representation",
                    "point actor size must be between 1 and 64 pixels",
                )
        _validate_actor_color(
            actor.get("color"),
            actor.get("range_provenance"),
            node=node,
            visible=cast(bool, actor.get("visible")),
        )
    if root_actors < 1 or values[0].get("node") != "root":  # type: ignore[union-attr]
        raise SceneManifestError(
            "invalid_representation",
            "the first actor must represent the root dataset node",
        )


def _validate_actor_color(
    value: object,
    provenance_value: object,
    *,
    node: Mapping[str, Any],
    visible: bool,
) -> None:
    color = _mapping(
        value,
        "invalid_representation",
        "actor color must be an object",
    )
    if color.get("mode") == "solid":
        _require_fields(
            color,
            {"mode", "rgb"},
            code="invalid_representation",
            label="solid color",
        )
        rgb = _vector(
            color.get("rgb"),
            "rgb",
            length=3,
            code="invalid_representation",
        )
        if any(not 0 <= item <= 1 for item in rgb):
            raise SceneManifestError(
                "invalid_representation",
                "solid color components must be between zero and one",
            )
        if provenance_value is not None:
            raise SceneManifestError(
                "invalid_representation",
                "solid actors cannot carry range provenance",
            )
        return
    _require_fields(
        color,
        {
            "mode",
            "field",
            "preset",
            "invert",
            "scale",
            "range_policy",
            "scalar_bar_visible",
        },
        code="invalid_representation",
        label="field color",
    )
    if color.get("mode") != "field":
        raise SceneManifestError(
            "invalid_representation",
            "actor color mode must be solid or field",
        )
    selector = _mapping(
        color.get("field"),
        "invalid_representation",
        "field selector must be an object",
    )
    _require_fields(
        selector,
        {"name", "association"},
        code="invalid_representation",
        label="field selector",
    )
    _require_node_field(selector, node, scalar=False)
    preset = color.get("preset")
    if preset is not None:
        _text(preset, "preset", maximum=256, code="invalid_representation")
    if not isinstance(color.get("invert"), bool):
        raise SceneManifestError(
            "invalid_representation",
            "field color invert must be boolean",
        )
    scale = color.get("scale")
    if scale not in ({"mode": "linear"}, {"mode": "log"}):
        raise SceneManifestError(
            "invalid_representation",
            "field color scale must be linear or log",
        )
    scalar_bar_visible = color.get("scalar_bar_visible")
    if not isinstance(scalar_bar_visible, bool) or (not visible and scalar_bar_visible):
        raise SceneManifestError(
            "invalid_representation",
            "hidden actors cannot expose scalar bars",
        )
    policy = _mapping(
        color.get("range_policy"),
        "invalid_representation",
        "range_policy must be an object",
    )
    if (
        set(policy) != {"mode", "range", "timestep_behavior"}
        or policy.get("mode") != "fixed"
        or policy.get("timestep_behavior") != "freeze"
        or not _finite_pair(policy.get("range"), increasing=True)
    ):
        raise SceneManifestError(
            "invalid_representation",
            "imported field colors require a frozen fixed transfer range",
        )
    if scale == {"mode": "log"} and not (cast(Sequence[float], policy["range"])[0] > 0):
        raise SceneManifestError(
            "invalid_representation",
            "log color scale requires a positive transfer range",
        )
    provenance = _mapping(
        provenance_value,
        "invalid_representation",
        "field color range_provenance must be an object",
    )
    _require_fields(
        provenance,
        {"observed_range", "transfer_range", "source_policy"},
        code="invalid_representation",
        label="range_provenance",
    )
    source_policy = provenance.get("source_policy")
    if (
        not _finite_pair(provenance.get("observed_range"), increasing=False)
        or not _finite_pair(provenance.get("transfer_range"), increasing=False)
        or provenance.get("transfer_range") != policy.get("range")
        or not isinstance(source_policy, dict)
    ):
        raise SceneManifestError(
            "invalid_representation",
            "field color range provenance is inconsistent",
        )
    _validate_source_range_policy(source_policy)


def _validate_source_range_policy(value: Mapping[str, Any]) -> None:
    """Validate export-only range provenance without accepting path-like extensions."""
    mode = value.get("mode")
    if mode == "full":
        if value != {"mode": "full", "timestep_behavior": "recompute"}:
            raise SceneManifestError(
                "invalid_representation",
                "full source range policy is invalid",
            )
        return
    if mode == "fixed":
        if (
            set(value) != {"mode", "range", "timestep_behavior"}
            or value.get("timestep_behavior") != "freeze"
            or not _finite_pair(value.get("range"), increasing=True)
        ):
            raise SceneManifestError(
                "invalid_representation",
                "fixed source range policy is invalid",
            )
        return
    if mode == "measurement_percentile":
        if (
            set(value)
            != {
                "mode",
                "measurement_id",
                "lower_percentile",
                "upper_percentile",
                "timestep_behavior",
            }
            or not isinstance(value.get("measurement_id"), str)
            or re.fullmatch(
                r"mea_[A-Za-z0-9._:-]{1,252}",
                cast(str, value.get("measurement_id")),
            )
            is None
            or value.get("timestep_behavior") != "freeze"
        ):
            raise SceneManifestError(
                "invalid_representation",
                "measurement source range policy is invalid",
            )
        lower = _number(
            value.get("lower_percentile"),
            "lower_percentile",
            "invalid_representation",
        )
        upper = _number(
            value.get("upper_percentile"),
            "upper_percentile",
            "invalid_representation",
        )
        if not 0 <= lower < upper <= 100:
            raise SceneManifestError(
                "invalid_representation",
                "measurement source percentiles are invalid",
            )
        return
    raise SceneManifestError(
        "invalid_representation",
        "source range policy mode is invalid",
    )


def _validate_camera(value: object) -> None:
    camera = _mapping(
        value,
        "invalid_camera",
        "camera must be an object",
    )
    _require_fields(
        camera,
        {
            "position",
            "focal_point",
            "view_up",
            "parallel_scale",
            "projection",
            "view_angle",
        },
        code="invalid_camera",
        label="camera",
    )
    position = _vector(
        camera.get("position"),
        "position",
        length=3,
        code="invalid_camera",
    )
    focal = _vector(
        camera.get("focal_point"),
        "focal_point",
        length=3,
        code="invalid_camera",
    )
    view_up = _vector(
        camera.get("view_up"),
        "view_up",
        length=3,
        code="invalid_camera",
    )
    direction = [focal[index] - position[index] for index in range(3)]
    direction_length = math.sqrt(sum(item * item for item in direction))
    view_up_length = math.sqrt(sum(item * item for item in view_up))
    cross = [
        direction[1] * view_up[2] - direction[2] * view_up[1],
        direction[2] * view_up[0] - direction[0] * view_up[2],
        direction[0] * view_up[1] - direction[1] * view_up[0],
    ]
    if (
        direction_length <= 1e-12
        or view_up_length <= 1e-12
        or math.sqrt(sum(item * item for item in cross))
        <= 1e-12 * direction_length * view_up_length
    ):
        raise SceneManifestError(
            "invalid_camera",
            "camera orientation is degenerate",
        )
    if (
        _number(
            camera.get("parallel_scale"),
            "parallel_scale",
            "invalid_camera",
        )
        <= 0
    ):
        raise SceneManifestError(
            "invalid_camera",
            "camera parallel_scale must be positive",
        )
    if camera.get("projection") not in {"parallel", "perspective"}:
        raise SceneManifestError(
            "invalid_camera",
            "camera projection is invalid",
        )
    view_angle = _number(
        camera.get("view_angle"),
        "view_angle",
        "invalid_camera",
    )
    if not 0 < view_angle < 180:
        raise SceneManifestError(
            "invalid_camera",
            "camera view_angle must be between zero and 180 degrees",
        )


def _portable_color(value: object) -> tuple[dict[str, Any], dict[str, Any] | None]:
    color = cast(Mapping[str, Any], value)
    if color["mode"] == "solid":
        return copy.deepcopy(dict(color)), None
    transfer_range = copy.deepcopy(color["transfer_range"])
    request = {
        "mode": "field",
        "field": {
            "name": color["field"]["name"],
            "association": color["field"]["association"],
        },
        "preset": color["preset"],
        "invert": color["invert"],
        "scale": copy.deepcopy(color["scale"]),
        "range_policy": {
            "mode": "fixed",
            "range": transfer_range,
            "timestep_behavior": "freeze",
        },
        "scalar_bar_visible": color["scalar_bar"]["visible"],
    }
    provenance = {
        "observed_range": copy.deepcopy(color["observation"]["observed_range"]),
        "transfer_range": copy.deepcopy(transfer_range),
        "source_policy": copy.deepcopy(color["range_policy"]),
    }
    return request, provenance


def _require_node_field(
    selector: Mapping[str, Any],
    node: Mapping[str, Any],
    *,
    scalar: bool,
) -> None:
    name = _text(
        selector.get("name"),
        "field name",
        maximum=512,
        code="stale_field",
    )
    association = selector.get("association")
    if association not in {"point", "cell"}:
        raise SceneManifestError(
            "stale_field",
            "field association must be point or cell",
        )
    arrays = node.get("arrays")
    if not isinstance(arrays, list):
        raise SceneManifestError(
            "stale_field",
            "scene node has no discoverable field list",
        )
    matches = [
        field
        for field in arrays
        if isinstance(field, dict)
        and field.get("name") == name
        and field.get("association") == association
    ]
    if len(matches) != 1:
        raise SceneManifestError(
            "stale_field",
            "scene references a field unavailable on its input node",
            details={"name": name, "association": association},
        )
    if scalar and matches[0].get("components") != 1:
        raise SceneManifestError(
            "stale_field",
            "scene filter requires a scalar field",
        )


def _find_field(
    output: Mapping[str, Any],
    name: str,
    association: str,
) -> dict[str, Any]:
    arrays = output.get("arrays")
    if not isinstance(arrays, list):
        raise ValueError("authoritative node output has no array list")
    matches = [
        item
        for item in arrays
        if isinstance(item, dict)
        and item.get("name") == name
        and item.get("association") == association
    ]
    if len(matches) != 1:
        raise ValueError("authoritative scene field is unavailable")
    return cast(dict[str, Any], matches[0])


def _field_identity(value: object) -> dict[str, Any]:
    field = _mapping(value, "invalid_manifest", "field identity must be an object")
    _require_fields(
        field,
        {"name", "association", "components", "units"},
        code="invalid_manifest",
        label="field identity",
    )
    name = _text(
        field.get("name"),
        "field name",
        maximum=512,
        code="invalid_manifest",
    )
    association = field.get("association")
    if association not in {"point", "cell"}:
        raise SceneManifestError(
            "invalid_manifest",
            "field association must be point or cell",
        )
    components = _integer(field.get("components"), "components", minimum=1)
    if components > 256:
        raise SceneManifestError(
            "invalid_manifest",
            "field components exceeds the service limit",
        )
    units = field.get("units")
    if units is not None:
        _text(units, "units", maximum=256, code="invalid_manifest")
    return {
        "name": name,
        "association": association,
        "components": components,
        "units": units,
    }


def _mapping(value: object, code: str, message: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise SceneManifestError(code, message)
    return value


def _require_fields(
    value: Mapping[str, Any],
    expected: set[str],
    *,
    code: str,
    label: str,
) -> None:
    if set(value) != expected:
        raise SceneManifestError(
            code,
            f"{label} fields do not match the versioned contract",
            details={
                "missing": sorted(expected - set(value)),
                "unexpected": sorted(set(value) - expected),
            },
        )


def _scene_id(value: object, label: str, code: str) -> str:
    if not isinstance(value, str) or _SCENE_ID.fullmatch(value) is None:
        raise SceneManifestError(code, f"{label} is invalid")
    return value


def _text(
    value: object,
    label: str,
    *,
    maximum: int,
    code: str,
) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.encode("utf-8")) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise SceneManifestError(code, f"{label} must be bounded text")
    return value


def _version(value: object, label: str) -> str:
    if not isinstance(value, str) or _VERSION_TEXT.fullmatch(value) is None:
        raise SceneManifestError(
            "invalid_manifest",
            f"{label} must be a portable version identifier",
        )
    return value


def _integer(value: object, label: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise SceneManifestError(
            "invalid_manifest",
            f"{label} must be an integer at least {minimum}",
        )
    return value


def _number(value: object, label: str, code: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise SceneManifestError(code, f"{label} must be finite")
    return float(value)


def _vector(
    value: object,
    label: str,
    *,
    length: int,
    code: str,
) -> list[float]:
    if not isinstance(value, list) or len(value) != length:
        raise SceneManifestError(code, f"{label} must contain {length} values")
    return [_number(item, label, code) for item in value]


def _finite_pair(value: object, *, increasing: bool) -> bool:
    if not isinstance(value, list) or len(value) != 2:
        return False
    if any(
        isinstance(item, bool)
        or not isinstance(item, (int, float))
        or not math.isfinite(float(item))
        for item in value
    ):
        return False
    return value[0] < value[1] if increasing else value[0] <= value[1]


def _finite_bounds(value: object) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 6
        and all(
            not isinstance(item, bool)
            and isinstance(item, (int, float))
            and math.isfinite(float(item))
            for item in value
        )
        and all(value[index] <= value[index + 1] for index in (0, 2, 4))
    )


def _reject_duplicate_keys(
    pairs: Sequence[tuple[str, Any]],
) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON object key: {key}")
        value[key] = item
    return value


__all__ = [
    "MAX_SCENE_MANIFEST_BYTES",
    "SCENE_ARTIFACT_MEDIA_TYPE",
    "SCENE_MANIFEST_SCHEMA",
    "SceneManifestError",
    "build_scene_manifest",
    "canonical_scene_bytes",
    "load_scene_manifest",
    "validate_scene_manifest",
]
