"""Focused production-contract tests for the explicit ParaView v2 scene."""

from __future__ import annotations

import copy
import hashlib
import math
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, cast

import pytest
from jarvis_cd.service_runtime import (
    DatasetDescriptor,
    DatasetMember,
    calculate_dataset_fingerprint,
)

_BUILTIN_REPOSITORY_ROOT = Path(__file__).resolve().parents[3] / "builtin"
sys.path.insert(0, str(_BUILTIN_REPOSITORY_ROOT))

from builtin.paraview import service as service_module  # noqa: E402
from builtin.paraview import service_http as service_http_module  # noqa: E402
from builtin.paraview.service_http import (  # noqa: E402
    CommandError,
    ServiceStateController,
)

_PNG = b"\x89PNG\r\n\x1a\nproduction-scene"


class _ArrayInformation:
    def __init__(
        self,
        name: str,
        components: int,
        observed_range: tuple[float, float],
        tuples: int = 10,
    ) -> None:
        self.name = name
        self.components = components
        self.observed_range = observed_range
        self.tuples = tuples
        self.requested_components: list[int] = []

    def GetName(self) -> str:
        return self.name

    def GetNumberOfComponents(self) -> int:
        return self.components

    def GetComponentFiniteRange(self, component: int) -> tuple[float, float]:
        self.requested_components.append(component)
        return self.observed_range

    def GetNumberOfTuples(self) -> int:
        return self.tuples


class _AttributesInformation:
    def __init__(self, arrays: list[_ArrayInformation]) -> None:
        self.arrays = arrays

    def GetNumberOfArrays(self) -> int:
        return len(self.arrays)

    def GetArrayInformation(self, index: int) -> _ArrayInformation:
        return self.arrays[index]


class _DataInformation:
    def __init__(
        self,
        arrays: list[_ArrayInformation],
        *,
        cell_arrays: list[_ArrayInformation] | None = None,
        bounds: tuple[float, ...] = (0.0, 1.0, 0.0, 1.0, 0.0, 1.0),
        points: int = 10,
        cells: int = 5,
        raw_type: str = "vtkPolyData",
    ) -> None:
        self.point = _AttributesInformation(arrays)
        self.cell = _AttributesInformation(cell_arrays or [])
        self.bounds = bounds
        self.points = points
        self.cells = cells
        self.raw_type = raw_type

    def GetPointDataInformation(self) -> _AttributesInformation:
        return self.point

    def GetCellDataInformation(self) -> _AttributesInformation:
        return self.cell

    def GetBounds(self) -> tuple[float, ...]:
        return self.bounds

    def GetNumberOfPoints(self) -> int:
        return self.points

    def GetNumberOfCells(self) -> int:
        return self.cells

    def GetDataSetTypeAsString(self) -> str:
        return self.raw_type


class _Source:
    def __init__(self, information: _DataInformation) -> None:
        self.information = information
        self.update_times: list[float | None] = []
        self.TimestepValues: list[float] = []

    def GetDataInformation(self) -> _DataInformation:
        return self.information

    def UpdatePipeline(self, value: float | None = None) -> None:
        self.update_times.append(value)


class _Display:
    def __init__(self, source: _Source) -> None:
        self.source = source
        self.SMProxy = self
        self.Visibility = 1
        self.Opacity = 1.0
        self.Representation = "Surface"
        self.PointSize = 1.0
        self.ColorArrayName = ["NONE", ""]
        self.DiffuseColor: list[float] = []
        self.AmbientColor: list[float] = []
        self.scalar_bar_calls: list[bool] = []
        self.scalar_bar_visible = False
        self.scalar_bar_proxy: _ScalarBar | None = None

    def SetScalarBarVisibility(self, _view: object, visible: bool) -> None:
        self.scalar_bar_calls.append(visible)
        self.scalar_bar_visible = visible
        if self.scalar_bar_proxy is not None:
            self.scalar_bar_proxy.Visibility = 1 if visible else 0


class _Lookup:
    def __init__(self) -> None:
        self.preset_calls: list[str] = []
        self.ranges: list[tuple[float, float]] = []
        self.UseLogScale = 0
        self.inverted = 0

    def ApplyPreset(self, preset: str, _rescale: bool) -> bool:
        self.preset_calls.append(preset)
        return preset != "Missing preset"

    def RescaleTransferFunction(self, lower: float, upper: float) -> None:
        self.ranges.append((lower, upper))

    def InvertTransferFunction(self) -> None:
        self.inverted += 1

    def MapControlPointsToLogSpace(self) -> bool:
        return True

    def MapControlPointsToLinearSpace(self) -> bool:
        return True


class _Opacity:
    def __init__(self) -> None:
        self.ranges: list[tuple[float, float]] = []

    def RescaleTransferFunction(self, lower: float, upper: float) -> None:
        self.ranges.append((lower, upper))


class _ScalarBar:
    def __init__(self, lookup: _Lookup, view: object) -> None:
        self.lookup = lookup
        self.view = view
        self.Visibility = 0


class _SceneSimple:
    def __init__(self) -> None:
        self.displays: list[_Display] = []
        self.lookups: dict[tuple[_Display, str], _Lookup] = {}
        self.opacities: dict[tuple[_Display, str], _Opacity] = {}
        self.scalar_bars: dict[tuple[_Lookup, object], _ScalarBar] = {}
        self.color_calls: list[tuple[_Display, object, bool]] = []
        self.active_source: object | None = object()
        self.render_calls = 0
        self.rendered_scalar_bars: list[dict[_Display, bool]] = []
        self.proxy_manager: _ProxyManager | None = None
        self.deleted: list[object] = []

    def Show(self, source: _Source, _view: object) -> _Display:
        if self.displays:
            return self.displays[0]
        display = _Display(source)
        self.displays.append(display)
        return display

    def ColorBy(
        self,
        display: _Display,
        field: object,
        *,
        separate: bool = False,
    ) -> None:
        if field is None and display.ColorArrayName[0] == "NONE":
            raise RuntimeError("invalid association string 'NONE'")
        self.color_calls.append((display, field, separate))

    def GetColorTransferFunction(
        self,
        name: str,
        display: _Display,
        *,
        separate: bool,
    ) -> _Lookup:
        assert separate is True
        return self.lookups.setdefault((display, name), _Lookup())

    def GetOpacityTransferFunction(
        self,
        name: str,
        display: _Display,
        *,
        separate: bool,
    ) -> _Opacity:
        assert separate is True
        return self.opacities.setdefault((display, name), _Opacity())

    def GetScalarBar(self, lookup: _Lookup, view: object) -> _ScalarBar:
        key = (lookup, view)
        scalar_bar = self.scalar_bars.get(key)
        if scalar_bar is None:
            scalar_bar = _ScalarBar(lookup, view)
            self.scalar_bars[key] = scalar_bar
            if self.proxy_manager is not None:
                self.proxy_manager.RegisterProxy(
                    "scalar_bars",
                    f"scalar-bar-{len(self.scalar_bars)}",
                    scalar_bar,
                )
        matching_displays = [
            display
            for (display, _name), candidate in self.lookups.items()
            if candidate is lookup
        ]
        assert len(matching_displays) == 1
        matching_displays[0].scalar_bar_proxy = scalar_bar
        return scalar_bar

    def GetActiveSource(self) -> object | None:
        return self.active_source

    def SetActiveSource(self, source: object | None) -> None:
        self.active_source = source

    def Render(self, _view: object) -> None:
        self.render_calls += 1
        self.rendered_scalar_bars.append(
            {display: display.scalar_bar_visible for display in self.displays}
        )

    def Delete(self, proxy: object) -> None:
        self.deleted.append(proxy)
        if proxy in self.displays:
            self.displays.remove(cast(_Display, proxy))
        self.lookups = {
            key: value for key, value in self.lookups.items() if value is not proxy
        }
        self.opacities = {
            key: value for key, value in self.opacities.items() if value is not proxy
        }
        self.scalar_bars = {
            key: value for key, value in self.scalar_bars.items() if value is not proxy
        }
        for display in self.displays:
            if display.scalar_bar_proxy is proxy:
                display.scalar_bar_proxy = None
                display.scalar_bar_visible = False
        if self.proxy_manager is not None:
            self.proxy_manager.unregister_value(proxy)


class _ProxyManager:
    def __init__(self) -> None:
        self.proxies: dict[tuple[str, str], object] = {}

    def GetProxy(self, group: str, name: str) -> object | None:
        return self.proxies.get((group, name))

    def RegisterProxy(self, group: str, name: str, proxy: object) -> None:
        self.proxies[(group, name)] = proxy

    def unregister_value(self, proxy: object) -> None:
        self.proxies = {
            key: value for key, value in self.proxies.items() if value is not proxy
        }


def _field_color(
    *,
    preset: str | None,
    invert: bool = False,
    name: str = "velocity",
) -> dict[str, Any]:
    return {
        "mode": "field",
        "field": {"name": name, "association": "point"},
        "preset": preset,
        "invert": invert,
        "scale": {"mode": "linear"},
        "range_policy": {
            "mode": "fixed",
            "range": [1.0, 9.0],
            "timestep_behavior": "freeze",
        },
        "scalar_bar_visible": True,
    }


def _representation_backend() -> tuple[Any, _SceneSimple, _Source]:
    backend = cast(Any, object.__new__(service_module.ParaViewBackend))
    arrays = [
        _ArrayInformation("velocity", 3, (1.0, 9.0)),
        _ArrayInformation("temperature", 1, (10.0, 90.0)),
    ]
    source = _Source(_DataInformation(arrays))
    simple = _SceneSimple()
    proxy_manager = _ProxyManager()
    simple.proxy_manager = proxy_manager
    root_display = simple.Show(source, object())
    backend.simple = simple

    def create_representation(source_value: _Source, _view: object) -> _Display:
        display = _Display(source_value)
        simple.displays.append(display)
        return display

    backend.servermanager = SimpleNamespace(
        CreateRepresentation=create_representation,
        ProxyManager=lambda: proxy_manager,
    )
    backend.service_instance_id = "srv-test"
    backend.view = _CameraView()
    backend._nodes = {
        "node_root": {
            "node_id": "node_root",
            "kind": "reader",
            "input_node_ids": [],
            "filter": None,
            "output": {
                "topology": "points",
                "raw_data_type": "vtkPolyData",
                "bounds": [0.0, 1.0, 0.0, 1.0, 0.0, 1.0],
                "point_count": 10,
                "cell_count": 5,
                "arrays": [
                    {
                        "name": "velocity",
                        "association": "point",
                        "components": 3,
                        "units": None,
                    },
                    {
                        "name": "temperature",
                        "association": "point",
                        "components": 1,
                        "units": None,
                    },
                ],
            },
        }
    }
    backend._node_proxies = {"node_root": source}
    backend._representations = {
        "rep_root": {
            "representation_id": "rep_root",
            "node_id": "node_root",
            "type": "points",
            "visible": True,
            "opacity": 1.0,
            "point_size_px": 3.0,
            "color": {"mode": "solid", "rgb": [0.8, 0.8, 0.8]},
        }
    }
    backend._representation_displays = {"rep_root": root_display}
    backend._representation_transfer_proxies = {}
    backend._measurements = {}
    backend._selection = None
    backend._artifacts = []
    backend._transaction_open = False
    backend._pending_deletes = []
    backend._retired_proxies = []
    backend._staged_artifacts = {}
    backend._reader_timesteps = []
    backend._timesteps = []
    backend._timestep_index = 0
    return backend, simple, source


def test_same_node_supports_independent_surface_and_point_actors() -> None:
    """One data node can drive multiple independently configured actors."""
    backend, simple, _source = _representation_backend()

    first = backend._set_representation(
        {
            "representation_id": None,
            "node_id": "node_root",
            "type": "surface",
            "visible": True,
            "opacity": 0.7,
            "point_size_px": None,
            "color": _field_color(preset=None),
        },
        "surface-overlay",
    )["representation"]
    second = backend._set_representation(
        {
            "representation_id": None,
            "node_id": "node_root",
            "type": "points",
            "visible": True,
            "opacity": 1.0,
            "point_size_px": 6.0,
            "color": _field_color(preset="Viridis (matplotlib)", invert=True),
        },
        "point-overlay",
    )["representation"]

    first_display = backend._representation_displays[first["representation_id"]]
    second_display = backend._representation_displays[second["representation_id"]]
    assert first_display is not second_display
    assert (
        simple.lookups[(first_display, "velocity")]
        is not simple.lookups[(second_display, "velocity")]
    )
    assert first["color"]["preset"] == "Cool to Warm"
    assert second["color"]["preset"] == "Viridis (matplotlib)"
    assert first["color"]["observation"]["value_mode"] == "magnitude"
    assert simple.lookups[(first_display, "velocity")].preset_calls == ["Cool to Warm"]
    assert simple.lookups[(second_display, "velocity")].inverted == 1
    assert all(call[2] is True for call in simple.color_calls[-2:])

    with pytest.raises(CommandError) as captured:
        backend._set_representation(
            {
                "representation_id": first["representation_id"],
                "node_id": "node_root",
                "type": "surface",
                "visible": True,
                "opacity": 1.0,
                "point_size_px": None,
                "color": _field_color(preset="Missing preset"),
            },
            "bad-preset",
        )
    assert captured.value.code == "preset_not_found"


def test_solid_color_normalizes_unset_paraview_association() -> None:
    """Fresh solid actors never toggle a scalar bar without a bound lookup table."""
    backend, simple, _source = _representation_backend()
    display = backend._representation_displays["rep_root"]

    backend._apply_representation_record(backend._representations["rep_root"])

    assert display.scalar_bar_calls == []
    assert display.ColorArrayName == ["POINTS", ""]
    assert simple.color_calls[-1] == (display, None, False)
    assert display.DiffuseColor == [0.8, 0.8, 0.8]


def test_hidden_field_actor_resolves_and_validates_without_scalar_bar() -> None:
    """A hidden actor cannot claim a scalar bar embedded in the rendered frame."""
    backend, _simple, _source = _representation_backend()

    hidden = backend._set_representation(
        {
            "representation_id": None,
            "node_id": "node_root",
            "type": "points",
            "visible": False,
            "opacity": 1.0,
            "point_size_px": 4.0,
            "color": _field_color(preset=None),
        },
        "hidden-field",
    )["representation"]

    assert hidden["color"]["scalar_bar"] == {
        "visible": False,
        "embedded_in_frame": False,
    }
    display = backend._representation_displays[hidden["representation_id"]]
    assert display.scalar_bar_calls[-1] is False
    service_http_module._validate_representations(
        list(backend._representations.values()),
        backend._nodes,
    )

    contradictory = copy.deepcopy(hidden)
    contradictory["color"]["scalar_bar"] = {
        "visible": True,
        "embedded_in_frame": True,
    }
    with pytest.raises(RuntimeError, match="field color values"):
        service_http_module._validate_representations(
            [backend._representations["rep_root"], contradictory],
            backend._nodes,
        )


def test_registered_independent_display_is_removed_without_proxy_or_lut_leak() -> None:
    backend, simple, _source = _representation_backend()
    assert simple.proxy_manager is not None
    checkpoint = backend.begin_command()
    created = backend._set_representation(
        {
            "representation_id": None,
            "node_id": "node_root",
            "type": "surface",
            "visible": True,
            "opacity": 1.0,
            "point_size_px": None,
            "color": _field_color(preset=None),
        },
        "registered-display",
    )["representation"]
    display = backend._representation_displays[created["representation_id"]]
    assert len(simple.proxy_manager.proxies) == 2
    assert (display, "velocity") in simple.lookups
    lookup = simple.lookups[(display, "velocity")]
    opacity = simple.opacities[(display, "velocity")]
    scalar_bar = simple.scalar_bars[(lookup, backend.view)]
    backend.commit_command(checkpoint)

    removal_checkpoint = backend.begin_command()
    backend._remove_scene_object(
        {"object_id": created["representation_id"]},
        "remove-registered-display",
    )
    assert len(simple.proxy_manager.proxies) == 2
    backend.commit_command(removal_checkpoint)

    assert simple.proxy_manager.proxies == {}
    assert display not in simple.displays
    assert (display, "velocity") not in simple.lookups
    assert (display, "velocity") not in simple.opacities
    assert simple.scalar_bars == {}
    assert display in simple.deleted
    assert scalar_bar in simple.deleted
    assert lookup in simple.deleted
    assert opacity in simple.deleted
    assert simple.rendered_scalar_bars[-1][display] is False


def test_failed_actor_command_rolls_back_registered_display_exactly() -> None:
    backend, simple, _source = _representation_backend()
    assert simple.proxy_manager is not None
    checkpoint = backend.begin_command()

    with pytest.raises(CommandError, match="preset"):
        backend._set_representation(
            {
                "representation_id": None,
                "node_id": "node_root",
                "type": "surface",
                "visible": True,
                "opacity": 1.0,
                "point_size_px": None,
                "color": _field_color(preset="Missing preset"),
            },
            "rollback-display",
        )
    backend.rollback_command(checkpoint)

    assert simple.proxy_manager.proxies == {}
    assert list(backend._representations) == ["rep_root"]
    assert list(backend._representation_displays) == ["rep_root"]
    assert len(simple.displays) == 1
    assert simple.lookups == {}
    assert simple.opacities == {}
    assert simple.scalar_bars == {}


def test_post_execute_rollback_deletes_candidate_scalar_bar_and_actor() -> None:
    """A later frame/state failure cannot orphan a successfully built actor."""
    backend, simple, _source = _representation_backend()
    assert simple.proxy_manager is not None
    checkpoint = backend.begin_command()
    created = backend._set_representation(
        {
            "representation_id": None,
            "node_id": "node_root",
            "type": "surface",
            "visible": True,
            "opacity": 1.0,
            "point_size_px": None,
            "color": _field_color(preset=None),
        },
        "post-execute-rollback",
    )["representation"]
    representation_id = created["representation_id"]
    display = backend._representation_displays[representation_id]
    lookup = simple.lookups[(display, "velocity")]
    opacity = simple.opacities[(display, "velocity")]
    scalar_bar = simple.scalar_bars[(lookup, backend.view)]

    backend.rollback_command(checkpoint)

    assert simple.proxy_manager.proxies == {}
    assert list(backend._representations) == ["rep_root"]
    assert list(backend._representation_displays) == ["rep_root"]
    assert len(simple.displays) == 1
    assert simple.lookups == {}
    assert simple.opacities == {}
    assert simple.scalar_bars == {}
    assert scalar_bar in simple.deleted
    assert opacity in simple.deleted
    assert lookup in simple.deleted
    assert display in simple.deleted


def test_representation_removal_rollback_restores_exact_scalar_ownership() -> None:
    """A failed post-remove command restores the same actor and proxy trio."""
    backend, simple, _source = _representation_backend()
    created = backend.execute(
        "set_representation",
        {
            "representation_id": None,
            "node_id": "node_root",
            "type": "surface",
            "visible": True,
            "opacity": 1.0,
            "point_size_px": None,
            "color": _field_color(preset=None),
        },
        "remove-rollback-create",
    )["representation"]
    representation_id = created["representation_id"]
    display = backend._representation_displays[representation_id]
    original_proxies = backend._representation_transfer_proxies[representation_id]
    assert len(original_proxies) == 3
    checkpoint = backend.begin_command()

    backend._remove_scene_object(
        {"object_id": representation_id},
        "remove-rollback",
    )
    backend.rollback_command(checkpoint)

    assert backend._representation_displays[representation_id] is display
    assert backend._representation_transfer_proxies[representation_id] == (
        original_proxies
    )
    assert all(proxy not in simple.deleted for proxy in original_proxies)
    assert display not in simple.deleted
    assert display.Visibility == 1
    assert display.scalar_bar_visible is True
    assert display.scalar_bar_proxy is original_proxies[0]


def test_field_change_and_solid_color_retire_exact_transfer_proxies() -> None:
    """An actor owns its scalar bar, PWF, and LUT through field changes."""
    backend, simple, _source = _representation_backend()
    created = backend.execute(
        "set_representation",
        {
            "representation_id": None,
            "node_id": "node_root",
            "type": "surface",
            "visible": True,
            "opacity": 1.0,
            "point_size_px": None,
            "color": _field_color(preset=None),
        },
        "transfer-lifecycle",
    )["representation"]
    representation_id = created["representation_id"]
    display = backend._representation_displays[representation_id]
    velocity_lookup = simple.lookups[(display, "velocity")]
    velocity_opacity = simple.opacities[(display, "velocity")]
    velocity_scalar_bar = simple.scalar_bars[(velocity_lookup, backend.view)]

    backend.execute(
        "set_representation",
        {
            "representation_id": representation_id,
            "node_id": "node_root",
            "type": "surface",
            "visible": True,
            "opacity": 1.0,
            "point_size_px": None,
            "color": _field_color(preset=None, name="temperature"),
        },
        "transfer-field-change",
    )

    temperature_lookup = simple.lookups[(display, "temperature")]
    temperature_opacity = simple.opacities[(display, "temperature")]
    temperature_scalar_bar = simple.scalar_bars[(temperature_lookup, backend.view)]
    assert velocity_scalar_bar in simple.deleted
    assert velocity_lookup in simple.deleted
    assert velocity_opacity in simple.deleted
    assert (display, "velocity") not in simple.lookups
    assert (display, "velocity") not in simple.opacities
    assert backend._representation_transfer_proxies[representation_id] == (
        temperature_scalar_bar,
        temperature_opacity,
        temperature_lookup,
    )

    backend.execute(
        "set_representation",
        {
            "representation_id": representation_id,
            "node_id": "node_root",
            "type": "surface",
            "visible": True,
            "opacity": 1.0,
            "point_size_px": None,
            "color": {"mode": "solid", "rgb": [0.2, 0.4, 0.6]},
        },
        "transfer-solid",
    )

    assert temperature_scalar_bar in simple.deleted
    assert temperature_lookup in simple.deleted
    assert temperature_opacity in simple.deleted
    assert (display, "temperature") not in simple.lookups
    assert (display, "temperature") not in simple.opacities
    assert representation_id not in backend._representation_transfer_proxies
    assert display.scalar_bar_calls[-1] is False


def test_failed_field_change_restores_old_transfer_proxies_without_leak() -> None:
    """Rollback rebinds the old field before explicitly deleting new proxies."""
    backend, simple, _source = _representation_backend()
    created = backend.execute(
        "set_representation",
        {
            "representation_id": None,
            "node_id": "node_root",
            "type": "surface",
            "visible": True,
            "opacity": 1.0,
            "point_size_px": None,
            "color": _field_color(preset=None),
        },
        "rollback-transfer-create",
    )["representation"]
    representation_id = created["representation_id"]
    display = backend._representation_displays[representation_id]
    old_proxies = backend._representation_transfer_proxies[representation_id]
    checkpoint = backend.begin_command()

    with pytest.raises(CommandError, match="preset"):
        backend._set_representation(
            {
                "representation_id": representation_id,
                "node_id": "node_root",
                "type": "surface",
                "visible": True,
                "opacity": 1.0,
                "point_size_px": None,
                "color": _field_color(
                    preset="Missing preset",
                    name="temperature",
                ),
            },
            "rollback-transfer-change",
        )
    new_lookup = simple.lookups[(display, "temperature")]
    new_opacity = simple.opacities[(display, "temperature")]
    new_scalar_bar = simple.scalar_bars[(new_lookup, backend.view)]
    backend.rollback_command(checkpoint)

    assert backend._representation_transfer_proxies[representation_id] == old_proxies
    assert all(proxy not in simple.deleted for proxy in old_proxies)
    assert new_scalar_bar in simple.deleted
    assert new_lookup in simple.deleted
    assert new_opacity in simple.deleted
    assert (display, "temperature") not in simple.lookups
    assert (display, "temperature") not in simple.opacities
    assert (display, "velocity") in simple.lookups
    assert (display, "velocity") in simple.opacities
    old_scalar_bar = old_proxies[0]
    assert simple.scalar_bars[
        (simple.lookups[(display, "velocity")], backend.view)
    ] is (old_scalar_bar)


class _HistogramColumn:
    def __init__(self, values: list[float]) -> None:
        self.values = values

    def GetNumberOfComponents(self) -> int:
        return 1

    def GetTuple1(self, index: int) -> float:
        return self.values[index]


class _HistogramTable:
    def __init__(self) -> None:
        self.columns = [_HistogramColumn([2.0, 6.0]), _HistogramColumn([4.0, 6.0])]

    def GetNumberOfRows(self) -> int:
        return 2

    def GetNumberOfColumns(self) -> int:
        return 2

    def GetColumn(self, index: int) -> _HistogramColumn:
        return self.columns[index]


class _HistogramProxy:
    def __init__(self) -> None:
        self.SelectInputArray: list[str] = []
        self._component: int | None = None
        self.BinCount = 0
        self.CalculateAverages = 0
        self.Normalize = 0
        self.CenterBinsAroundMinAndMax = 0

    @property
    def Component(self) -> int | None:
        return self._component

    @Component.setter
    def Component(self, value: int) -> None:
        if isinstance(value, str):
            raise TypeError("ParaView Component is an IntVectorProperty")
        self._component = value

    def UpdatePipeline(self) -> None:
        return


def test_vector_histogram_uses_paraview_integer_magnitude_component() -> None:
    """ParaView 5.13 magnitude selection is components, never a string sentinel."""
    proxy = _HistogramProxy()
    simple = SimpleNamespace(
        Histogram=lambda **_kwargs: proxy,
        Delete=lambda _proxy: None,
        GetActiveSource=lambda: None,
        SetActiveSource=lambda _source: None,
    )
    backend = cast(Any, object.__new__(service_module.ParaViewBackend))
    backend.simple = simple
    backend.servermanager = SimpleNamespace(Fetch=lambda _proxy: _HistogramTable())

    distribution = backend._measure_distribution(
        object(),
        name="velocity",
        association="point",
        components=3,
        observed_range=[0.0, 8.0],
        tuple_count=10,
    )

    assert proxy.Component == 3
    assert distribution["status"] == "available"


def test_descriptor_topology_matches_whole_semantic_tokens_only() -> None:
    assert service_module._descriptor_topology("checkpoint-series") == "unknown"
    assert service_module._descriptor_topology("temporal-volume-series") == "volume"
    assert service_module._descriptor_topology("temporal-points") == "points"
    assert service_module._descriptor_topology("mesh-sequence") == "surface"


class _CameraView:
    def __init__(self) -> None:
        self.ViewSize: list[int] = []
        self.ViewTime: float | None = None
        self.CameraPosition = [4.0, 3.0, 2.0]
        self.CameraFocalPoint = [0.0, 0.0, 0.0]
        self.CameraViewUp = [0.0, 1.0, 0.0]
        self.CameraParallelScale = 5.0
        self.CameraParallelProjection = 0
        self.CameraViewAngle = 30.0
        self.reset_bounds: list[list[float]] = []

    def ResetCamera(self, bounds: list[float]) -> None:
        self.reset_bounds.append(list(bounds))
        center = [
            (bounds[0] + bounds[1]) / 2.0,
            (bounds[2] + bounds[3]) / 2.0,
            (bounds[4] + bounds[5]) / 2.0,
        ]
        self.CameraFocalPoint = center
        self.CameraPosition = [center[0] + 10.0, center[1], center[2]]
        self.CameraParallelScale = 4.0


class _InitSimple(_SceneSimple):
    def __init__(self, source: _Source) -> None:
        super().__init__()
        self.source = source
        self.view = _CameraView()

    def OpenDataFile(self, _value: object) -> _Source:
        return self.source

    def GetActiveViewOrCreate(self, _kind: str) -> _CameraView:
        return self.view

    def ResetCamera(self, _view: object) -> None:
        return


def test_point_topology_initializes_readable_point_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Generic point datasets start as points without dataset-specific recipes."""
    source = _Source(_DataInformation([], points=20, cells=0))
    simple = _InitSimple(source)
    modules = {
        "paraview.simple": simple,
        "paraview.servermanager": SimpleNamespace(),
        "paraview.vtk": SimpleNamespace(),
    }
    monkeypatch.setattr(
        service_module.importlib,
        "import_module",
        lambda name: modules[name],
    )
    original_exists = Path.exists

    def exists(path: Path) -> bool:
        if path.as_posix() == "/cluster/input.vtp":
            return True
        return original_exists(path)

    monkeypatch.setattr(Path, "exists", exists)
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    backend = service_module.ParaViewBackend(
        descriptor=_descriptor(),
        output_dir=output_dir,
        service_instance_id="srv-points",
        execution_id="exec-test",
        package_name="builtin.paraview",
        package_id="viewer",
    )
    pipeline = backend.pipeline_state()

    assert set(pipeline) == {
        "timestep",
        "nodes",
        "representations",
        "measurements",
        "camera",
        "selection",
        "artifacts",
    }
    assert pipeline["nodes"][0]["output"]["topology"] == "points"
    assert pipeline["representations"][0]["type"] == "points"
    assert pipeline["representations"][0]["point_size_px"] == 3.0


def test_initial_temporal_summary_uses_exact_first_reader_time(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Root discovery and first pixels are produced only after exact time zeroing."""

    class TemporalSource(_Source):
        def GetDataInformation(self) -> _DataInformation:
            assert self.update_times[-1] == 2.5
            return super().GetDataInformation()

    source = TemporalSource(_DataInformation([], points=4, cells=2))
    source.TimestepValues = [2.5, 7.5]
    simple = _InitSimple(source)
    modules = {
        "paraview.simple": simple,
        "paraview.servermanager": SimpleNamespace(),
        "paraview.vtk": SimpleNamespace(),
    }
    monkeypatch.setattr(
        service_module.importlib,
        "import_module",
        lambda name: modules[name],
    )
    original_exists = Path.exists
    monkeypatch.setattr(
        Path,
        "exists",
        lambda path: (
            True if path.as_posix() == "/cluster/input.vtp" else original_exists(path)
        ),
    )

    backend = service_module.ParaViewBackend(
        descriptor=_descriptor(),
        output_dir=tmp_path / "output",
        service_instance_id="srv-time",
        execution_id="exec-test",
        package_name="builtin.paraview",
        package_id="viewer",
    )

    assert backend.view.ViewTime == 2.5
    assert source.update_times == [None, 2.5]
    assert backend.pipeline_state()["timestep"] == {
        "index": 0,
        "value": 2.5,
        "count": 2,
    }


@pytest.mark.parametrize(
    ("point_count", "cell_count"),
    [(257, 0), (0, 257), (200, 57)],
)
def test_array_discovery_fails_when_raw_total_exceeds_contract(
    point_count: int,
    cell_count: int,
) -> None:
    """Point and cell metadata never truncate or exceed the shared 256 cap."""
    point_arrays = [
        _ArrayInformation(f"point_{index}", 1, (0.0, 1.0))
        for index in range(point_count)
    ]
    cell_arrays = [
        _ArrayInformation(f"cell_{index}", 1, (0.0, 1.0)) for index in range(cell_count)
    ]
    source = _Source(_DataInformation(point_arrays, cell_arrays=cell_arrays))
    backend = cast(Any, object.__new__(service_module.ParaViewBackend))

    with pytest.raises(RuntimeError, match="array count exceeds 256"):
        backend._discover_arrays(source)
    with pytest.raises(RuntimeError, match="array count exceeds 256"):
        backend._array_information(
            source,
            point_arrays[-1].name if point_arrays else cell_arrays[-1].name,
            "point" if point_arrays else "cell",
            1,
        )


def test_array_discovery_iterates_every_admitted_point_and_cell_array() -> None:
    point_arrays = [
        _ArrayInformation(f"point_{index}", 1, (0.0, 1.0)) for index in range(128)
    ]
    cell_arrays = [
        _ArrayInformation(f"cell_{index}", 1, (0.0, 1.0)) for index in range(128)
    ]
    source = _Source(_DataInformation(point_arrays, cell_arrays=cell_arrays))
    backend = cast(Any, object.__new__(service_module.ParaViewBackend))

    discovered = backend._discover_arrays(source)

    assert len(discovered) == 256
    assert discovered[-1]["name"] == "cell_127"
    assert backend._array_information(source, "cell_127", "cell", 1) is cell_arrays[-1]


def test_http_array_caps_match_descriptor_and_relay_contract() -> None:
    arrays = [
        {
            "name": f"field_{index}",
            "association": "point",
            "components": 1,
            "units": None,
        }
        for index in range(257)
    ]
    with pytest.raises(RuntimeError, match="discovery arrays"):
        service_http_module._validate_dataset_discovery(
            {"arrays": arrays, "bounds": None, "timestep_values": []}
        )
    with pytest.raises(RuntimeError, match="output arrays"):
        service_http_module._validate_output_summary(
            {
                "topology": "surface",
                "raw_data_type": "vtkPolyData",
                "bounds": None,
                "point_count": 0,
                "cell_count": 0,
                "arrays": arrays,
            }
        )


def test_scene_v2_rejects_table_topology_until_chart_semantics_exist(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    members = (DatasetMember(index=0, location="/cluster/table.csv"),)
    descriptor = DatasetDescriptor(
        dataset_id="table-test",
        kind="table",
        format="csv",
        members=members,
        fingerprint=calculate_dataset_fingerprint(
            dataset_id="table-test",
            kind="table",
            format="csv",
            members=members,
        ),
    ).to_dict()
    modules = {
        "paraview.simple": SimpleNamespace(),
        "paraview.servermanager": SimpleNamespace(),
        "paraview.vtk": SimpleNamespace(),
    }
    monkeypatch.setattr(
        service_module.importlib,
        "import_module",
        lambda name: modules[name],
    )

    with pytest.raises(ValueError, match="does not support table"):
        service_module.ParaViewBackend(
            descriptor=descriptor,
            output_dir=tmp_path,
            service_instance_id="srv-table",
            execution_id="exec-test",
            package_name="builtin.paraview",
            package_id="viewer",
        )
    with pytest.raises(RuntimeError, match="output summary values"):
        service_http_module._validate_output_summary(
            {
                "topology": "table",
                "raw_data_type": "vtkTable",
                "bounds": None,
                "point_count": 0,
                "cell_count": 0,
                "arrays": [],
            }
        )


class _MeasureSimple:
    def __init__(self) -> None:
        self.active_source: object | None = object()
        self.histograms: list[_HistogramProxy] = []
        self.deleted: list[object] = []

    def Histogram(self, **_kwargs: object) -> _HistogramProxy:
        proxy = _HistogramProxy()
        self.histograms.append(proxy)
        return proxy

    def Delete(self, proxy: object) -> None:
        self.deleted.append(proxy)

    def GetActiveSource(self) -> object | None:
        return self.active_source

    def SetActiveSource(self, source: object | None) -> None:
        self.active_source = source

    def Render(self, _view: object) -> None:
        return


def test_measurement_is_queryable_and_restores_temporal_scene() -> None:
    """Measurement persists evidence while restoring time, camera, and source."""
    array = _ArrayInformation("temperature", 1, (0.0, 8.0), tuples=10)
    source = _Source(_DataInformation([array]))
    simple = _MeasureSimple()
    backend = cast(Any, object.__new__(service_module.ParaViewBackend))
    backend.simple = simple
    backend.servermanager = SimpleNamespace(Fetch=lambda _proxy: _HistogramTable())
    backend.view = _CameraView()
    backend._nodes = {
        "node_root": {
            "node_id": "node_root",
            "output": {
                "arrays": [
                    {
                        "name": "temperature",
                        "association": "point",
                        "components": 1,
                        "units": "K",
                    }
                ]
            },
        }
    }
    backend._node_proxies = {"node_root": source}
    backend._measurements = {}
    backend._representations = {}
    backend._reader_timesteps = [0.0, 1.0]
    backend._timesteps = [10.0, 20.0]
    backend._timestep_index = 0
    original_camera = backend._camera_state()
    original_active = simple.active_source

    measurement = backend._measure_field(
        {
            "node_id": "node_root",
            "name": "temperature",
            "association": "point",
            "timestep_indices": [0, 1],
        },
        "measure-temperature",
    )["measurement"]

    assert measurement["value_mode"] == "scalar"
    assert measurement["timestep_indices"] == [0, 1]
    assert len(measurement["samples"]) == 2
    assert measurement["aggregate"]["distribution"]["status"] == "available"
    assert backend._camera_state() == original_camera
    assert backend.view.ViewTime == 0.0
    assert backend._timestep_index == 0
    assert simple.active_source is original_active
    assert source.update_times == [0.0, 1.0, 0.0]
    assert all(proxy.Component == 0 for proxy in simple.histograms)


def test_cumulative_measurement_budget_rejects_before_time_mutation() -> None:
    backend = cast(Any, object.__new__(service_module.ParaViewBackend))
    source = _Source(_DataInformation([]))
    backend._measurements = {
        "mea_full": {"samples": [{}] * service_module.MAX_STORED_MEASUREMENT_SAMPLES}
    }
    backend._nodes = {"node_root": {"output": {"arrays": []}}}
    backend._node_proxies = {"node_root": source}
    backend._timesteps = []
    backend._timestep_index = 0

    with pytest.raises(CommandError) as captured:
        backend._measure_field(
            {
                "node_id": "node_root",
                "name": "temperature",
                "association": "point",
                "timestep_indices": [0],
            },
            "over-budget",
        )

    assert captured.value.code == "measurement_sample_limit"
    assert source.update_times == []


class _ContourProxy:
    def __init__(self) -> None:
        self.ContourBy: list[str] = []
        self.Isosurfaces: list[float] = []
        self.ComputeScalars = 0
        self.updated = False
        self.update_times: list[float | None] = []

    def UpdatePipeline(self, value: float | None = None) -> None:
        self.updated = True
        self.update_times.append(value)


def test_contour_is_point_scalar_topology_without_actor_side_effects() -> None:
    proxy = _ContourProxy()
    active = object()
    simple = SimpleNamespace(
        Contour=lambda **_kwargs: proxy,
        Delete=lambda _proxy: None,
        GetActiveSource=lambda: active,
        SetActiveSource=lambda source: setattr(simple, "restored", source),
    )
    backend = cast(Any, object.__new__(service_module.ParaViewBackend))
    backend.simple = simple
    backend._nodes = {
        "node_root": {
            "node_id": "node_root",
            "output": {
                "topology": "volume",
                "arrays": [
                    {
                        "name": "temperature",
                        "association": "point",
                        "components": 1,
                        "units": None,
                    }
                ],
            },
        }
    }
    backend._node_proxies = {"node_root": object()}
    backend._reader_timesteps = [0.0, 1.0]
    backend._timestep_index = 1
    backend._output_summary = lambda _proxy, *, topology: {
        "topology": topology,
        "raw_data_type": "vtkPolyData",
        "bounds": None,
        "point_count": 0,
        "cell_count": 0,
        "arrays": [],
    }

    node = backend._create_filter(
        {
            "input_node_id": "node_root",
            "type": "contour",
            "parameters": {
                "name": "temperature",
                "association": "point",
                "isovalues": [1.0, 2.0],
            },
        },
        "contour-node",
    )["node"]

    assert proxy.ContourBy == ["POINTS", "temperature"]
    assert proxy.Isosurfaces == [1.0, 2.0]
    assert proxy.updated is True
    assert proxy.update_times == [1.0]
    assert node["output"]["topology"] == "surface"
    assert simple.restored is active
    assert not hasattr(simple, "Show")


@pytest.mark.parametrize(
    ("projection", "expected_distance", "expected_parallel_scale"),
    [
        ("perspective", 11.0, 4.0),
        ("parallel", 10.0, 4.4),
    ],
)
def test_fit_camera_unions_real_bounds_across_actors_and_time(
    projection: str,
    expected_distance: float,
    expected_parallel_scale: float,
) -> None:
    backend = cast(Any, object.__new__(service_module.ParaViewBackend))
    first = _Source(_DataInformation([]))
    second = _Source(_DataInformation([]))
    backend.view = _CameraView()
    backend.view.CameraParallelProjection = 1 if projection == "parallel" else 0
    backend.simple = _MeasureSimple()
    backend._reader_timesteps = [0.0, 1.0]
    backend._timesteps = [10.0, 20.0]
    backend._timestep_index = 0
    backend._node_proxies = {"node_a": first, "node_b": second}
    backend._nodes = {"node_a": {}, "node_b": {}}
    backend._representations = {
        "rep_a": {"node_id": "node_a", "visible": True},
        "rep_b": {"node_id": "node_b", "visible": True},
    }
    bounds = {
        ("node_a", 0.0): (-1.0, 1.0, 0.0, 2.0, 0.0, 1.0),
        ("node_a", 1.0): (-2.0, 1.0, -1.0, 2.0, 0.0, 1.0),
        ("node_b", 0.0): (0.0, 3.0, 1.0, 4.0, -1.0, 2.0),
        ("node_b", 1.0): (0.0, 2.0, 1.0, 5.0, -2.0, 2.0),
    }
    proxy_names = {id(first): "node_a", id(second): "node_b"}
    backend._discover_bounds = lambda proxy: bounds[
        (proxy_names[id(proxy)], backend.view.ViewTime)
    ]

    result = backend._fit_camera(
        {
            "representation_ids": ["rep_a", "rep_b"],
            "timestep_indices": [0, 1],
            "padding": 1.1,
        },
        "fit-all",
    )

    assert result["bounds"] == [-2.0, 3.0, -1.0, 5.0, -2.0, 2.0]
    assert backend.view.reset_bounds == [result["bounds"]]
    distance = math.sqrt(
        sum(
            (backend.view.CameraPosition[index] - backend.view.CameraFocalPoint[index])
            ** 2
            for index in range(3)
        )
    )
    assert distance == pytest.approx(expected_distance)
    assert backend.view.CameraParallelScale == pytest.approx(expected_parallel_scale)
    assert result["camera"]["projection"] == projection
    assert backend.view.ViewTime == 0.0
    assert backend._timestep_index == 0
    assert result["representation_ids"] == ["rep_a", "rep_b"]


class _Collection:
    def GetNumberOfItems(self) -> int:
        return 0

    def GetItemAsObject(self, _index: int) -> object:
        raise IndexError


class _PointSelectionView:
    def __init__(self) -> None:
        self.point_calls = 0

    def SelectSurfacePoints(self, *_args: object) -> None:
        self.point_calls += 1

    def SelectSurfaceCells(self, *_args: object) -> None:
        raise AssertionError("point actors must not use cell selection")


def test_point_actor_selection_uses_point_picker_and_target_identity() -> None:
    source = _Source(_DataInformation([], points=10, cells=0))
    display = object()
    view = _PointSelectionView()
    cleared: list[object] = []
    backend = cast(Any, object.__new__(service_module.ParaViewBackend))
    backend.view = view
    backend.simple = SimpleNamespace(
        Render=lambda _view: None,
        ClearSelection=lambda target: cleared.append(target),
    )
    backend.vtk = SimpleNamespace(vtkCollection=_Collection)
    backend.servermanager = SimpleNamespace()
    backend._representations = {
        "rep_points": {"node_id": "node_root", "visible": True, "type": "points"}
    }
    backend._representation_displays = {"rep_points": display}
    backend._node_proxies = {"node_root": source}
    backend._selection = None

    selection = backend._inspect_selection(
        {
            "representation_id": "rep_points",
            "viewport": {"x0": 0.1, "y0": 0.1, "x1": 0.2, "y1": 0.2},
        },
        "select-points",
    )["selection"]

    assert view.point_calls == 1
    assert selection["representation_id"] == "rep_points"
    assert selection["node_id"] == "node_root"
    assert selection["association"] == "point"
    assert selection["status"] == "empty"
    assert cleared == [source]


def test_viewport_selection_fails_closed_before_large_id_materialization() -> None:
    source = _Source(
        _DataInformation(
            [],
            points=service_module.MAX_VIEWPORT_SELECTION_SOURCE_ELEMENTS + 1,
            cells=0,
        )
    )
    view = _PointSelectionView()
    calls = {"render": 0, "clear": 0}
    backend = cast(Any, object.__new__(service_module.ParaViewBackend))
    backend.view = view
    backend.simple = SimpleNamespace(
        Render=lambda _view: calls.__setitem__("render", calls["render"] + 1),
        ClearSelection=lambda _source: calls.__setitem__("clear", calls["clear"] + 1),
    )
    backend._representations = {
        "rep_points": {"node_id": "node_root", "visible": True, "type": "points"}
    }
    backend._representation_displays = {"rep_points": _Display(source)}
    backend._node_proxies = {"node_root": source}
    backend._selection = None

    selection = backend._inspect_viewport_selection(
        "rep_points",
        {"x0": 0.1, "y0": 0.1, "x1": 0.2, "y1": 0.2},
    )["selection"]

    assert selection["status"] == "unsupported"
    assert selection["reason"] == (
        "paraview_selection_source_exceeds_materialization_limit"
    )
    assert selection["ids"] == []
    assert view.point_calls == 0
    assert calls == {"render": 0, "clear": 0}


def test_element_inspection_targets_actor_state_without_paraview_selection() -> None:
    """Process-zero element inspection changes no highlight, actor, or pixel state."""
    source = _Source(_DataInformation([], points=12, cells=7))
    first_display = _Display(source)
    second_display = _Display(source)
    calls = {"select": 0, "render": 0, "clear": 0}
    simple = SimpleNamespace(
        SelectIDs=lambda **_kwargs: calls.__setitem__("select", calls["select"] + 1),
        Render=lambda _view: calls.__setitem__("render", calls["render"] + 1),
        ClearSelection=lambda _source: calls.__setitem__("clear", calls["clear"] + 1),
    )
    backend = cast(Any, object.__new__(service_module.ParaViewBackend))
    backend.simple = simple
    backend._representations = {
        "rep_first": {"node_id": "node_root", "visible": True, "type": "surface"},
        "rep_second": {"node_id": "node_root", "visible": True, "type": "surface"},
    }
    backend._representation_displays = {
        "rep_first": first_display,
        "rep_second": second_display,
    }
    backend._node_proxies = {"node_root": source}
    backend._nodes = {
        "node_root": {
            "output": {
                "raw_data_type": "vtkPolyData",
            }
        }
    }
    backend._selection = None
    pixel_state = [
        (display.Visibility, display.Opacity, list(display.scalar_bar_calls))
        for display in (first_display, second_display)
    ]

    selection = backend._inspect_selection(
        {
            "representation_id": "rep_second",
            "element": {"association": "cell", "index": 6},
        },
        "inspect-element",
    )["selection"]

    assert selection["representation_id"] == "rep_second"
    assert selection["ids"] == [{"process_id": 0, "element_id": 6}]
    assert selection["element_count"] == 7
    assert calls == {"select": 0, "render": 0, "clear": 0}
    assert pixel_state == [
        (display.Visibility, display.Opacity, list(display.scalar_bar_calls))
        for display in (first_display, second_display)
    ]


@pytest.mark.parametrize(
    "raw_data_type",
    [
        "vtkMultiBlockDataSet",
        "vtkPartitionedDataSetCollection",
        "vtkHierarchicalBoxDataSet",
        "vtkOverlappingAMR",
    ],
)
def test_element_inspection_rejects_ambiguous_composite_identity(
    raw_data_type: str,
) -> None:
    source = _Source(_DataInformation([], raw_type=raw_data_type))
    backend = cast(Any, object.__new__(service_module.ParaViewBackend))
    backend._representations = {
        "rep_root": {"node_id": "node_root", "visible": True, "type": "surface"}
    }
    backend._node_proxies = {"node_root": source}
    backend._nodes = {"node_root": {"output": {"raw_data_type": raw_data_type}}}

    with pytest.raises(CommandError) as captured:
        backend._inspect_element_selection(
            "rep_root",
            {"association": "cell", "index": 0},
        )

    assert captured.value.code == "ambiguous_element_selection"
    assert captured.value.details == {"raw_data_type": raw_data_type}


def test_remove_rejects_live_measurement_and_node_dependencies() -> None:
    backend = cast(Any, object.__new__(service_module.ParaViewBackend))
    backend._retired_proxies = []
    backend._pending_deletes = []
    backend._selection = None
    backend._measurements = {
        "mea_temperature": {"node_id": "node_filter", "samples": []}
    }
    backend._representations = {
        "rep_filter": {
            "node_id": "node_filter",
            "color": {
                "mode": "field",
                "range_policy": {"measurement_id": "mea_temperature"},
            },
        }
    }
    backend._nodes = {
        "node_root": {"input_node_ids": []},
        "node_filter": {"input_node_ids": ["node_root"]},
        "node_child": {"input_node_ids": ["node_filter"]},
    }
    backend._node_proxies = {"node_filter": object()}

    with pytest.raises(CommandError) as measurement_error:
        backend._remove_scene_object(
            {"object_id": "mea_temperature"}, "remove-measurement"
        )
    assert measurement_error.value.code == "scene_dependency"
    assert measurement_error.value.details == {"representation_ids": ["rep_filter"]}

    with pytest.raises(CommandError) as node_error:
        backend._remove_scene_object({"object_id": "node_filter"}, "remove-node")
    assert node_error.value.code == "scene_dependency"
    assert node_error.value.details == {
        "node_ids": ["node_child"],
        "representation_ids": ["rep_filter"],
        "measurement_ids": ["mea_temperature"],
    }


def test_multi_member_physical_time_requires_real_reader_time_axis() -> None:
    backend = cast(Any, object.__new__(service_module.ParaViewBackend))
    backend.descriptor = {
        "members": [
            {"index": index, "location": f"/cluster/{index}.vti", "timestep": index}
            for index in range(5)
        ]
    }
    with pytest.raises(RuntimeError, match="count differs"):
        backend._resolve_timesteps([])


def _descriptor() -> dict[str, Any]:
    members = (DatasetMember(index=0, location="/cluster/input.vtp"),)
    fingerprint = calculate_dataset_fingerprint(
        dataset_id="dataset-valid",
        kind="temporal-points",
        format="vtk-polydata",
        members=members,
    )
    return DatasetDescriptor(
        dataset_id="dataset-valid",
        kind="temporal-points",
        format="vtk-polydata",
        members=members,
        fingerprint=fingerprint,
    ).to_dict()


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda value: value.update(dataset_id="bad id"), "dataset_id"),
        (lambda value: value.update(kind="\x00points"), "kind"),
        (
            lambda value: value["members"].append(dict(value["members"][0], index=1)),
            "locations must be unique",
        ),
        (
            lambda value: value.update(
                arrays=[
                    {"name": "a", "association": "point", "components": 1},
                    {"name": "a", "association": "point", "components": 1},
                ]
            ),
            "identities must be unique",
        ),
        (lambda value: value.update(bounds=[1, 0, 0, 1, 0, 1]), "bounds"),
        (
            lambda value: value.update(
                source_artifact={"artifact_id": "bad", "sha256": "A" * 64}
            ),
            "source artifact",
        ),
        (
            lambda value: value["fingerprint"].update(digest="A" * 64),
            "fingerprint",
        ),
    ],
)
def test_descriptor_validation_rejects_relay_unbindable_shapes(
    mutate: Any,
    message: str,
) -> None:
    value = _descriptor()
    mutate(value)
    with pytest.raises(ValueError, match=message):
        service_module._validate_descriptor(value)


def _transport_backend() -> Any:
    class Backend:
        def __init__(self) -> None:
            self.index = 0
            self.open = False
            self.execute_calls = 0
            self.commit_calls = 0
            self.rollback_calls = 0

        def dataset_state(self) -> dict[str, Any]:
            return {
                "descriptor": {"schema_version": "jarvis.dataset-descriptor.v1"},
                "discovery": {
                    "arrays": [],
                    "bounds": None,
                    "timestep_values": [0.0, 1.0],
                },
            }

        def pipeline_state(self) -> dict[str, Any]:
            return {
                "timestep": {
                    "index": self.index,
                    "value": float(self.index),
                    "count": 2,
                },
                "nodes": [
                    {
                        "node_id": "node_root",
                        "kind": "reader",
                        "input_node_ids": [],
                        "filter": None,
                        "output": {
                            "topology": "unknown",
                            "raw_data_type": None,
                            "bounds": None,
                            "point_count": 0,
                            "cell_count": 0,
                            "arrays": [],
                        },
                    }
                ],
                "representations": [
                    {
                        "representation_id": "rep_root",
                        "node_id": "node_root",
                        "type": "surface",
                        "visible": True,
                        "opacity": 1.0,
                        "point_size_px": None,
                        "color": {"mode": "solid", "rgb": [0.8, 0.8, 0.8]},
                    }
                ],
                "measurements": [],
                "camera": {
                    "position": [1.0, 1.0, 1.0],
                    "focal_point": [0.0, 0.0, 0.0],
                    "view_up": [0.0, 1.0, 0.0],
                    "parallel_scale": 1.0,
                    "projection": "perspective",
                    "view_angle": 30.0,
                },
                "selection": None,
                "artifacts": [],
            }

        def execute(
            self,
            operation: str,
            arguments: Mapping[str, Any],
            command_id: str,
        ) -> dict[str, Any]:
            del operation, command_id
            self.execute_calls += 1
            self.index = cast(int, arguments["index"])
            return {"timestep": {"index": self.index, "value": float(self.index)}}

        def render_png(self) -> bytes:
            return _PNG

        def begin_command(self) -> int:
            self.open = True
            return self.index

        def commit_command(self, checkpoint: object) -> None:
            del checkpoint
            self.commit_calls += 1
            self.open = False

        def rollback_command(self, checkpoint: object) -> None:
            self.rollback_calls += 1
            self.index = cast(int, checkpoint)
            self.open = False

    return Backend()


def _command() -> dict[str, Any]:
    return {
        "schema_version": "jarvis.paraview.command.v2",
        "command_id": "bounded-state",
        "operation": "set_timestep",
        "expected_revision": 1,
        "arguments": {"index": 1},
    }


def test_state_and_response_budgets_rollback_before_semantic_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _transport_backend()
    controller = ServiceStateController(
        backend=backend,
        execution_id="exec-test",
        package_name="builtin.paraview",
        package_id="viewer",
        service_instance_id="srv-test",
    )
    monkeypatch.setattr(service_http_module, "MAX_STATE_BYTES", 1)
    with pytest.raises(CommandError) as state_failure:
        controller.command(_command())
    assert state_failure.value.code == "operation_failed"
    assert str(state_failure.value) == "ParaView operation failed"
    assert backend.index == 0
    assert controller.state()["revision"] == 1

    monkeypatch.setattr(service_http_module, "MAX_STATE_BYTES", 8 * 1024 * 1024)
    monkeypatch.setattr(service_http_module, "MAX_RESPONSE_BYTES", 1)
    with pytest.raises(CommandError) as captured:
        controller.command(_command())
    assert captured.value.code == "operation_failed"
    assert str(captured.value) == "ParaView operation failed"
    assert backend.index == 0
    assert controller.state()["revision"] == 1


def test_backend_exception_is_fixed_public_error_and_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backend diagnostics never cross the public command boundary."""
    backend = _transport_backend()
    controller = ServiceStateController(
        backend=backend,
        execution_id="exec-test",
        package_name="builtin.paraview",
        package_id="viewer",
        service_instance_id="srv-test",
    )
    secret = "private-path-/mnt/internal/" + "X" * 4096

    def fail_execute(
        _operation: str,
        _arguments: Mapping[str, Any],
        _command_id: str,
    ) -> dict[str, Any]:
        raise RuntimeError(secret)

    monkeypatch.setattr(backend, "execute", fail_execute)

    with pytest.raises(CommandError) as captured:
        controller.command(_command())

    assert captured.value.code == "operation_failed"
    assert str(captured.value) == "ParaView operation failed"
    assert secret not in str(captured.value)
    assert backend.index == 0
    assert backend.commit_calls == 0
    assert backend.rollback_calls == 1
    assert controller.state()["revision"] == 1


def test_idempotency_payload_budget_rolls_back_before_commit() -> None:
    backend = _transport_backend()
    controller = ServiceStateController(
        backend=backend,
        execution_id="exec-test",
        package_name="builtin.paraview",
        package_id="viewer",
        service_instance_id="srv-test",
        max_idempotency_bytes=1,
    )

    with pytest.raises(CommandError) as captured:
        controller.command(_command())

    assert captured.value.code == "idempotency_payload_limit"
    assert backend.index == 0
    assert backend.commit_calls == 0
    assert backend.rollback_calls == 1
    assert controller.state()["revision"] == 1
    assert controller._results == {}
    assert controller._idempotency_payload_bytes == 0


def test_idempotency_cache_is_bounded_bytes_and_replays_exactly() -> None:
    backend = _transport_backend()
    controller = ServiceStateController(
        backend=backend,
        execution_id="exec-test",
        package_name="builtin.paraview",
        package_id="viewer",
        service_instance_id="srv-test",
        max_idempotency_bytes=64 * 1024,
    )
    command = _command()

    first = controller.command(command)
    replay = controller.command(copy.deepcopy(command))

    assert replay == first
    assert backend.execute_calls == 1
    cached_request, cached_response = controller._results[command["command_id"]]
    assert isinstance(cached_request, bytes)
    assert isinstance(cached_response, bytes)
    assert controller._idempotency_payload_bytes == (
        len(cached_request) + len(cached_response)
    )

    controller.max_idempotency_bytes = controller._idempotency_payload_bytes + 1
    second = copy.deepcopy(command)
    second["command_id"] = "bounded-state-2"
    second["expected_revision"] = 2
    second["arguments"]["index"] = 0
    with pytest.raises(CommandError) as captured:
        controller.command(second)
    assert captured.value.code == "idempotency_payload_limit"
    assert backend.index == 1
    assert controller.state()["revision"] == 2
    assert controller.command(command) == first


def test_http_state_rejects_discovery_timestep_disagreement() -> None:
    backend = _transport_backend()
    state = {
        "schema_version": service_http_module.STATE_SCHEMA,
        "service_instance_id": "srv-test",
        "revision": 1,
        "execution_id": "exec-test",
        "dataset": backend.dataset_state(),
        "pipeline": backend.pipeline_state(),
    }
    state["pipeline"]["timestep"] = {"index": 0, "value": 99.0, "count": 2}
    with pytest.raises(RuntimeError, match="disagrees"):
        service_http_module._validate_state_shape(state)


def _http_measurement_fixture() -> tuple[dict[str, Any], dict[str, Any]]:
    field = {
        "name": "temperature",
        "association": "point",
        "components": 1,
        "units": "K",
    }
    samples = []
    for index, timestep_value in enumerate((0.0, 1.0)):
        distribution = service_module._histogram_evidence(
            [(0.5, 10.0)],
            observed_range=[0.0, 1.0],
            tuple_count=10,
            method="paraview.histogram-filter",
        )
        samples.append(
            {
                "timestep_index": index,
                "timestep_value": timestep_value,
                "observed_range": [0.0, 1.0],
                "tuple_count": 10,
                "distribution": distribution,
            }
        )
    measurement = {
        "measurement_id": "mea_temperature",
        "node_id": "node_root",
        "field": field,
        "value_mode": "scalar",
        "timestep_indices": [0, 1],
        "samples": samples,
        "aggregate": service_module._aggregate_measurement_samples(samples),
    }
    nodes = {
        "node_root": {
            "output": {
                "arrays": [field],
            }
        }
    }
    return measurement, nodes


def test_measurement_samples_bind_exactly_to_discovery_time_axis() -> None:
    measurement, nodes = _http_measurement_fixture()
    service_http_module._validate_measurements(
        [measurement],
        nodes,
        [0.0, 1.0],
    )

    wrong_value = copy.deepcopy(measurement)
    wrong_value["samples"][1]["timestep_value"] = 99.0
    with pytest.raises(RuntimeError, match="sample values"):
        service_http_module._validate_measurements(
            [wrong_value],
            nodes,
            [0.0, 1.0],
        )

    wrong_count = copy.deepcopy(measurement)
    wrong_count["samples"].pop()
    with pytest.raises(RuntimeError, match="measurement values"):
        service_http_module._validate_measurements(
            [wrong_count],
            nodes,
            [0.0, 1.0],
        )

    wrong_index = copy.deepcopy(measurement)
    wrong_index["timestep_indices"][1] = 2
    wrong_index["samples"][1]["timestep_index"] = 2
    with pytest.raises(RuntimeError, match="measurement values"):
        service_http_module._validate_measurements(
            [wrong_index],
            nodes,
            [0.0, 1.0],
        )


def test_static_measurement_requires_only_index_zero_and_null_time() -> None:
    measurement, nodes = _http_measurement_fixture()
    measurement["timestep_indices"] = [0]
    measurement["samples"] = [measurement["samples"][0]]
    measurement["samples"][0]["timestep_value"] = None
    measurement["aggregate"] = service_module._aggregate_measurement_samples(
        measurement["samples"]
    )
    service_http_module._validate_measurements([measurement], nodes, [])

    wrong_value = copy.deepcopy(measurement)
    wrong_value["samples"][0]["timestep_value"] = 0.0
    with pytest.raises(RuntimeError, match="sample values"):
        service_http_module._validate_measurements([wrong_value], nodes, [])

    wrong_index = copy.deepcopy(measurement)
    wrong_index["timestep_indices"] = [1]
    wrong_index["samples"][0]["timestep_index"] = 1
    with pytest.raises(RuntimeError, match="measurement values"):
        service_http_module._validate_measurements([wrong_index], nodes, [])


def _artifact_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[Path, Path]:
    sidecar = (tmp_path / "artifacts.jsonl").resolve()
    output = (tmp_path / "frame.png").resolve()
    monkeypatch.setenv("JARVIS_ARTIFACT_PATH", str(sidecar))
    monkeypatch.setenv("JARVIS_EXECUTION_ID", "exec-test")
    monkeypatch.setenv("JARVIS_PACKAGE_NAME", "paraview")
    monkeypatch.setenv("JARVIS_PACKAGE_ID", "builtin.paraview")
    return sidecar, output


def _event(
    *,
    artifact_id: str,
    sequence: int,
    output: Path,
    payload: bytes = _PNG,
) -> dict[str, Any]:
    return {
        "schema_version": "jarvis.artifact.v1",
        "package_name": "paraview",
        "package_id": "builtin.paraview",
        "execution_id": "exec-test",
        "artifact_id": artifact_id,
        "logical_name": output.name,
        "kind": "image",
        "role": "output",
        "structure": "file",
        "ownership": "shared",
        "state": "finalized",
        "location": {"kind": "cluster_path", "value": "/cluster/" + output.name},
        "media_type": "image/png",
        "format": "png",
        "size_bytes": len(payload),
        "checksum": "sha256:" + hashlib.sha256(payload).hexdigest(),
        "message": "ParaView service exported an image",
        "revision": 1,
        "sequence": sequence,
        "observed_at_epoch": 1.0 + sequence,
        "metadata": {
            "application": "paraview",
            "service_instance_id": "srv-test",
            "command_id": "cmd-" + artifact_id,
            "generation_stage": "final",
            "representation_ids": ["rep_root"],
            "scene_digest": "sha256:" + "0" * 64,
        },
    }


def test_artifact_identity_mismatch_rolls_back_without_publication(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sidecar, output = _artifact_environment(monkeypatch, tmp_path)
    backend, _simple, _source = _representation_backend()
    backend.execution_id = "exec-test"
    backend.package_name = "paraview"
    backend.package_id = "builtin.paraview"
    checkpoint = backend.begin_command()
    scene_digest = backend._scene_digest()
    event = _event(artifact_id="art_identity", sequence=1, output=output)
    event["execution_id"] = "exec-other"
    event["metadata"]["command_id"] = "cmd-art_identity"
    event["metadata"]["scene_digest"] = scene_digest
    staged_path = tmp_path / ".paraview-artifact.identity.tmp.png"
    staged_path.write_bytes(_PNG)
    backend._artifacts = [event]
    backend._staged_artifacts = {
        event["artifact_id"]: {
            "event": event,
            "staged_path": staged_path,
            "output_path": output,
            "command_id": "cmd-art_identity",
            "representation_ids": ["rep_root"],
            "scene_digest": scene_digest,
        }
    }

    with pytest.raises(RuntimeError, match="artifact identity"):
        backend.commit_command(checkpoint)
    backend.rollback_command(checkpoint)

    assert not staged_path.exists()
    assert not output.exists()
    assert not sidecar.exists()
    assert backend._artifacts == []


def test_startup_resumes_exact_marker_stage_without_leaking_temp(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sidecar, output = _artifact_environment(monkeypatch, tmp_path)
    event = _event(artifact_id="art_marker_only", sequence=1, output=output)
    staged = tmp_path / ".paraview-artifact.marker-only.tmp.png"
    staged.write_bytes(_PNG)
    marker_path = service_module._artifact_marker_path(sidecar, event["artifact_id"])
    marker = service_module._artifact_marker_record(
        event,
        output,
        staged,
        ledger_prefix=b"",
        previous_sequence=0,
    )
    service_module._create_artifact_marker(marker_path, marker)

    service_module._recover_artifact_transactions()

    assert not marker_path.exists()
    assert not staged.exists()
    assert output.read_bytes() == _PNG
    assert service_module._read_artifact_lines(sidecar) == [event]


def test_partial_ledger_recovery_preserves_prior_event_bytes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sidecar, output = _artifact_environment(monkeypatch, tmp_path)
    prior_output = tmp_path / "prior.png"
    prior = _event(artifact_id="art_prior", sequence=1, output=prior_output)
    prefix = service_module._artifact_event_payload(prior)
    sidecar.write_bytes(prefix)
    event = _event(artifact_id="art_partial", sequence=2, output=output)
    staged = tmp_path / ".paraview-artifact.partial.tmp.png"
    marker_path = service_module._artifact_marker_path(sidecar, event["artifact_id"])
    marker = service_module._artifact_marker_record(
        event,
        output,
        staged,
        ledger_prefix=prefix,
        previous_sequence=1,
    )
    service_module._create_artifact_marker(marker_path, marker)
    output.write_bytes(_PNG)
    partial = service_module._artifact_event_payload(event)[:31]
    with sidecar.open("ab") as stream:
        stream.write(partial)
        stream.flush()
        os.fsync(stream.fileno())

    service_module._recover_artifact_transactions()

    published = sidecar.read_bytes()
    assert published[: len(prefix)] == prefix
    assert service_module._read_artifact_lines(sidecar) == [prior, event]
    assert not marker_path.exists()


def test_event_appended_marker_cleanup_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sidecar, output = _artifact_environment(monkeypatch, tmp_path)
    event = _event(artifact_id="art_appended", sequence=1, output=output)
    staged = tmp_path / ".paraview-artifact.appended.tmp.png"
    marker_path = service_module._artifact_marker_path(sidecar, event["artifact_id"])
    marker = service_module._artifact_marker_record(
        event,
        output,
        staged,
        ledger_prefix=b"",
        previous_sequence=0,
    )
    service_module._create_artifact_marker(marker_path, marker)
    output.write_bytes(_PNG)
    sidecar.write_bytes(service_module._artifact_event_payload(event))

    service_module._recover_artifact_transactions()
    service_module._recover_artifact_transactions()

    assert service_module._read_artifact_lines(sidecar) == [event]
    assert not marker_path.exists()


def test_raw_matching_output_without_marker_is_a_conflict(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sidecar, output = _artifact_environment(monkeypatch, tmp_path)
    event = _event(artifact_id="art_raw", sequence=1, output=output)
    staged = tmp_path / ".staged.png"
    staged.write_bytes(_PNG)
    output.write_bytes(_PNG)

    with pytest.raises(CommandError) as captured:
        service_module._commit_staged_artifacts(
            {
                event["artifact_id"]: {
                    "event": event,
                    "staged_path": staged,
                    "output_path": output,
                }
            }
        )

    assert captured.value.code == "artifact_exists"
    assert not sidecar.exists()
    assert not list(tmp_path.glob(".artifacts.jsonl.paraview-transaction-*.json"))


def test_post_execute_state_failure_publishes_no_png_or_ledger(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Artifact publication happens only after candidate state validation."""
    sidecar, output = _artifact_environment(monkeypatch, tmp_path)
    base = _transport_backend()

    class ArtifactBackend:
        def __init__(self) -> None:
            self.artifacts: list[dict[str, Any]] = []
            self.staged: dict[str, dict[str, Any]] = {}

        def dataset_state(self) -> dict[str, Any]:
            return base.dataset_state()

        def pipeline_state(self) -> dict[str, Any]:
            state = base.pipeline_state()
            state["artifacts"] = list(self.artifacts)
            return state

        def render_png(self) -> bytes:
            return _PNG

        def begin_command(self) -> list[dict[str, Any]]:
            return list(self.artifacts)

        def execute(
            self,
            operation: str,
            arguments: Mapping[str, Any],
            command_id: str,
        ) -> dict[str, Any]:
            del operation, arguments, command_id
            event = _event(artifact_id="art_state_failure", sequence=1, output=output)
            staged_path = tmp_path / ".candidate.png"
            staged_path.write_bytes(_PNG)
            self.staged = {
                event["artifact_id"]: {
                    "event": event,
                    "staged_path": staged_path,
                    "output_path": output,
                }
            }
            self.artifacts.append(event)
            return {"artifact": event}

        def commit_command(self, checkpoint: object) -> None:
            del checkpoint
            service_module._commit_staged_artifacts(self.staged)
            self.staged = {}

        def rollback_command(self, checkpoint: object) -> None:
            for staged in self.staged.values():
                cast(Path, staged["staged_path"]).unlink(missing_ok=True)
            self.staged = {}
            self.artifacts = cast(list[dict[str, Any]], checkpoint)

    backend = ArtifactBackend()
    controller = ServiceStateController(
        backend=backend,
        execution_id="exec-test",
        package_name="builtin.paraview",
        package_id="viewer",
        service_instance_id="srv-test",
    )
    monkeypatch.setattr(service_http_module, "MAX_STATE_BYTES", 1)

    with pytest.raises(CommandError) as captured:
        controller.command(
            {
                "schema_version": "jarvis.paraview.command.v2",
                "command_id": "export-state-failure",
                "operation": "export_artifact",
                "expected_revision": 1,
                "arguments": {"filename": "frame.png"},
            }
        )
    assert captured.value.code == "operation_failed"
    assert str(captured.value) == "ParaView operation failed"

    assert controller.state()["revision"] == 1
    assert backend.artifacts == []
    assert not output.exists()
    assert not sidecar.exists()
    assert not list(tmp_path.glob(".artifacts.jsonl.paraview-transaction-*.json"))


def test_post_commit_proxy_delete_failure_is_bounded_and_retryable() -> None:
    """Cleanup failure cannot make an already committed command ambiguous."""
    proxy = object()
    backend = cast(Any, object.__new__(service_module.ParaViewBackend))
    backend._transaction_open = True
    backend._staged_artifacts = {}
    backend._pending_deletes = [proxy]
    backend._retired_proxies = []
    backend.simple = SimpleNamespace(
        Delete=lambda _proxy: (_ for _ in ()).throw(RuntimeError("delete failed"))
    )

    backend.commit_command({})

    assert backend._transaction_open is False
    assert backend._pending_deletes == []
    assert backend._retired_proxies == [proxy]
    backend.simple = SimpleNamespace(Delete=lambda _proxy: None)
    backend._drain_retired_proxies()
    assert backend._retired_proxies == []


def test_scalar_bar_delete_failure_remains_in_bounded_retry_backlog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed scalar-bar unregister is remembered and retried after commit."""
    backend, simple, _source = _representation_backend()
    assert simple.proxy_manager is not None
    created = backend.execute(
        "set_representation",
        {
            "representation_id": None,
            "node_id": "node_root",
            "type": "surface",
            "visible": True,
            "opacity": 1.0,
            "point_size_px": None,
            "color": _field_color(preset=None),
        },
        "scalar-delete-create",
    )["representation"]
    representation_id = created["representation_id"]
    scalar_bar = backend._representation_transfer_proxies[representation_id][0]
    original_delete = simple.Delete

    def fail_scalar_bar(proxy: object) -> None:
        if proxy is scalar_bar:
            raise RuntimeError("scalar bar delete failed")
        original_delete(proxy)

    monkeypatch.setattr(simple, "Delete", fail_scalar_bar)
    backend.execute(
        "set_representation",
        {
            "representation_id": representation_id,
            "node_id": "node_root",
            "type": "surface",
            "visible": True,
            "opacity": 1.0,
            "point_size_px": None,
            "color": {"mode": "solid", "rgb": [0.2, 0.4, 0.6]},
        },
        "scalar-delete-solid",
    )

    assert backend._retired_proxies == [scalar_bar]
    assert scalar_bar in simple.proxy_manager.proxies.values()
    monkeypatch.setattr(simple, "Delete", original_delete)
    backend._drain_retired_proxies()
    assert backend._retired_proxies == []
    assert scalar_bar not in simple.proxy_manager.proxies.values()
