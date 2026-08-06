"""Maintained OpenFOAM package with legacy and supplied-study profiles."""

from __future__ import annotations

from typing import Any

from builtin.openfoam.airfoil import OpenfoamAirfoil
from builtin.openfoam.legacy import OpenfoamLegacy


class Openfoam(OpenfoamAirfoil, OpenfoamLegacy):
    """Run either the legacy OpenFOAM launcher or a digest-bound airfoil study."""

    def _init(self) -> None:
        OpenfoamAirfoil._init(self)

    def _configure_menu(self) -> list[dict[str, Any]]:
        """Expose one additive menu without duplicate process controls."""

        merged: list[dict[str, Any]] = []
        names: set[str] = set()
        for item in (
            *OpenfoamAirfoil._configure_menu(self),
            *OpenfoamLegacy._configure_menu(self),
        ):
            name = str(item["name"])
            if name not in names:
                merged.append(item)
                names.add(name)
        return merged

    # Pkg leaves these override points untyped, so Pyright infers None-only returns.
    def _build_phase(  # pyright: ignore[reportIncompatibleMethodOverride]
        self,
    ) -> tuple[str, str] | None:
        return OpenfoamLegacy._build_phase(self)

    def _build_deploy_phase(  # pyright: ignore[reportIncompatibleMethodOverride]
        self,
    ) -> tuple[str, str] | None:
        return OpenfoamLegacy._build_deploy_phase(self)

    def _configure(self, **kwargs: Any) -> None:
        """Select the supplied-input profile only when a bundle is explicit."""

        if kwargs.get("input_bundle") or self.config.get("input_bundle"):
            OpenfoamAirfoil._configure(self, **kwargs)
            return
        OpenfoamLegacy._configure(self, **kwargs)

    def start(self) -> None:
        """Launch the explicitly configured OpenFOAM profile."""

        if self.config.get("input_bundle"):
            OpenfoamAirfoil.start(self)
            return
        OpenfoamLegacy.start(self)

    def stop(self) -> None:
        """Stop the selected profile when it owns a long-running process."""

        OpenfoamLegacy.stop(self)

    def clean(self) -> None:
        """Preserve supplied-study results and apply legacy cleanup semantics."""

        if not self.config.get("input_bundle"):
            OpenfoamLegacy.clean(self)
