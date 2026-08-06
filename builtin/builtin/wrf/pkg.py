"""Maintained WRF package with legacy and supplied-study profiles."""

from __future__ import annotations

from typing import Any

from builtin.wrf.legacy import WrfLegacy
from builtin.wrf.tropical_cyclone import WrfTropicalCyclone


class Wrf(WrfTropicalCyclone, WrfLegacy):
    """Run either the legacy WRF launcher or a digest-bound TC comparison."""

    def _init(self) -> None:
        WrfTropicalCyclone._init(self)

    def _configure_menu(self) -> list[dict[str, Any]]:
        """Expose one additive menu without duplicate process controls."""

        merged: list[dict[str, Any]] = []
        names: set[str] = set()
        for item in (
            *WrfTropicalCyclone._configure_menu(self),
            *WrfLegacy._configure_menu(self),
        ):
            name = str(item["name"])
            if name not in names:
                merged.append(item)
                names.add(name)
        return merged

    def _configure(self, **kwargs: Any) -> None:
        """Select the supplied-input profile only when a bundle is explicit."""

        if kwargs.get("input_bundle") or self.config.get("input_bundle"):
            WrfTropicalCyclone._configure(self, **kwargs)
            return
        WrfLegacy._configure(self, **kwargs)

    def start(self) -> None:
        """Launch the explicitly configured WRF profile."""

        if self.config.get("input_bundle"):
            WrfTropicalCyclone.start(self)
            return
        WrfLegacy.start(self)

    def stop(self) -> None:
        """Stop the selected profile when it owns a long-running process."""

        WrfLegacy.stop(self)

    def clean(self) -> None:
        """Preserve supplied-study results and apply legacy cleanup semantics."""

        if not self.config.get("input_bundle"):
            WrfLegacy.clean(self)
