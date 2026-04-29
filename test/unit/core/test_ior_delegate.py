"""
Unit tests for IOR pipeline container engine configuration.

The IOR class was merged into a single pkg.py that handles both default
(bare-metal) and container deployment modes directly — there are no separate
default.py / container.py delegate files.  These tests verify that:

  - IOR correctly reports its container engine (docker, podman, apptainer)
    via the _container_engine property when running in container mode.
  - IOR returns None from _build_phase() / _build_deploy_phase() in default mode.
  - IOR returns valid build content in container mode regardless of engine.
  - The _build_engine property correctly resolves apptainer → docker/podman.
  - deploy_mode is propagated from the pipeline to all packages.
"""
import unittest
import sys
import os
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

from jarvis_cd.core.config import Jarvis
from jarvis_cd.core.pipeline import Pipeline


def initialize_jarvis_for_test(config_dir, private_dir, shared_dir):
    jarvis = Jarvis.get_instance()
    saved_config = None
    if jarvis.config_file.exists():
        import yaml
        with open(jarvis.config_file, 'r') as f:
            saved_config = yaml.safe_load(f)
    jarvis.initialize(config_dir, private_dir, shared_dir, force=False)
    return jarvis, saved_config


class TestIorContainerEngine(unittest.TestCase):
    """Test IOR pipeline behaviour across docker, podman, and apptainer engines."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix='jarvis_test_ior_engine_')
        self.config_dir = os.path.join(self.test_dir, 'config')
        self.private_dir = os.path.join(self.test_dir, 'private')
        self.shared_dir = os.path.join(self.test_dir, 'shared')
        for d in [self.config_dir, self.private_dir, self.shared_dir]:
            os.makedirs(d, exist_ok=True)

        os.environ['JARVIS_CONFIG'] = self.config_dir
        os.environ['JARVIS_PRIVATE'] = self.private_dir
        os.environ['JARVIS_SHARED'] = self.shared_dir

        self.jarvis, self._saved_config = initialize_jarvis_for_test(
            self.config_dir, self.private_dir, self.shared_dir
        )

    def tearDown(self):
        if self._saved_config:
            import yaml
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

    def _make_pipeline(self, name, engine, install_manager='container',
                       container_base='ubuntu:24.04'):
        pipeline = Pipeline()
        pipeline.create(name)
        pipeline.install_manager = install_manager
        pipeline.container_engine = engine
        pipeline.container_base = container_base
        return pipeline

    def _make_pkg_def(self, pipeline_name):
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

    def _load_ior(self, pipeline, pkg_def):
        pipeline.packages.append(pkg_def)
        pipeline._propagate_deploy_mode()
        pipeline.save()
        return pipeline._load_package_instance(pkg_def, {})

    # ------------------------------------------------------------------
    # Default (bare-metal) mode — no container engine
    # ------------------------------------------------------------------

    def test_default_mode_container_engine_is_none(self):
        """In default mode _container_engine should return 'none'."""
        pipeline = self._make_pipeline('ior_def_eng', 'docker',
                                       install_manager=None)
        pkg_def = self._make_pkg_def(pipeline.name)
        pkg = self._load_ior(pipeline, pkg_def)

        self.assertEqual(pkg._container_engine, 'none')

    def test_default_mode_build_phase_returns_none(self):
        """_build_phase() returns None in default mode (no container build)."""
        pipeline = self._make_pipeline('ior_def_bp', 'docker',
                                       install_manager=None)
        pkg_def = self._make_pkg_def(pipeline.name)
        pkg = self._load_ior(pipeline, pkg_def)

        self.assertIsNone(pkg._build_phase())

    def test_default_mode_deploy_phase_returns_none(self):
        """_build_deploy_phase() returns None in default mode."""
        pipeline = self._make_pipeline('ior_def_dp', 'docker',
                                       install_manager=None)
        pkg_def = self._make_pkg_def(pipeline.name)
        pkg = self._load_ior(pipeline, pkg_def)

        self.assertIsNone(pkg._build_deploy_phase())

    # ------------------------------------------------------------------
    # Docker
    # ------------------------------------------------------------------

    def test_docker_container_engine_reported(self):
        """_container_engine returns 'docker' when pipeline uses docker."""
        pipeline = self._make_pipeline('ior_docker_eng', 'docker')
        pkg_def = self._make_pkg_def(pipeline.name)
        pkg = self._load_ior(pipeline, pkg_def)

        self.assertEqual(pkg._container_engine, 'docker')

    def test_docker_pipeline_persists_engine(self):
        """Pipeline saves and reloads container_engine=docker correctly."""
        pipeline = self._make_pipeline('ior_docker_persist', 'docker')
        pipeline.save()

        pipeline2 = Pipeline('ior_docker_persist')
        self.assertEqual(pipeline2.container_engine, 'docker')

    def test_docker_build_phase_returns_script(self):
        """_build_phase() returns a (script, suffix) tuple for docker."""
        pipeline = self._make_pipeline('ior_docker_bp', 'docker')
        pkg_def = self._make_pkg_def(pipeline.name)
        pkg = self._load_ior(pipeline, pkg_def)

        result = pkg._build_phase()
        self.assertIsNotNone(result)
        content, suffix = result
        self.assertIsInstance(content, str)
        self.assertIn('set -e', content)
        self.assertEqual(suffix, 'mpi')

    def test_docker_deploy_phase_references_build_image(self):
        """_build_deploy_phase() Dockerfile must reference the build image."""
        pipeline = self._make_pipeline('ior_docker_dp', 'docker')
        pkg_def = self._make_pkg_def(pipeline.name)
        pkg = self._load_ior(pipeline, pkg_def)

        _, build_suffix = pkg._build_phase()
        pkg._build_suffix = build_suffix
        content, _ = pkg._build_deploy_phase()
        self.assertIn(pkg.build_image_name(), content)

    def test_docker_deploy_mode_propagated(self):
        """deploy_mode='container' should be propagated to IOR package."""
        pipeline = self._make_pipeline('ior_docker_mode', 'docker')
        pkg_def = self._make_pkg_def(pipeline.name)
        pkg = self._load_ior(pipeline, pkg_def)

        self.assertEqual(pkg.config.get('deploy_mode'), 'container')

    # ------------------------------------------------------------------
    # Podman
    # ------------------------------------------------------------------

    def test_podman_container_engine_reported(self):
        """_container_engine returns 'podman' when pipeline uses podman."""
        pipeline = self._make_pipeline('ior_podman_eng', 'podman',
                                       container_base='docker.io/ubuntu:24.04')
        pkg_def = self._make_pkg_def(pipeline.name)
        pkg = self._load_ior(pipeline, pkg_def)

        self.assertEqual(pkg._container_engine, 'podman')

    def test_podman_pipeline_persists_engine(self):
        """Pipeline saves and reloads container_engine=podman correctly."""
        pipeline = self._make_pipeline('ior_podman_persist', 'podman')
        pipeline.save()

        pipeline2 = Pipeline('ior_podman_persist')
        self.assertEqual(pipeline2.container_engine, 'podman')

    def test_podman_build_phase_returns_script(self):
        """_build_phase() returns the same MPI build script for podman."""
        pipeline = self._make_pipeline('ior_podman_bp', 'podman',
                                       container_base='docker.io/ubuntu:24.04')
        pkg_def = self._make_pkg_def(pipeline.name)
        pkg = self._load_ior(pipeline, pkg_def)

        result = pkg._build_phase()
        self.assertIsNotNone(result)
        content, suffix = result
        self.assertIn('set -e', content)
        self.assertEqual(suffix, 'mpi')

    def test_podman_deploy_mode_propagated(self):
        """deploy_mode='container' propagates to IOR package with podman engine."""
        pipeline = self._make_pipeline('ior_podman_mode', 'podman')
        pkg_def = self._make_pkg_def(pipeline.name)
        pkg = self._load_ior(pipeline, pkg_def)

        self.assertEqual(pkg.config.get('deploy_mode'), 'container')

    def test_podman_build_suffix_matches_docker(self):
        """Podman and docker produce the same image suffix for IOR."""
        docker_pipeline = self._make_pipeline('ior_suf_docker', 'docker')
        podman_pipeline = self._make_pipeline('ior_suf_podman', 'podman')

        pkg_def_d = self._make_pkg_def(docker_pipeline.name)
        pkg_def_p = self._make_pkg_def(podman_pipeline.name)

        pkg_d = self._load_ior(docker_pipeline, pkg_def_d)
        pkg_p = self._load_ior(podman_pipeline, pkg_def_p)

        _, suffix_d = pkg_d._build_phase()
        _, suffix_p = pkg_p._build_phase()

        self.assertEqual(suffix_d, suffix_p)

    # ------------------------------------------------------------------
    # Apptainer
    # ------------------------------------------------------------------

    def test_apptainer_container_engine_reported(self):
        """_container_engine returns 'apptainer' when pipeline uses apptainer."""
        pipeline = self._make_pipeline('ior_apptainer_eng', 'apptainer')
        pkg_def = self._make_pkg_def(pipeline.name)
        pkg = self._load_ior(pipeline, pkg_def)

        self.assertEqual(pkg._container_engine, 'apptainer')

    def test_apptainer_pipeline_persists_engine(self):
        """Pipeline saves and reloads container_engine=apptainer correctly."""
        pipeline = self._make_pipeline('ior_apptainer_persist', 'apptainer')
        pipeline.save()

        pipeline2 = Pipeline('ior_apptainer_persist')
        self.assertEqual(pipeline2.container_engine, 'apptainer')

    def test_apptainer_build_engine_falls_back_to_docker_or_podman(self):
        """_build_engine for apptainer must resolve to docker or podman."""
        pipeline = self._make_pipeline('ior_apptainer_be', 'apptainer')
        pkg_def = self._make_pkg_def(pipeline.name)
        pkg = self._load_ior(pipeline, pkg_def)

        # Apptainer uses docker/podman for the build phase.
        # Mock shutil.which to return a docker path so the test is hermetic.
        with patch('shutil.which', side_effect=lambda cmd: '/usr/bin/docker' if cmd == 'docker' else None):
            build_engine = pkg._build_engine
        self.assertEqual(build_engine, 'docker')

    def test_apptainer_build_engine_falls_back_to_podman_when_no_docker(self):
        """_build_engine falls back to podman if docker is absent."""
        pipeline = self._make_pipeline('ior_apptainer_be2', 'apptainer')
        pkg_def = self._make_pkg_def(pipeline.name)
        pkg = self._load_ior(pipeline, pkg_def)

        with patch('shutil.which', side_effect=lambda cmd: None if cmd == 'docker' else '/usr/bin/podman'):
            build_engine = pkg._build_engine
        self.assertEqual(build_engine, 'podman')

    def test_apptainer_build_phase_returns_same_script(self):
        """_build_phase() returns the same MPI build script regardless of engine."""
        pipeline = self._make_pipeline('ior_apptainer_bp', 'apptainer')
        pkg_def = self._make_pkg_def(pipeline.name)
        pkg = self._load_ior(pipeline, pkg_def)

        result = pkg._build_phase()
        self.assertIsNotNone(result)
        content, suffix = result
        self.assertIn('set -e', content)
        self.assertEqual(suffix, 'mpi')

    def test_apptainer_deploy_mode_propagated(self):
        """deploy_mode='container' propagates to IOR package with apptainer engine."""
        pipeline = self._make_pipeline('ior_apptainer_mode', 'apptainer')
        pkg_def = self._make_pkg_def(pipeline.name)
        pkg = self._load_ior(pipeline, pkg_def)

        self.assertEqual(pkg.config.get('deploy_mode'), 'container')

    def test_apptainer_raises_when_neither_docker_nor_podman(self):
        """_build_engine raises RuntimeError if neither docker nor podman exists."""
        pipeline = self._make_pipeline('ior_apptainer_err', 'apptainer')
        pkg_def = self._make_pkg_def(pipeline.name)
        pkg = self._load_ior(pipeline, pkg_def)

        with patch('shutil.which', return_value=None):
            with self.assertRaises(RuntimeError):
                _ = pkg._build_engine

    # ------------------------------------------------------------------
    # Cross-engine: deploy image name is pipeline-scoped
    # ------------------------------------------------------------------

    def test_deploy_image_name_is_pipeline_name(self):
        """deploy_image_name() returns the pipeline name (engine-independent)."""
        for engine in ('docker', 'podman', 'apptainer'):
            with self.subTest(engine=engine):
                name = f'ior_img_{engine}'
                pipeline = self._make_pipeline(name, engine)
                pkg_def = self._make_pkg_def(pipeline.name)
                pkg = self._load_ior(pipeline, pkg_def)
                self.assertEqual(pkg.deploy_image_name(), name)


if __name__ == '__main__':
    unittest.main()
