"""Tests for generic package configuration-input materialization."""

from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path
from typing import Any

import pytest

from jarvis_cd.configuration_input import (
    configuration_input_materialization_matches,
    materialize_configuration_inputs,
)
from jarvis_cd.core.config import Jarvis
from jarvis_cd.core.pipeline import Pipeline


_INPUT_BINDING = {
    "schema_version": "jarvis.configuration-input-binding.v1",
    "kind": "local_file",
    "structure": "regular_file",
}


class _SuccessfulOperation:
    """Capture a package operation without invoking LAMMPS or a shell."""

    commands: list[str] = []

    def __init__(self, command: str, _exec_info: Any) -> None:
        self.command = command
        self.exit_code = {"localhost": 0}

    def run(self) -> "_SuccessfulOperation":
        """Record the operation and report success."""
        self.commands.append(self.command)
        return self


def test_materialization_is_content_addressed_and_source_independent(
    tmp_path: Path,
) -> None:
    """A configured input remains stable after its caller-owned source changes."""
    source = tmp_path / "caller" / "in.research"
    source.parent.mkdir()
    original = b"units lj\nrun 250\n"
    source.write_bytes(original)
    shared_dir = (tmp_path / "pipeline-shared" / "simulation").resolve()
    shared_dir.mkdir(parents=True)
    menu = [
        {
            "name": "script",
            "type": str,
            "default": "",
            "input_binding": dict(_INPUT_BINDING),
        }
    ]

    configured = materialize_configuration_inputs(
        menu=menu,
        config={"script": str(source)},
        shared_dir=shared_dir,
    )

    target = Path(configured["script"])
    assert target.is_relative_to(shared_dir / "configuration-inputs" / "script")
    assert target.read_bytes() == original
    assert target.name.endswith(".research")

    source.write_text("units metal\nrun 999\n", encoding="utf-8")
    source.unlink()

    replayed = materialize_configuration_inputs(
        menu=menu,
        config=configured,
        shared_dir=shared_dir,
    )
    assert replayed == configured
    assert target.read_bytes() == original


def test_undeclared_path_setting_is_not_materialized(tmp_path: Path) -> None:
    """JARVIS never infers local-file authority from a setting name or value."""
    source = tmp_path / "script.py"
    source.write_text("print('not declared')\n", encoding="utf-8")
    shared_dir = (tmp_path / "shared").resolve()
    shared_dir.mkdir()

    configured = materialize_configuration_inputs(
        menu=[{"name": "script", "type": str, "default": ""}],
        config={"script": str(source)},
        shared_dir=shared_dir,
    )

    assert configured == {"script": str(source)}
    assert not (shared_dir / "configuration-inputs").exists()


def test_materialization_match_requires_the_exact_owned_content_address(
    tmp_path: Path,
) -> None:
    """Transport adapters can distinguish a valid rewrite from path substitution."""
    source = tmp_path / "relay-staging" / "in.research"
    source.parent.mkdir()
    source.write_text("units lj\nrun 25\n", encoding="utf-8")
    shared_dir = (tmp_path / "shared").resolve()
    shared_dir.mkdir()
    menu = [
        {
            "name": "script",
            "type": str,
            "default": "",
            "input_binding": dict(_INPUT_BINDING),
        }
    ]

    configured = materialize_configuration_inputs(
        menu=menu,
        config={"script": str(source)},
        shared_dir=shared_dir,
    )
    target = Path(configured["script"])

    assert configuration_input_materialization_matches(
        menu=menu,
        parameter="script",
        requested=source,
        materialized=target,
        shared_dir=shared_dir,
    )

    substituted = target.with_name(f"substituted{target.suffix}")
    substituted.write_bytes(target.read_bytes())
    assert not configuration_input_materialization_matches(
        menu=menu,
        parameter="script",
        requested=source,
        materialized=substituted,
        shared_dir=shared_dir,
    )


def test_materialized_input_rejects_external_hardlink_alias(tmp_path: Path) -> None:
    """Package-owned bytes cannot remain mutable through another hardlink."""
    source = tmp_path / "relay-staging" / "in.research"
    source.parent.mkdir()
    source.write_text("units lj\nrun 25\n", encoding="utf-8")
    shared_dir = (tmp_path / "shared").resolve()
    shared_dir.mkdir()
    menu = [
        {
            "name": "script",
            "type": str,
            "default": "",
            "input_binding": dict(_INPUT_BINDING),
        }
    ]

    configured = materialize_configuration_inputs(
        menu=menu,
        config={"script": str(source)},
        shared_dir=shared_dir,
    )
    target = Path(configured["script"])
    alias = tmp_path / "external-alias"
    os.link(target, alias)

    assert not configuration_input_materialization_matches(
        menu=menu,
        parameter="script",
        requested=source,
        materialized=target,
        shared_dir=shared_dir,
    )


def test_malformed_declared_input_binding_fails_closed(tmp_path: Path) -> None:
    """A near-match descriptor cannot silently acquire local-file authority."""
    source = tmp_path / "in.research"
    source.write_text("run 1\n", encoding="utf-8")

    with pytest.raises(ValueError, match="fields must be exactly"):
        materialize_configuration_inputs(
            menu=[
                {
                    "name": "script",
                    "input_binding": {
                        **_INPUT_BINDING,
                        "unreviewed_extension": True,
                    },
                }
            ],
            config={"script": str(source)},
            shared_dir=(tmp_path / "shared").resolve(),
        )


def test_lammps_pipeline_runs_from_materialized_input_after_source_deletion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A later package launch uses pipeline-owned bytes, not relay staging."""
    previous = Jarvis._instance
    Jarvis._instance = None
    try:
        jarvis = Jarvis(jarvis_root=str(tmp_path / "jarvis"))
        jarvis.initialize(
            config_dir=str(tmp_path / "config"),
            private_dir=str(tmp_path / "private"),
            shared_dir=str(tmp_path / "shared"),
        )
        pipeline = Pipeline()
        pipeline.create("materialized-lammps-input")
        source = tmp_path / "relay-staging" / "in.research"
        source.parent.mkdir()
        original = "units lj\natom_style atomic\nrun 25\n"
        source.write_text(original, encoding="utf-8")
        pipeline.append(
            "builtin.lammps",
            package_alias="simulation",
            config_args=[f"script={source}", "out=."],
        )
        pipeline.configure_package(
            "simulation",
            [f"script={source}", "out=."],
        )

        persisted = Pipeline("materialized-lammps-input")
        definition = persisted.packages[0]
        target = Path(definition["config"]["script"])
        assert target != source
        assert target.read_text(encoding="utf-8") == original

        source.write_text("clear\n", encoding="utf-8")
        source.unlink()
        reloaded = Pipeline("materialized-lammps-input")
        definition = reloaded.packages[0]
        package = reloaded._load_package_instance(definition, reloaded.env)
        lammps_module = sys.modules[type(package).__module__]
        assert Path(package.config["script"]) == target
        assert target.read_text(encoding="utf-8") == original

        _SuccessfulOperation.commands = []
        monkeypatch.setattr(lammps_module, "Exec", _SuccessfulOperation)
        monkeypatch.setattr(lammps_module, "Mkdir", _SuccessfulOperation)
        package.start()

        launch = next(
            command
            for command in _SuccessfulOperation.commands
            if command.startswith("lmp ")
        )
        assert f"-in {shlex.quote(str(target))}" in launch
        assert str(source) not in launch
    finally:
        Jarvis._instance = previous
