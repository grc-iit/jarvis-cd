"""Reusable ParaView scene import, export, and compatibility tests."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping, cast

import pytest
from jarvis_cd.service_runtime import (
    DatasetArray,
    DatasetDescriptor,
    DatasetMember,
    calculate_dataset_fingerprint,
)

_BUILTIN_REPOSITORY_ROOT = Path(__file__).resolve().parents[3] / "builtin"
sys.path.insert(0, str(_BUILTIN_REPOSITORY_ROOT))

from builtin.paraview import pkg as package_module  # noqa: E402
from builtin.paraview import service as service_module  # noqa: E402
from builtin.paraview.scene_manifest import (  # noqa: E402
    SCENE_ARTIFACT_MEDIA_TYPE,
    SceneManifestError,
    build_scene_manifest,
    canonical_scene_bytes,
    load_scene_manifest,
    validate_scene_manifest,
)


def _descriptor() -> dict[str, Any]:
    members = (DatasetMember(index=0, location="/cluster/deep-water.vti"),)
    arrays = (
        DatasetArray(
            name="pressure",
            association="point",
            components=1,
            units="Pa",
        ),
    )
    fingerprint = calculate_dataset_fingerprint(
        dataset_id="deep-water-impact-2018",
        kind="temporal-volume",
        format="vtk-image-data",
        members=members,
        arrays=arrays,
        bounds=(0.0, 10.0, 0.0, 6.0, -2.0, 2.0),
    )
    return DatasetDescriptor(
        dataset_id="deep-water-impact-2018",
        kind="temporal-volume",
        format="vtk-image-data",
        members=members,
        arrays=arrays,
        bounds=(0.0, 10.0, 0.0, 6.0, -2.0, 2.0),
        fingerprint=fingerprint,
    ).to_dict()


def _field() -> dict[str, Any]:
    return {
        "name": "pressure",
        "association": "point",
        "components": 1,
        "units": "Pa",
    }


def _output(*, topology: str) -> dict[str, Any]:
    return {
        "topology": topology,
        "raw_data_type": "vtkImageData",
        "bounds": [0.0, 10.0, 0.0, 6.0, -2.0, 2.0],
        "point_count": 256,
        "cell_count": 128,
        "arrays": [_field()],
    }


def _pipeline() -> dict[str, Any]:
    return {
        "timestep": {"index": 1, "value": 0.5, "count": 3},
        "nodes": [
            {
                "node_id": "node_root",
                "kind": "reader",
                "input_node_ids": [],
                "filter": None,
                "output": _output(topology="volume"),
            },
            {
                "node_id": "node_threshold",
                "kind": "threshold",
                "input_node_ids": ["node_root"],
                "filter": {
                    "type": "threshold",
                    "parameters": {
                        "name": "pressure",
                        "association": "point",
                        "lower": 2.0,
                        "upper": 8.0,
                    },
                },
                "output": _output(topology="volume"),
            },
            {
                "node_id": "node_slice",
                "kind": "slice",
                "input_node_ids": ["node_threshold"],
                "filter": {
                    "type": "slice",
                    "parameters": {
                        "origin": [5.0, 3.0, 0.0],
                        "normal": [0.0, 1.0, 0.0],
                    },
                },
                "output": _output(topology="surface"),
            },
        ],
        "representations": [
            {
                "representation_id": "rep_root",
                "node_id": "node_root",
                "type": "surface",
                "visible": False,
                "opacity": 0.15,
                "point_size_px": None,
                "color": {"mode": "solid", "rgb": [0.1, 0.1, 0.15]},
            },
            {
                "representation_id": "rep_slice",
                "node_id": "node_slice",
                "type": "surface",
                "visible": True,
                "opacity": 0.9,
                "point_size_px": None,
                "color": {
                    "mode": "field",
                    "field": _field(),
                    "observation": {
                        "observed_range": [1.0, 9.0],
                        "tuple_count": 128,
                        "value_mode": "scalar",
                    },
                    "preset": "Viridis (matplotlib)",
                    "invert": False,
                    "scale": {"mode": "linear"},
                    "range_policy": {
                        "mode": "measurement_percentile",
                        "measurement_id": "mea_pressure",
                        "lower_percentile": 5.0,
                        "upper_percentile": 95.0,
                        "timestep_behavior": "freeze",
                    },
                    "transfer_range": [2.0, 8.0],
                    "scalar_bar": {
                        "visible": True,
                        "embedded_in_frame": True,
                    },
                    "supported_scales": ["linear", "log"],
                },
            },
        ],
        "measurements": [],
        "camera": {
            "position": [14.0, 8.0, 6.0],
            "focal_point": [5.0, 3.0, 0.0],
            "view_up": [0.0, 0.0, 1.0],
            "parallel_scale": 6.0,
            "projection": "perspective",
            "view_angle": 30.0,
        },
        "selection": None,
        "artifacts": [],
    }


def _manifest(*, constraint: str = "exact") -> dict[str, Any]:
    return build_scene_manifest(
        descriptor=_descriptor(),
        pipeline=_pipeline(),
        final_revision=17,
        artifact_id="art_0123456789abcdefghijkl",
        jarvis_version="1.7.0",
        paraview_version="5.13.3",
        fingerprint_constraint=constraint,
    )


def _discovery() -> dict[str, Any]:
    return {
        "topology": "volume",
        "bounds": [0.0, 10.0, 0.0, 6.0, -2.0, 2.0],
        "arrays": [_field()],
        "timestep_count": 3,
    }


def test_canonical_scene_round_trip_preserves_effective_graph_and_provenance() -> None:
    """Exported JSON validates unchanged and freezes measurement-derived ranges."""
    manifest = _manifest()

    validated = validate_scene_manifest(
        json.loads(canonical_scene_bytes(manifest)),
        descriptor=_descriptor(),
        discovery=_discovery(),
    )

    assert validated == manifest
    assert validated["scene"]["filters"] == manifest["scene"]["filters"]
    assert validated["scene"]["actors"] == manifest["scene"]["actors"]
    field_actor = validated["scene"]["actors"][1]
    assert field_actor["color"]["range_policy"] == {
        "mode": "fixed",
        "range": [2.0, 8.0],
        "timestep_behavior": "freeze",
    }
    assert field_actor["range_provenance"]["source_policy"]["mode"] == (
        "measurement_percentile"
    )
    assert field_actor["color"]["scalar_bar_visible"] is True
    assert validated["scene"]["camera"] == _pipeline()["camera"]


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (
            lambda value: value["dataset_binding"].update(topology="points"),
            "topology_mismatch",
        ),
        (
            lambda value: value["dataset_binding"].update(
                bounds=[0.0, 9.0, 0.0, 6.0, -2.0, 2.0]
            ),
            "bounds_mismatch",
        ),
        (
            lambda value: value["dataset_binding"]["required_fields"][0].update(
                name="stale_pressure"
            ),
            "stale_field",
        ),
        (
            lambda value: value["compatibility"].update(
                required_plugins=["site.CustomFilter"]
            ),
            "unsupported_plugin",
        ),
        (
            lambda value: value["compatibility"]["resource_requirements"].update(
                nodes=33
            ),
            "resource_limit",
        ),
        (
            lambda value: value["scene"]["camera"].update(
                focal_point=value["scene"]["camera"]["position"]
            ),
            "invalid_camera",
        ),
        (
            lambda value: value["scene"]["filters"][0]["parameters"].update(
                lower=9.0, upper=2.0
            ),
            "invalid_filter",
        ),
    ],
)
def test_scene_preflight_rejects_incompatible_or_unbounded_content(
    mutate: Any,
    code: str,
) -> None:
    manifest = _manifest()
    mutate(manifest)

    with pytest.raises(SceneManifestError) as captured:
        validate_scene_manifest(
            manifest,
            descriptor=_descriptor(),
            discovery=_discovery(),
        )

    assert captured.value.code == code
    assert captured.value.as_dict()["schema_version"] == (
        "jarvis.paraview.scene-rejection.v1"
    )


def test_exact_fingerprint_rejects_different_descriptor_but_compatible_can_open() -> (
    None
):
    descriptor = _descriptor()
    descriptor["dataset_id"] = "compatible-copy"
    intrinsic = {key: item for key, item in descriptor.items() if key != "fingerprint"}
    descriptor["fingerprint"] = {
        "algorithm": "sha256",
        "digest": hashlib.sha256(
            json.dumps(
                intrinsic,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest(),
    }
    with pytest.raises(SceneManifestError) as captured:
        validate_scene_manifest(
            _manifest(),
            descriptor=descriptor,
            discovery=_discovery(),
        )
    assert captured.value.code == "descriptor_fingerprint_mismatch"

    compatible = validate_scene_manifest(
        _manifest(constraint="compatible"),
        descriptor=descriptor,
        discovery=_discovery(),
    )
    assert compatible["dataset_binding"]["fingerprint_constraint"] == "compatible"


def test_scene_manifest_file_rejects_symlink_and_duplicate_keys(
    tmp_path: Path,
) -> None:
    source = tmp_path / "scene.json"
    source.write_bytes(canonical_scene_bytes(_manifest()))
    assert load_scene_manifest(source.resolve()) == _manifest()

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"schema_version":"one","schema_version":"two"}')
    with pytest.raises(SceneManifestError) as captured:
        load_scene_manifest(duplicate.resolve())
    assert captured.value.code == "invalid_manifest"

    link = tmp_path / "linked.json"
    try:
        link.symlink_to(source)
    except OSError:
        return
    with pytest.raises(SceneManifestError) as captured:
        load_scene_manifest(link.absolute())
    assert captured.value.code == "unsafe_scene_path"


class _ImportBackend:
    """Minimal transactional surface that records real importer semantics."""

    _import_scene_manifest = service_module.ParaViewBackend._import_scene_manifest

    def __init__(self) -> None:
        self.descriptor = _descriptor()
        self._nodes: dict[str, dict[str, Any]] = {
            "node_root": {
                "node_id": "node_root",
                "output": _output(topology="volume"),
            }
        }
        self._timesteps = [0.0, 0.5, 1.0]
        self._transaction_open = False
        self.calls: list[tuple[str, Mapping[str, Any]]] = []
        self.created_nodes: dict[str, str] = {}
        self.rollback_calls = 0

    def begin_command(self) -> object:
        self._transaction_open = True
        return {}

    def commit_command(self, _checkpoint: object) -> None:
        self._transaction_open = False

    def rollback_command(self, _checkpoint: object) -> None:
        self.rollback_calls += 1
        self._transaction_open = False

    def _set_timestep(
        self,
        arguments: Mapping[str, Any],
        _command_id: str,
    ) -> dict[str, Any]:
        self.calls.append(("set_timestep", copy.deepcopy(arguments)))
        return {"timestep": dict(arguments)}

    def _create_filter(
        self,
        arguments: Mapping[str, Any],
        _command_id: str,
    ) -> dict[str, Any]:
        self.calls.append(("create_filter", copy.deepcopy(arguments)))
        node_id = f"node_imported_{len(self.created_nodes) + 1}"
        self.created_nodes[node_id] = cast(str, arguments["input_node_id"])
        return {"node": {"node_id": node_id}}

    def _set_representation(
        self,
        arguments: Mapping[str, Any],
        _command_id: str,
    ) -> dict[str, Any]:
        self.calls.append(("set_representation", copy.deepcopy(arguments)))
        return {"representation": dict(arguments)}

    def _set_camera(
        self,
        arguments: Mapping[str, Any],
        _command_id: str,
    ) -> dict[str, Any]:
        self.calls.append(("set_camera", copy.deepcopy(arguments)))
        return {"camera": dict(arguments)}


def test_import_replays_preflighted_graph_atomically_and_keeps_revision_semantics() -> (
    None
):
    backend = _ImportBackend()

    evidence = backend._import_scene_manifest(_manifest())

    assert [name for name, _arguments in backend.calls] == [
        "set_timestep",
        "create_filter",
        "create_filter",
        "set_representation",
        "set_representation",
        "set_camera",
    ]
    first_filter = backend.calls[1][1]
    second_filter = backend.calls[2][1]
    assert first_filter["input_node_id"] == "node_root"
    assert second_filter["input_node_id"] == "node_imported_1"
    assert backend.calls[3][1]["representation_id"] == "rep_root"
    assert backend.calls[4][1]["representation_id"] is None
    assert backend._transaction_open is False
    assert backend.rollback_calls == 0
    assert evidence["source_final_revision"] == 17
    assert evidence["descriptor_fingerprint"] == _descriptor()["fingerprint"]


class _RevisionBackend:
    """Valid state backend proving export does not disable later edits."""

    execution_id = "exec-scene"
    package_name = "builtin.paraview"
    package_id = "viewer"
    service_instance_id = "srv-scene"

    def __init__(self) -> None:
        self.index = 0
        self.last_export_arguments: dict[str, Any] | None = None

    def dataset_state(self) -> dict[str, Any]:
        return {
            "descriptor": _descriptor(),
            "discovery": {
                "arrays": [_field()],
                "bounds": [0.0, 10.0, 0.0, 6.0, -2.0, 2.0],
                "timestep_values": [0.0, 0.5, 1.0],
            },
        }

    def pipeline_state(self) -> dict[str, Any]:
        pipeline = _pipeline()
        pipeline["nodes"] = [pipeline["nodes"][0]]
        pipeline["representations"] = [pipeline["representations"][0]]
        pipeline["timestep"] = {
            "index": self.index,
            "value": [0.0, 0.5, 1.0][self.index],
            "count": 3,
        }
        return pipeline

    def execute(
        self,
        operation: str,
        arguments: Mapping[str, Any],
        _command_id: str,
    ) -> dict[str, Any]:
        if operation == "export_scene":
            self.last_export_arguments = dict(arguments)
            return {"artifact": {"artifact_id": "art_scene"}}
        self.index = cast(int, arguments["index"])
        return {"timestep": self.index}

    def render_png(self) -> bytes:
        return b"\x89PNG\r\n\x1a\nscene"

    def begin_command(self) -> object:
        return self.index

    def commit_command(self, _checkpoint: object) -> None:
        return

    def rollback_command(self, checkpoint: object) -> None:
        self.index = cast(int, checkpoint)


def test_export_revision_continues_into_normal_temporal_edit() -> None:
    backend = _RevisionBackend()
    controller = service_module.ServiceStateController(
        backend=backend,
        execution_id=backend.execution_id,
        package_name=backend.package_name,
        package_id=backend.package_id,
        service_instance_id=backend.service_instance_id,
    )

    exported = controller.command(
        {
            "schema_version": "jarvis.paraview.command.v2",
            "command_id": "export-scene",
            "operation": "export_scene",
            "expected_revision": 1,
            "arguments": {
                "filename": "scenes/deep-water.json",
                "fingerprint_constraint": "exact",
            },
        }
    )
    edited = controller.command(
        {
            "schema_version": "jarvis.paraview.command.v2",
            "command_id": "advance-time",
            "operation": "set_timestep",
            "expected_revision": 2,
            "arguments": {"index": 2},
        }
    )

    assert backend.last_export_arguments == {
        "filename": "scenes/deep-water.json",
        "fingerprint_constraint": "exact",
        "_final_revision": 2,
    }
    assert exported["state"]["revision"] == 2
    assert edited["state"]["revision"] == 3
    assert edited["state"]["pipeline"]["timestep"]["index"] == 2


class _ArtifactBackend:
    """File-backed export surface using production artifact transactions."""

    _export_scene = service_module.ParaViewBackend._export_scene
    _scene_digest = service_module.ParaViewBackend._scene_digest
    _validate_staged_artifact_identities = (
        service_module.ParaViewBackend._validate_staged_artifact_identities
    )

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.output_dir.mkdir()
        self.descriptor = _descriptor()
        self.service_instance_id = "srv-scene-artifact"
        self.execution_id = "exec-scene-artifact"
        self.package_name = "builtin.paraview"
        self.package_id = "viewer"
        self._artifacts: list[dict[str, Any]] = []
        self._staged_artifacts: dict[str, dict[str, Any]] = {}
        self._representations = {
            "rep_root": {"visible": True},
            "rep_slice": {"visible": True},
        }

    def pipeline_state(self) -> dict[str, Any]:
        pipeline = _pipeline()
        pipeline["artifacts"] = copy.deepcopy(self._artifacts)
        return pipeline

    def _paraview_version(self) -> str:
        return "5.13.3"


class _ImportedArtifactBackend(_ArtifactBackend):
    """Commit imported-scene provenance through the production ledger helpers."""

    _publish_imported_scene = service_module.ParaViewBackend._publish_imported_scene

    def __init__(self, output_dir: Path) -> None:
        super().__init__(output_dir)
        self._transaction_open = False
        self._checkpoint_artifacts: list[dict[str, Any]] = []

    def begin_command(self) -> object:
        self._transaction_open = True
        self._checkpoint_artifacts = copy.deepcopy(self._artifacts)
        return {}

    def commit_command(self, _checkpoint: object) -> None:
        self._validate_staged_artifact_identities()
        service_module._commit_staged_artifacts(self._staged_artifacts)
        self._staged_artifacts = {}
        self._transaction_open = False

    def rollback_command(self, _checkpoint: object) -> None:
        for staged in self._staged_artifacts.values():
            staged_path = staged.get("staged_path")
            if isinstance(staged_path, Path):
                staged_path.unlink(missing_ok=True)
        self._staged_artifacts = {}
        self._artifacts = self._checkpoint_artifacts
        self._transaction_open = False


def _prepare_with_portable_cluster_location(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepare_artifact = service_module._prepare_artifact_event

    def prepare_with_cluster_location(**kwargs: Any) -> dict[str, Any]:
        kwargs["cluster_location"] = (
            "/cluster/jarvis/scenes/" + cast(Path, kwargs["path"]).name
        )
        return prepare_artifact(**kwargs)

    monkeypatch.setattr(
        service_module,
        "_prepare_artifact_event",
        prepare_with_cluster_location,
    )


def test_export_scene_publishes_queryable_artifact_without_transient_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_path = (tmp_path / "artifacts.jsonl").resolve()
    monkeypatch.setenv("JARVIS_ARTIFACT_PATH", str(artifact_path))
    monkeypatch.setenv("JARVIS_EXECUTION_ID", "exec-scene-artifact")
    monkeypatch.setenv("JARVIS_PACKAGE_NAME", "builtin.paraview")
    monkeypatch.setenv("JARVIS_PACKAGE_ID", "viewer")
    monkeypatch.setenv("JARVIS_SERVICE_AUTHORIZATION", "secret-token-must-not-leak")
    _prepare_with_portable_cluster_location(monkeypatch)
    backend = _ArtifactBackend(tmp_path / "output")

    result = backend._export_scene(
        {
            "filename": "scenes/deep-water.json",
            "fingerprint_constraint": "exact",
            "_final_revision": 22,
        },
        "export-scene-artifact",
    )
    backend._validate_staged_artifact_identities()
    service_module._commit_staged_artifacts(backend._staged_artifacts)

    scene_path = tmp_path / "output" / "scenes" / "deep-water.json"
    manifest = json.loads(scene_path.read_text(encoding="utf-8"))
    event = json.loads(artifact_path.read_text(encoding="utf-8"))
    rendered = scene_path.read_text(encoding="utf-8")
    assert result["artifact"]["kind"] == "visualization_scene"
    assert event["media_type"] == SCENE_ARTIFACT_MEDIA_TYPE
    assert event["metadata"]["final_revision"] == 22
    assert event["metadata"]["descriptor_fingerprint"] == (_descriptor()["fingerprint"])
    assert manifest["source"]["artifact_id"] == event["artifact_id"]
    assert manifest["source"]["final_revision"] == 22
    assert "/cluster/" not in rendered
    assert str(tmp_path) not in rendered
    assert "exec-scene-artifact" not in rendered
    assert "srv-scene-artifact" not in rendered
    assert "secret-token-must-not-leak" not in rendered
    assert "service_port" not in rendered


def test_imported_scene_is_queryable_provenance_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_path = (tmp_path / "artifacts.jsonl").resolve()
    monkeypatch.setenv("JARVIS_ARTIFACT_PATH", str(artifact_path))
    monkeypatch.setenv("JARVIS_EXECUTION_ID", "exec-scene-artifact")
    monkeypatch.setenv("JARVIS_PACKAGE_NAME", "builtin.paraview")
    monkeypatch.setenv("JARVIS_PACKAGE_ID", "viewer")
    _prepare_with_portable_cluster_location(monkeypatch)
    backend = _ImportedArtifactBackend(tmp_path / "output")
    source = _manifest()
    evidence = {
        "source_artifact_id": source["source"]["artifact_id"],
        "source_final_revision": source["source"]["final_revision"],
        "manifest_sha256": hashlib.sha256(canonical_scene_bytes(source)).hexdigest(),
        "descriptor_fingerprint": _descriptor()["fingerprint"],
    }

    imported = backend._publish_imported_scene(source, evidence)

    copied = tmp_path / "output" / "initial-scene-import.json"
    event = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert copied.read_bytes() == canonical_scene_bytes(source)
    assert event["artifact_id"] == imported["imported_artifact_id"]
    assert event["kind"] == "visualization_scene"
    assert event["role"] == "provenance"
    assert event["metadata"]["imported"] is True
    assert event["metadata"]["source_artifact_id"] == (source["source"]["artifact_id"])
    assert event["metadata"]["source_final_revision"] == 17


def test_package_advertises_initial_scene_as_optional_declared_file_input() -> None:
    package = object.__new__(package_module.Paraview)
    menu = {item["name"]: item for item in package._configure_menu()}

    assert menu["initial_scene"]["default"] == ""
    assert menu["initial_scene"]["input_binding"] == {
        "schema_version": "jarvis.configuration-input-binding.v1",
        "kind": "local_file",
        "structure": "regular_file",
    }


def test_empty_scene_default_does_not_require_initial_scene() -> None:
    package = object.__new__(package_module.Paraview)
    package.config = {
        "mode": "service",
        "dataset_descriptor": json.dumps(_descriptor()),
        "initial_scene": "",
    }

    package._configure()


def test_structured_scene_rejection_never_echoes_unsafe_input_path(
    tmp_path: Path,
) -> None:
    missing = (tmp_path / "private" / "missing.json").resolve()
    with pytest.raises(SceneManifestError) as captured:
        load_scene_manifest(missing)

    rendered = json.dumps(captured.value.as_dict(), sort_keys=True)
    assert captured.value.code == "unsafe_scene_path"
    assert str(missing) not in rendered
    assert os.fspath(tmp_path) not in rendered


def test_service_emits_structured_scene_rejection_before_readiness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    descriptor_path = tmp_path / "descriptor.json"
    descriptor_path.write_text(json.dumps(_descriptor()), encoding="utf-8")
    authorization_path = (tmp_path / "authorization.token").resolve()
    authorization_path.write_text("a" * 64 + "\n", encoding="ascii")
    if os.name != "nt":
        authorization_path.chmod(0o600)
    missing = (tmp_path / "private" / "missing-scene.json").resolve()
    monkeypatch.setenv("JARVIS_EXECUTION_ID", "exec-scene-rejection")
    monkeypatch.setenv("JARVIS_PACKAGE_NAME", "builtin.paraview")
    monkeypatch.setenv("JARVIS_PACKAGE_ID", "viewer")

    result = service_module.main(
        [
            "--descriptor",
            str(descriptor_path.resolve()),
            "--output-dir",
            str((tmp_path / "output").resolve()),
            "--bind-host",
            "127.0.0.1",
            "--port",
            "18080",
            "--execution-id",
            "exec-scene-rejection",
            "--service-instance-id",
            "srv-scene-rejection",
            "--authorization-file",
            str(authorization_path),
            "--initial-scene",
            str(missing),
        ]
    )

    captured = capsys.readouterr()
    assert result == 2
    assert captured.out == ""
    prefix = "JARVIS_PARAVIEW_SCENE_REJECTION "
    assert captured.err.startswith(prefix)
    evidence = json.loads(captured.err.removeprefix(prefix))
    assert evidence["code"] == "unsafe_scene_path"
    assert str(missing) not in captured.err
