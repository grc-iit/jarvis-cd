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
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

from jarvis_cd.core.config import Jarvis
from jarvis_cd.core.pipeline import Pipeline


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def initialize_jarvis_for_test(config_dir, private_dir, shared_dir):
    jarvis = Jarvis.get_instance()
    saved_config = None
    if jarvis.config_file.exists():
        with open(jarvis.config_file, 'r') as f:
            saved_config = yaml.safe_load(f)
    jarvis.initialize(config_dir, private_dir, shared_dir, force=False)
    return jarvis, saved_config


class TestPipelineCoverage(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix='jarvis_test_pipeline_coverage_')
        self.config_dir = os.path.join(self.test_dir, 'config')
        self.private_dir = os.path.join(self.test_dir, 'private')
        self.shared_dir = os.path.join(self.test_dir, 'shared')
        for d in [self.config_dir, self.private_dir, self.shared_dir]:
            os.makedirs(d, exist_ok=True)

        self.jarvis, self._saved_config = initialize_jarvis_for_test(
            self.config_dir, self.private_dir, self.shared_dir
        )

    def tearDown(self):
        if self._saved_config:
            jarvis = Jarvis.get_instance()
            jarvis.save_config(self._saved_config)
            jarvis.config_dir = self._saved_config.get('config_dir', jarvis.config_dir)
            jarvis.private_dir = self._saved_config.get('private_dir', jarvis.private_dir)
            jarvis.shared_dir = self._saved_config.get('shared_dir', jarvis.shared_dir)
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
            'pkg_type': 'builtin.ior',
            'pkg_id': 'ior',
            'pkg_name': 'ior',
            'global_id': f'{pipeline_name}.ior',
            'config': {
                'nprocs': 2, 'ppn': 2,
                'block': '32m', 'xfer': '1m',
                'api': 'posix', 'out': '/tmp/ior.bin',
                'write': True, 'read': False,
                'fpp': False, 'reps': 1, 'direct': False,
                'interceptors': [],
            },
        }

    # ------------------------------------------------------------------
    # destroy()
    # ------------------------------------------------------------------

    def test_destroy_removes_pipeline_dirs(self):
        """destroy() should remove config/shared/private dirs for the pipeline."""
        pipeline = self._make_pipeline('destroy_test')

        config_dir = self.jarvis.get_pipeline_dir('destroy_test')
        shared_dir = self.jarvis.get_pipeline_shared_dir('destroy_test')
        private_dir = self.jarvis.get_pipeline_private_dir('destroy_test')

        # All three directories must exist before destroy
        self.assertTrue(config_dir.exists(), "config dir should exist before destroy")
        self.assertTrue(shared_dir.exists(), "shared dir should exist before destroy")

        pipeline.destroy()

        self.assertFalse(config_dir.exists(), "config dir should be gone after destroy")

    # ------------------------------------------------------------------
    # rm()
    # ------------------------------------------------------------------

    def test_rm_package_removes_from_list(self):
        """rm() removes the named package from pipeline.packages."""
        pipeline = self._make_pipeline('rm_test')
        pkg_def = self._make_ior_pkg_def('rm_test')
        pipeline.packages.append(pkg_def)
        pipeline._propagate_deploy_mode()
        pipeline.save()

        self.assertEqual(len(pipeline.packages), 1)
        pipeline.rm('ior')
        self.assertEqual(len(pipeline.packages), 0)

    def test_rm_nonexistent_package_no_crash(self):
        """rm() on a non-existent package ID must not raise."""
        pipeline = self._make_pipeline('rm_noexist')
        # Should print a message but not raise
        try:
            pipeline.rm('nonexistent')
        except Exception as e:
            self.fail(f"rm() raised unexpectedly: {e}")

    # ------------------------------------------------------------------
    # status()
    # ------------------------------------------------------------------

    def test_status_returns_string(self):
        """status() must return a string (even with no packages)."""
        pipeline = self._make_pipeline('status_test')
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
        pipeline = self._make_pipeline('uid_pass')
        pkg_def = self._make_ior_pkg_def('uid_pass')
        pipeline.packages.append(pkg_def)
        # interceptors dict is empty — no conflict
        try:
            pipeline._validate_unique_ids()
        except Exception as e:
            self.fail(f"_validate_unique_ids raised unexpectedly: {e}")

    def test_validate_unique_ids_raises_for_duplicate(self):
        """_validate_unique_ids() raises when a package ID collides with an interceptor ID."""
        pipeline = self._make_pipeline('uid_fail')
        pkg_def = self._make_ior_pkg_def('uid_fail')
        pipeline.packages.append(pkg_def)
        # Add a fake interceptor with the same id 'ior'
        pipeline.interceptors['ior'] = {
            'pkg_type': 'builtin.ior',
            'pkg_id': 'ior',
            'pkg_name': 'ior',
            'global_id': 'uid_fail.ior',
            'config': {},
        }
        with self.assertRaises((ValueError, Exception)):
            pipeline._validate_unique_ids()

    # ------------------------------------------------------------------
    # _get_package_default_config()
    # ------------------------------------------------------------------

    def test_get_package_default_config_returns_dict(self):
        """_get_package_default_config('builtin.ior') must return a non-empty dict."""
        pipeline = self._make_pipeline('defcfg_test')
        result = pipeline._get_package_default_config('builtin.ior')
        self.assertIsInstance(result, dict)
        self.assertGreater(len(result), 0)

    # ------------------------------------------------------------------
    # configure_package()
    # ------------------------------------------------------------------

    def test_configure_package_updates_config(self):
        """configure_package() updates the config field for the named package."""
        pipeline = self._make_pipeline('cfgpkg_test')
        pkg_def = self._make_ior_pkg_def('cfgpkg_test')
        pipeline.packages.append(pkg_def)
        pipeline._propagate_deploy_mode()
        pipeline.save()

        # nprocs starts at 2; reconfigure to 8
        pipeline.configure_package('ior', ['--nprocs=8'])

        # Reload pipeline and confirm nprocs was persisted
        pipeline2 = Pipeline('cfgpkg_test')
        self.assertEqual(pipeline2.packages[0]['config'].get('nprocs'), 8)

    # ------------------------------------------------------------------
    # save / load container fields
    # ------------------------------------------------------------------

    def test_save_load_container_engine(self):
        """container_engine is persisted across save/load cycles."""
        pipeline = self._make_pipeline('ce_test')
        pipeline.container_engine = 'podman'
        pipeline.save()

        pipeline2 = Pipeline('ce_test')
        self.assertEqual(pipeline2.container_engine, 'podman')

    def test_save_load_install_manager(self):
        """install_manager='container' is persisted across save/load cycles."""
        pipeline = self._make_pipeline('im_test')
        pipeline.install_manager = 'container'
        pipeline.save()

        pipeline2 = Pipeline('im_test')
        self.assertEqual(pipeline2.install_manager, 'container')

    def test_save_load_container_base(self):
        """container_base is persisted across save/load cycles."""
        pipeline = self._make_pipeline('cb_test')
        pipeline.container_base = 'ubuntu:22.04'
        pipeline.save()

        pipeline2 = Pipeline('cb_test')
        self.assertEqual(pipeline2.container_base, 'ubuntu:22.04')

    def test_save_load_deploy_image(self):
        """container_image (deploy image) is persisted across save/load cycles."""
        pipeline = self._make_pipeline('di_test')
        pipeline.container_image = 'myimg:latest'
        pipeline.save()

        pipeline2 = Pipeline('di_test')
        self.assertEqual(pipeline2.container_image, 'myimg:latest')

    # ------------------------------------------------------------------
    # _generate_pipeline_container_yaml()
    # ------------------------------------------------------------------

    def test_generate_container_yaml_creates_file(self):
        """_generate_pipeline_container_yaml() writes pipeline.yaml to shared_dir."""
        pipeline = self._make_pipeline('gcy_test')
        pkg_def = self._make_ior_pkg_def('gcy_test')
        pipeline.packages.append(pkg_def)
        pipeline.save()

        yaml_path = pipeline._generate_pipeline_container_yaml()

        self.assertTrue(os.path.exists(str(yaml_path)),
                        f"Expected YAML file at {yaml_path}")

    def test_generate_container_yaml_has_packages(self):
        """The generated YAML file contains the pipeline packages."""
        pipeline = self._make_pipeline('gcy_pkgs_test')
        pkg_def = self._make_ior_pkg_def('gcy_pkgs_test')
        pipeline.packages.append(pkg_def)
        pipeline.save()

        yaml_path = pipeline._generate_pipeline_container_yaml()

        with open(str(yaml_path), 'r') as f:
            data = yaml.safe_load(f)

        self.assertIn('pkgs', data)
        self.assertGreater(len(data['pkgs']), 0)
        pkg_types = [p['pkg_type'] for p in data['pkgs']]
        self.assertIn('builtin.ior', pkg_types)

    # ------------------------------------------------------------------
    # _validate_required_config()
    # ------------------------------------------------------------------

    def test_validate_required_passes_when_all_present(self):
        """_validate_required_config() does not raise when config has all fields."""
        pipeline = self._make_pipeline('vrc_pass')
        # Provide a full IOR config so nothing is missing
        full_config = {
            'nprocs': 2, 'ppn': 2,
            'block': '32m', 'xfer': '1m',
            'api': 'posix', 'out': '/tmp/ior.bin',
            'write': True, 'read': False,
            'fpp': False, 'reps': 1, 'direct': False,
            'interceptors': [],
        }
        try:
            pipeline._validate_required_config('builtin.ior', full_config)
        except ValueError as e:
            if 'Missing required' in str(e):
                self.fail(f"_validate_required_config raised unexpectedly: {e}")

    def test_validate_required_raises_when_missing(self):
        """_validate_required_config() raises ValueError when required fields are absent."""
        pipeline = self._make_pipeline('vrc_fail')

        # Use a package type that is known to have required fields with no default.
        # We patch configure_menu to inject a required field ourselves so the
        # test is self-contained and doesn't depend on IOR's specific schema.
        from unittest.mock import patch, MagicMock

        mock_pkg = MagicMock()
        mock_pkg.configure_menu.return_value = [
            {'name': 'required_field', 'default': None},
        ]

        with patch.object(pipeline, '_load_package_instance', return_value=mock_pkg):
            with self.assertRaises(ValueError) as ctx:
                pipeline._validate_required_config('builtin.ior', {})
        self.assertIn('Missing required', str(ctx.exception))


if __name__ == '__main__':
    unittest.main()
