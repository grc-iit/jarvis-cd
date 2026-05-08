"""
Tests for spack install_manager integration.
Verifies that:
  - install_manager is parsed from pipeline config
  - deploy_mode is derived from install_manager and propagated
  - Spack spec collection works correctly
  - Backwards compatibility: no install_manager = default behavior
"""
import unittest
import sys
import os
import tempfile
import shutil

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


class TestInstallManagerBase(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix='jarvis_test_spack_')
        self.config_dir = os.path.join(self.test_dir, 'config')
        self.private_dir = os.path.join(self.test_dir, 'private')
        self.shared_dir = os.path.join(self.test_dir, 'shared')
        os.makedirs(self.config_dir, exist_ok=True)
        os.makedirs(self.private_dir, exist_ok=True)
        os.makedirs(self.shared_dir, exist_ok=True)

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

    def _make_pkg_def(self, pipeline_name, pkg_type, pkg_name, config):
        config.setdefault('interceptors', [])
        return {
            'pkg_type': pkg_type,
            'pkg_id': pkg_name,
            'pkg_name': pkg_name.split('.')[-1],
            'global_id': f'{pipeline_name}.{pkg_name}',
            'config': config,
        }


class TestInstallManagerParsing(TestInstallManagerBase):
    """Test that install_manager is parsed and persisted correctly."""

    def test_default_install_manager_is_none(self):
        pipeline = Pipeline()
        pipeline.create('test_default')
        self.assertIsNone(pipeline.install_manager)

    def test_container_install_manager(self):
        pipeline = Pipeline()
        pipeline.create('test_container')
        pipeline.install_manager = 'container'
        pipeline.save()

        pipeline2 = Pipeline('test_container')
        self.assertEqual(pipeline2.install_manager, 'container')

    def test_spack_install_manager(self):
        pipeline = Pipeline()
        pipeline.create('test_spack')
        pipeline.install_manager = 'spack'
        pipeline.save()

        pipeline2 = Pipeline('test_spack')
        self.assertEqual(pipeline2.install_manager, 'spack')

    def test_no_install_manager_not_saved(self):
        """When install_manager is None, it should not appear in saved config."""
        pipeline = Pipeline()
        pipeline.create('test_none')
        pipeline.save()

        import yaml
        config_file = self.jarvis.get_pipeline_dir('test_none') / 'pipeline.yaml'
        with open(config_file, 'r') as f:
            config = yaml.safe_load(f)
        self.assertNotIn('install_manager', config)


class TestDeployModePropagation(TestInstallManagerBase):
    """Test that deploy_mode is derived from install_manager."""

    def test_container_sets_deploy_mode_container(self):
        pipeline = Pipeline()
        pipeline.create('test_prop_cont')
        pipeline.install_manager = 'container'

        pkg_def = self._make_pkg_def('test_prop_cont', 'builtin.ior', 'ior', {
            'nprocs': 1, 'ppn': 1, 'block': '32m', 'xfer': '1m',
            'api': 'posix', 'out': '/tmp/ior.bin', 'write': True,
            'read': False, 'fpp': False, 'reps': 1, 'direct': False,
        })
        pipeline.packages.append(pkg_def)
        pipeline._propagate_deploy_mode()

        self.assertEqual(pkg_def['config']['deploy_mode'], 'container')

    def test_spack_sets_deploy_mode_default(self):
        pipeline = Pipeline()
        pipeline.create('test_prop_spack')
        pipeline.install_manager = 'spack'

        pkg_def = self._make_pkg_def('test_prop_spack', 'builtin.ior', 'ior', {
            'nprocs': 1, 'ppn': 1, 'block': '32m', 'xfer': '1m',
            'api': 'posix', 'out': '/tmp/ior.bin', 'write': True,
            'read': False, 'fpp': False, 'reps': 1, 'direct': False,
        })
        pipeline.packages.append(pkg_def)
        pipeline._propagate_deploy_mode()

        self.assertEqual(pkg_def['config']['deploy_mode'], 'default')

    def test_no_install_manager_sets_deploy_mode_default(self):
        pipeline = Pipeline()
        pipeline.create('test_prop_none')

        pkg_def = self._make_pkg_def('test_prop_none', 'builtin.ior', 'ior', {
            'nprocs': 1, 'ppn': 1, 'block': '32m', 'xfer': '1m',
            'api': 'posix', 'out': '/tmp/ior.bin', 'write': True,
            'read': False, 'fpp': False, 'reps': 1, 'direct': False,
        })
        pipeline.packages.append(pkg_def)
        pipeline._propagate_deploy_mode()

        self.assertEqual(pkg_def['config']['deploy_mode'], 'default')

    def test_propagates_to_interceptors(self):
        pipeline = Pipeline()
        pipeline.create('test_prop_intc')
        pipeline.install_manager = 'container'

        interceptor_def = self._make_pkg_def('test_prop_intc', 'builtin.darshan',
                                             'darshan', {})
        pipeline.interceptors['darshan'] = interceptor_def
        pipeline._propagate_deploy_mode()

        self.assertEqual(interceptor_def['config']['deploy_mode'], 'container')

    def test_propagates_to_multiple_packages(self):
        pipeline = Pipeline()
        pipeline.create('test_prop_multi')
        pipeline.install_manager = 'container'

        ior_def = self._make_pkg_def('test_prop_multi', 'builtin.ior', 'ior', {
            'nprocs': 1, 'ppn': 1, 'block': '32m', 'xfer': '1m',
            'api': 'posix', 'out': '/tmp/ior.bin', 'write': True,
            'read': False, 'fpp': False, 'reps': 1, 'direct': False,
        })
        redis_def = self._make_pkg_def('test_prop_multi', 'builtin.redis',
                                       'redis', {'port': 6379})
        pipeline.packages.append(ior_def)
        pipeline.packages.append(redis_def)
        pipeline._propagate_deploy_mode()

        self.assertEqual(ior_def['config']['deploy_mode'], 'container')
        self.assertEqual(redis_def['config']['deploy_mode'], 'container')


class TestSpackSpecCollection(TestInstallManagerBase):
    """Test that spack specs are collected from packages."""

    def test_collect_specs(self):
        pipeline = Pipeline()
        pipeline.create('test_specs')
        pipeline.install_manager = 'spack'

        ior_def = self._make_pkg_def('test_specs', 'builtin.ior', 'ior', {
            'install': 'ior',
            'nprocs': 1, 'ppn': 1, 'block': '32m', 'xfer': '1m',
            'api': 'posix', 'out': '/tmp/ior.bin', 'write': True,
            'read': False, 'fpp': False, 'reps': 1, 'direct': False,
        })
        lammps_def = self._make_pkg_def('test_specs', 'builtin.lammps', 'lammps', {
            'install': 'lammps+kokkos',
            'nprocs': 1, 'ppn': 1, 'cuda_arch': 80,
            'base_image': 'sci-hpc-base', 'kokkos_gpu': False,
            'num_gpus': 0, 'script': '/opt/lammps/bench/in.lj',
            'out': '/tmp/out', 'lmp_bin': 'lmp',
        })
        pipeline.packages.append(ior_def)
        pipeline.packages.append(lammps_def)

        specs = []
        for pkg_def in pipeline.packages:
            install_spec = pkg_def.get('config', {}).get('install', '')
            if install_spec:
                specs.append(install_spec)

        self.assertEqual(specs, ['ior', 'lammps+kokkos'])

    def test_skip_empty_install(self):
        pipeline = Pipeline()
        pipeline.create('test_skip')
        pipeline.install_manager = 'spack'

        pkg_def = self._make_pkg_def('test_skip', 'builtin.ior', 'ior', {
            'install': '',
            'nprocs': 1, 'ppn': 1, 'block': '32m', 'xfer': '1m',
            'api': 'posix', 'out': '/tmp/ior.bin', 'write': True,
            'read': False, 'fpp': False, 'reps': 1, 'direct': False,
        })
        pipeline.packages.append(pkg_def)

        specs = []
        for pd in pipeline.packages:
            install_spec = pd.get('config', {}).get('install', '')
            if install_spec:
                specs.append(install_spec)

        self.assertEqual(specs, [])


class TestHasContainerizedPackages(TestInstallManagerBase):
    """Test _has_containerized_packages with install_manager."""

    def test_spack_returns_false(self):
        """When install_manager is spack, _has_containerized_packages should be False."""
        pipeline = Pipeline()
        pipeline.create('test_hcp_spack')
        pipeline.install_manager = 'spack'

        pkg_def = self._make_pkg_def('test_hcp_spack', 'builtin.ior', 'ior', {
            'install': 'ior', 'nprocs': 1, 'ppn': 1, 'block': '32m',
            'xfer': '1m', 'api': 'posix', 'out': '/tmp/ior.bin',
            'write': True, 'read': False, 'fpp': False, 'reps': 1,
            'direct': False,
        })
        pipeline.packages.append(pkg_def)
        pipeline._propagate_deploy_mode()

        self.assertFalse(pipeline._has_containerized_packages())

    def test_container_returns_true(self):
        """When install_manager is container, _has_containerized_packages should be True."""
        pipeline = Pipeline()
        pipeline.create('test_hcp_cont')
        pipeline.install_manager = 'container'

        pkg_def = self._make_pkg_def('test_hcp_cont', 'builtin.ior', 'ior', {
            'nprocs': 1, 'ppn': 1, 'block': '32m', 'xfer': '1m',
            'api': 'posix', 'out': '/tmp/ior.bin', 'write': True,
            'read': False, 'fpp': False, 'reps': 1, 'direct': False,
        })
        pipeline.packages.append(pkg_def)
        pipeline._propagate_deploy_mode()

        self.assertTrue(pipeline._has_containerized_packages())

    def test_none_returns_false(self):
        """When no install_manager, default deploy_mode=default means no containers."""
        pipeline = Pipeline()
        pipeline.create('test_hcp_none')

        pkg_def = self._make_pkg_def('test_hcp_none', 'builtin.ior', 'ior', {
            'nprocs': 1, 'ppn': 1, 'block': '32m', 'xfer': '1m',
            'api': 'posix', 'out': '/tmp/ior.bin', 'write': True,
            'read': False, 'fpp': False, 'reps': 1, 'direct': False,
        })
        pipeline.packages.append(pkg_def)
        pipeline._propagate_deploy_mode()

        self.assertFalse(pipeline._has_containerized_packages())


if __name__ == '__main__':
    unittest.main()
