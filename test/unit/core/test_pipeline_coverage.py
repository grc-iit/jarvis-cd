"""
Unit tests for uncovered paths in pipeline.py.

Covers:
- pipeline.destroy()
- pipeline.rm()
- pipeline.status()
- pipeline._validate_unique_ids()
- pipeline._get_package_default_config()
- pipeline.configure_package()
- pipeline save/load of container fields
- pipeline._generate_pipeline_container_yaml()
- pipeline._validate_required_config()
"""

import os
import sys
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from jarvis_cd.core.config import Jarvis
from jarvis_cd.core.pipeline import Pipeline
from jarvis_cd.shell.exec_info import ExecType
from jarvis_cd.util.hostfile import Hostfile


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def initialize_jarvis_for_test(config_dir, private_dir, shared_dir):
    jarvis = Jarvis.get_instance()
    saved_config = None
    if jarvis.config_file.exists():
        with open(jarvis.config_file, "r") as f:
            saved_config = yaml.safe_load(f)
    jarvis.initialize(config_dir, private_dir, shared_dir, force=False)
    return jarvis, saved_config


class TestPipelineCoverage(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="jarvis_test_pipeline_coverage_")
        self.config_dir = os.path.join(self.test_dir, "config")
        self.private_dir = os.path.join(self.test_dir, "private")
        self.shared_dir = os.path.join(self.test_dir, "shared")
        for d in [self.config_dir, self.private_dir, self.shared_dir]:
            os.makedirs(d, exist_ok=True)

        self.jarvis, self._saved_config = initialize_jarvis_for_test(
            self.config_dir, self.private_dir, self.shared_dir
        )

    def tearDown(self):
        if self._saved_config:
            jarvis = Jarvis.get_instance()
            jarvis.save_config(self._saved_config)
            jarvis.config_dir = self._saved_config.get("config_dir", jarvis.config_dir)
            jarvis.private_dir = self._saved_config.get(
                "private_dir", jarvis.private_dir
            )
            jarvis.shared_dir = self._saved_config.get("shared_dir", jarvis.shared_dir)
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_pipeline(self, name):
        pipeline = Pipeline()
        pipeline.create(name)
        return pipeline

    def _make_ior_pkg_def(self, pipeline_name):
        return {
            "pkg_type": "builtin.ior",
            "pkg_id": "ior",
            "pkg_name": "ior",
            "global_id": f"{pipeline_name}.ior",
            "config": {
                "nprocs": 2,
                "ppn": 2,
                "block": "32m",
                "xfer": "1m",
                "api": "posix",
                "out": os.path.join(self.shared_dir, "ior.bin"),
                "write": True,
                "read": False,
                "fpp": False,
                "reps": 1,
                "direct": False,
                "interceptors": [],
            },
        }

    # ------------------------------------------------------------------
    # destroy()
    # ------------------------------------------------------------------

    def test_destroy_removes_pipeline_dirs(self):
        """destroy() should remove config/shared/private dirs for the pipeline."""
        pipeline = self._make_pipeline("destroy_test")

        config_dir = self.jarvis.get_pipeline_dir("destroy_test")
        shared_dir = self.jarvis.get_pipeline_shared_dir("destroy_test")
        private_dir = self.jarvis.get_pipeline_private_dir("destroy_test")

        # All three directories must exist before destroy
        self.assertTrue(config_dir.exists(), "config dir should exist before destroy")
        self.assertTrue(shared_dir.exists(), "shared dir should exist before destroy")
        self.assertTrue(private_dir.exists(), "private dir should exist before destroy")

        pipeline.destroy()

        self.assertFalse(config_dir.exists(), "config dir should be gone after destroy")

    # ------------------------------------------------------------------
    # rm()
    # ------------------------------------------------------------------

    def test_rm_package_removes_from_list(self):
        """rm() removes the named package from pipeline.packages."""
        pipeline = self._make_pipeline("rm_test")
        pkg_def = self._make_ior_pkg_def("rm_test")
        pipeline.packages.append(pkg_def)
        pipeline._propagate_deploy_mode()
        pipeline.save()

        self.assertEqual(len(pipeline.packages), 1)
        pipeline.rm("ior")
        self.assertEqual(len(pipeline.packages), 0)

    def test_rm_nonexistent_package_no_crash(self):
        """rm() on a non-existent package ID must not raise."""
        pipeline = self._make_pipeline("rm_noexist")
        # Should print a message but not raise
        try:
            pipeline.rm("nonexistent")
        except Exception as e:
            self.fail(f"rm() raised unexpectedly: {e}")

    # ------------------------------------------------------------------
    # status()
    # ------------------------------------------------------------------

    def test_status_returns_string(self):
        """status() must return a string (even with no packages)."""
        pipeline = self._make_pipeline("status_test")
        result = pipeline.status()
        self.assertIsInstance(result, str)

    def test_status_no_pipeline_name(self):
        """status() on a bare Pipeline() with no name returns a string."""
        pipeline = Pipeline.__new__(Pipeline)
        pipeline.jarvis = Jarvis.get_instance()
        pipeline.name = None
        pipeline.packages = []
        pipeline.interceptors = {}
        pipeline.env = {}
        result = pipeline.status()
        self.assertIsInstance(result, str)
        self.assertIn("No pipeline", result)

    # ------------------------------------------------------------------
    # _validate_unique_ids()
    # ------------------------------------------------------------------

    def test_validate_unique_ids_passes_for_unique(self):
        """_validate_unique_ids() does not raise when IDs are distinct."""
        pipeline = self._make_pipeline("uid_pass")
        pkg_def = self._make_ior_pkg_def("uid_pass")
        pipeline.packages.append(pkg_def)
        # interceptors dict is empty — no conflict
        try:
            pipeline._validate_unique_ids()
        except Exception as e:
            self.fail(f"_validate_unique_ids raised unexpectedly: {e}")

    def test_validate_unique_ids_raises_for_duplicate(self):
        """_validate_unique_ids() raises when a package ID collides with an interceptor ID."""
        pipeline = self._make_pipeline("uid_fail")
        pkg_def = self._make_ior_pkg_def("uid_fail")
        pipeline.packages.append(pkg_def)
        # Add a fake interceptor with the same id 'ior'
        pipeline.interceptors["ior"] = {
            "pkg_type": "builtin.ior",
            "pkg_id": "ior",
            "pkg_name": "ior",
            "global_id": "uid_fail.ior",
            "config": {},
        }
        with self.assertRaises((ValueError, Exception)):
            pipeline._validate_unique_ids()

    # ------------------------------------------------------------------
    # _get_package_default_config()
    # ------------------------------------------------------------------

    def test_get_package_default_config_returns_dict(self):
        """_get_package_default_config('builtin.ior') must return a non-empty dict."""
        pipeline = self._make_pipeline("defcfg_test")
        result = pipeline._get_package_default_config("builtin.ior")
        self.assertIsInstance(result, dict)
        self.assertGreater(len(result), 0)

    def test_append_rejects_unknown_package_setting_atomically(self):
        """Invalid package settings cannot leave a partially appended step."""
        pipeline = self._make_pipeline("append_unknown")

        with self.assertRaisesRegex(ValueError, "Unknown argument"):
            pipeline.append(
                "builtin.echo",
                package_alias="echo",
                config_args=["message=not-supported"],
            )

        self.assertEqual(pipeline.packages, [])
        self.assertEqual(Pipeline("append_unknown").packages, [])

    def test_append_persists_valid_package_setting(self):
        """Package-owned settings are parsed and saved with an appended step."""
        pipeline = self._make_pipeline("append_valid")

        pipeline.append(
            "builtin.echo",
            package_alias="echo",
            config_args=["retry_count=4"],
        )

        reloaded = Pipeline("append_valid")
        self.assertEqual(reloaded.packages[0]["config"]["retry_count"], 4)

    # ------------------------------------------------------------------
    # configure_package()
    # ------------------------------------------------------------------

    def test_configure_package_updates_config(self):
        """configure_package() updates the config field for the named package."""
        pipeline = self._make_pipeline("cfgpkg_test")
        pkg_def = self._make_ior_pkg_def("cfgpkg_test")
        pipeline.packages.append(pkg_def)
        pipeline._propagate_deploy_mode()
        pipeline.save()

        # nprocs starts at 2; reconfigure to 8
        pipeline.configure_package("ior", ["--nprocs=8"])

        # Reload pipeline and confirm nprocs was persisted
        pipeline2 = Pipeline("cfgpkg_test")
        self.assertEqual(pipeline2.packages[0]["config"].get("nprocs"), 8)

    def test_configure_package_can_reset_a_value_to_its_default(self):
        """Explicit default values replace an earlier non-default value."""
        pipeline = self._make_pipeline("cfgpkg_reset_default")
        pkg_def = self._make_ior_pkg_def("cfgpkg_reset_default")
        pipeline.packages.append(pkg_def)
        pipeline._propagate_deploy_mode()
        pipeline.save()

        pipeline.configure_package("ior", ["nprocs=8"])
        pipeline.configure_package("ior", ["nprocs=1"])

        reloaded = Pipeline("cfgpkg_reset_default")
        self.assertEqual(reloaded.packages[0]["config"].get("nprocs"), 1)

    def test_configure_package_accepts_spaced_boolean_values(self):
        """Long boolean options retain the parser's documented value form."""
        pipeline = self._make_pipeline("cfgpkg_spaced_bool")
        pkg_def = self._make_ior_pkg_def("cfgpkg_spaced_bool")
        pipeline.packages.append(pkg_def)
        pipeline._propagate_deploy_mode()
        pipeline.save()

        pipeline.configure_package("ior", ["--container_cache", "false"])

        reloaded = Pipeline("cfgpkg_spaced_bool")
        self.assertIs(reloaded.packages[0]["config"].get("container_cache"), False)

    def test_configure_package_rejects_unknown_key_without_persisting(self):
        """Unknown key=value arguments fail closed and leave YAML unchanged."""
        pipeline = self._make_pipeline("cfgpkg_unknown")
        pkg_def = self._make_ior_pkg_def("cfgpkg_unknown")
        pipeline.packages.append(pkg_def)
        pipeline._propagate_deploy_mode()
        pipeline.save()
        pipeline_path = self.jarvis.get_pipeline_dir("cfgpkg_unknown") / "pipeline.yaml"
        original = pipeline_path.read_bytes()

        with self.assertRaisesRegex(ValueError, "Unknown argument"):
            pipeline.configure_package("ior", ["message=not-supported"])

        self.assertEqual(pipeline_path.read_bytes(), original)
        self.assertNotIn("message", pipeline.packages[0]["config"])

    def test_configure_package_failure_is_not_persisted(self):
        """A package configuration failure cannot mutate durable pipeline state."""
        from jarvis_cd.util import PkgArgParse

        class FailingPackage:
            def __init__(self, config):
                self.config = config

            @staticmethod
            def configure_menu():
                return [{"name": "count", "type": int, "default": 1}]

            def get_argparse(self):
                return PkgArgParse("failing", self.configure_menu())

            def configure(self, **kwargs):
                self.config["count"] = kwargs["count"]
                self.config["leaked_default"] = True
                raise RuntimeError(f"rejected count {kwargs['count']}")

        pipeline = self._make_pipeline("cfgpkg_failure")
        pipeline.packages.append(
            {
                "pkg_type": "builtin.test_pkg",
                "pkg_id": "failing",
                "pkg_name": "failing",
                "global_id": "cfgpkg_failure.failing",
                "config": {"count": 1},
            }
        )

        with (
            patch.object(
                pipeline,
                "_load_package_instance",
                return_value=FailingPackage(pipeline.packages[0]["config"]),
            ),
            patch.object(pipeline, "save") as save,
            self.assertRaisesRegex(ValueError, "rejected count 2"),
        ):
            pipeline.configure_package("failing", ["count=2"])

        save.assert_not_called()
        self.assertEqual(pipeline.packages[0]["config"], {"count": 1})

    def test_configure_package_persists_package_derived_values(self):
        """Successful package-owned configuration commits its derived settings."""
        from jarvis_cd.util import PkgArgParse

        class DerivedPackage:
            def __init__(self):
                self.config = {}

            @staticmethod
            def configure_menu():
                return [{"name": "count", "type": int, "default": 1}]

            def get_argparse(self):
                return PkgArgParse("derived", self.configure_menu())

            def configure(self, **kwargs):
                self.config["count"] = kwargs["count"]
                self.config["derived_path"] = f"/output/{kwargs['count']}"

        pipeline = self._make_pipeline("cfgpkg_derived")
        pipeline.packages.append(
            {
                "pkg_type": "builtin.test_pkg",
                "pkg_id": "derived",
                "pkg_name": "derived",
                "global_id": "cfgpkg_derived.derived",
                "config": {"count": 1},
            }
        )

        with (
            patch.object(
                pipeline,
                "_load_package_instance",
                return_value=DerivedPackage(),
            ),
            patch.object(pipeline, "save") as save,
        ):
            pipeline.configure_package("derived", ["count=2"])

        save.assert_called_once_with()
        self.assertEqual(
            pipeline.packages[0]["config"],
            {"count": 2, "derived_path": "/output/2"},
        )

    def test_configure_localhost_does_not_require_ssh(self):
        """Local IOR setup remains usable when localhost SSH is unavailable."""
        pipeline = self._make_pipeline("cfgpkg_local")
        pkg_def = self._make_ior_pkg_def("cfgpkg_local")
        pipeline.packages.append(pkg_def)
        pipeline._propagate_deploy_mode()
        pipeline.save()

        with patch(
            "jarvis_cd.shell.ssh_exec.SshExec",
            side_effect=AssertionError("localhost SSH must not be attempted"),
        ) as ssh_exec:
            pipeline.configure_package("ior", ["--nprocs=4"])

        ssh_exec.assert_not_called()
        self.assertTrue(os.path.isdir(os.path.dirname(pkg_def["config"]["out"])))
        self.assertEqual(pkg_def["config"]["nprocs"], 4)

    def test_configure_remote_directory_uses_bounded_pssh(self):
        """Remote IOR setup retains all-host PSSH with a hard time bound."""
        pipeline = self._make_pipeline("cfgpkg_remote")
        pipeline.hostfile = Hostfile(hosts=["node-a"], find_ips=False)
        pkg_def = self._make_ior_pkg_def("cfgpkg_remote")
        pipeline.packages.append(pkg_def)
        pipeline._propagate_deploy_mode()
        pipeline.save()

        with patch("builtin.ior.pkg.Mkdir") as mkdir:
            pipeline.configure_package("ior", ["--nprocs=4"])

        mkdir.assert_called_once()
        mkdir.return_value.run.assert_called_once_with()
        exec_info = mkdir.call_args.args[1]
        self.assertEqual(exec_info.exec_type, ExecType.PSSH)
        self.assertEqual(exec_info.hostfile.hosts, ["node-a"])
        self.assertEqual(exec_info.timeout, 30)

    # ------------------------------------------------------------------
    # save / load container fields
    # ------------------------------------------------------------------

    def test_save_load_container_engine(self):
        """container_engine is persisted across save/load cycles."""
        pipeline = self._make_pipeline("ce_test")
        pipeline.container_engine = "podman"
        pipeline.save()

        pipeline2 = Pipeline("ce_test")
        self.assertEqual(pipeline2.container_engine, "podman")

    def test_save_load_base_deploy_mode(self):
        """base_deploy_mode='container' is persisted across save/load cycles."""
        pipeline = self._make_pipeline("im_test")
        pipeline.base_deploy_mode = "container"
        pipeline.save()

        pipeline2 = Pipeline("im_test")
        self.assertEqual(pipeline2.base_deploy_mode, "container")

    def test_save_load_container_base(self):
        """container_base is persisted across save/load cycles."""
        pipeline = self._make_pipeline("cb_test")
        pipeline.container_base = "ubuntu:22.04"
        pipeline.save()

        pipeline2 = Pipeline("cb_test")
        self.assertEqual(pipeline2.container_base, "ubuntu:22.04")

    def test_save_load_deploy_image(self):
        """container_image (deploy image) is persisted across save/load cycles."""
        pipeline = self._make_pipeline("di_test")
        pipeline.container_image = "myimg:latest"
        pipeline.save()

        pipeline2 = Pipeline("di_test")
        self.assertEqual(pipeline2.container_image, "myimg:latest")

    def test_container_stop_rejects_local_nonzero_exit(self):
        """A local container teardown cannot report success after exit failure."""
        pipeline = self._make_pipeline("container_stop_local_failure")
        pipeline.container_engine = "docker"
        pipeline.packages = []
        pipeline.hostfile = Hostfile(hosts=["localhost"], find_ips=False)

        result = SimpleNamespace(
            exit_code={"localhost": 7},
            stderr={"localhost": "compose teardown failed"},
        )
        with (
            patch("jarvis_cd.shell.Exec") as executor,
            self.assertRaises(ExceptionGroup) as raised,
        ):
            executor.return_value.run.return_value = result
            pipeline._stop_containerized_pipeline()

        self.assertIn("localhost=exit 7", str(raised.exception.exceptions[0]))
        exec_info = executor.call_args.args[1]
        self.assertEqual(exec_info.exec_type, ExecType.LOCAL)

    def test_container_stop_rejects_one_remote_nonzero_exit(self):
        """A PSSH teardown fails when any selected host returns nonzero."""
        pipeline = self._make_pipeline("container_stop_remote_failure")
        pipeline.container_engine = "podman"
        pipeline.packages = []
        pipeline.hostfile = Hostfile(
            hosts=["node-a", "node-b"],
            find_ips=False,
        )

        result = SimpleNamespace(
            exit_code={"node-a": 0, "node-b": 9},
            stderr={"node-a": "", "node-b": "podman unavailable"},
        )
        with (
            patch("jarvis_cd.shell.Exec") as executor,
            self.assertRaises(ExceptionGroup) as raised,
        ):
            executor.return_value.run.return_value = result
            pipeline._stop_containerized_pipeline()

        self.assertIn("node-b=exit 9", str(raised.exception.exceptions[0]))
        exec_info = executor.call_args.args[1]
        self.assertEqual(exec_info.exec_type, ExecType.PSSH)
        self.assertEqual(exec_info.hostfile.hosts, ["node-a", "node-b"])

    def test_container_force_kill_aggregates_executor_failure(self):
        """Forced container cleanup exposes missing executor status."""
        pipeline = self._make_pipeline("container_kill_failure")
        pipeline.container_engine = "docker"
        pipeline.packages = []
        pipeline.hostfile = Hostfile(hosts=["localhost"], find_ips=False)

        with (
            patch("jarvis_cd.shell.Exec") as executor,
            self.assertRaises(ExceptionGroup) as raised,
        ):
            executor.return_value.run.return_value = SimpleNamespace(
                exit_code={},
                stderr={},
            )
            pipeline._kill_containerized_pipeline()

        self.assertIn("no per-host exit status", str(raised.exception.exceptions[0]))

    # ------------------------------------------------------------------
    # _generate_pipeline_container_yaml()
    # ------------------------------------------------------------------

    def test_generate_container_yaml_creates_file(self):
        """_generate_pipeline_container_yaml() writes pipeline.yaml to shared_dir."""
        pipeline = self._make_pipeline("gcy_test")
        pkg_def = self._make_ior_pkg_def("gcy_test")
        pipeline.packages.append(pkg_def)
        pipeline.save()

        yaml_path = pipeline._generate_pipeline_container_yaml()

        self.assertTrue(
            os.path.exists(str(yaml_path)), f"Expected YAML file at {yaml_path}"
        )

    def test_generate_container_yaml_has_packages(self):
        """The generated YAML file contains the pipeline packages."""
        pipeline = self._make_pipeline("gcy_pkgs_test")
        pkg_def = self._make_ior_pkg_def("gcy_pkgs_test")
        pipeline.packages.append(pkg_def)
        pipeline.save()

        yaml_path = pipeline._generate_pipeline_container_yaml()

        with open(str(yaml_path), "r") as f:
            data = yaml.safe_load(f)

        self.assertIn("pkgs", data)
        self.assertGreater(len(data["pkgs"]), 0)
        pkg_types = [p["pkg_type"] for p in data["pkgs"]]
        self.assertIn("builtin.ior", pkg_types)

    # ------------------------------------------------------------------
    # _validate_required_config()
    # ------------------------------------------------------------------

    def test_validate_required_passes_when_all_present(self):
        """_validate_required_config() does not raise when config has all fields."""
        pipeline = self._make_pipeline("vrc_pass")
        # Provide a full IOR config so nothing is missing
        full_config = {
            "nprocs": 2,
            "ppn": 2,
            "block": "32m",
            "xfer": "1m",
            "api": "posix",
            "out": "/tmp/ior.bin",
            "write": True,
            "read": False,
            "fpp": False,
            "reps": 1,
            "direct": False,
            "interceptors": [],
        }
        try:
            pipeline._validate_required_config("builtin.ior", full_config)
        except ValueError as e:
            if "Missing required" in str(e):
                self.fail(f"_validate_required_config raised unexpectedly: {e}")

    def test_validate_required_raises_when_missing(self):
        """_validate_required_config() raises ValueError when required fields are absent."""
        pipeline = self._make_pipeline("vrc_fail")

        # Use a package type that is known to have required fields with no default.
        # We patch configure_menu to inject a required field ourselves so the
        # test is self-contained and doesn't depend on IOR's specific schema.
        from unittest.mock import patch, MagicMock

        mock_pkg = MagicMock()
        mock_pkg.configure_menu.return_value = [
            {"name": "required_field", "default": None},
        ]

        with patch.object(pipeline, "_load_package_instance", return_value=mock_pkg):
            with self.assertRaises(ValueError) as ctx:
                pipeline._validate_required_config("builtin.ior", {})
        self.assertIn("Missing required", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
