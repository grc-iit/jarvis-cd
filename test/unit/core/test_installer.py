"""
Tests for the Installer factory and the concrete pip / conda / spack /
container installers.

The pip / conda / spack tests stub out subprocess execution so they run
fast and don't need the underlying tooling present. The container test
exercises ``Installer.get_installers()`` grouping plus the prebuilt-uri
fast path (no real ``docker build`` invoked).
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

from jarvis_cd.core.installer import (
    Installer,
    PipInstaller,
    CondaInstaller,
    SpackInstaller,
    ContainerInstaller,
)


class FakePipeline:
    """Minimal stand-in for Pipeline that the installers actually touch."""

    def __init__(self, packages=None, base_deploy_mode=None, **extras):
        self.packages = packages or []
        self.env = {}
        self.base_deploy_mode = base_deploy_mode
        self.name = extras.get('name', 'fake_pipeline')
        self.container_image = extras.get('container_image', '')
        self.container_uri = extras.get('container_uri', '')
        self.container_engine = extras.get('container_engine', 'podman')
        self.container_base = extras.get('container_base', 'ubuntu:24.04')
        # Tracked side-effect signals
        self.saved = False
        self.generated_yaml = False
        self.generated_compose = False
        # Optional jarvis stub for the spack env-capture path.
        self.jarvis = extras.get('jarvis')

    def save(self):
        self.saved = True

    def is_containerized(self):
        return bool(self.container_image)

    def _generate_pipeline_container_yaml(self):
        self.generated_yaml = True

    def _generate_pipeline_compose_file(self):
        self.generated_compose = True


def pkg(method, query, name='p'):
    return {
        'pkg_type': f'builtin.{name}',
        'pkg_id': name,
        'pkg_name': name,
        'config': {'install_method': method, 'install_query': query},
    }


class TestInstallerFactory(unittest.TestCase):
    """Installer.get_installers groups packages by install_method."""

    def test_registry_lists_known_methods(self):
        reg = Installer._registry()
        self.assertIn('pip', reg)
        self.assertIn('conda', reg)
        self.assertIn('spack', reg)
        self.assertIn('container', reg)

    def test_for_method_returns_correct_class(self):
        self.assertIsInstance(Installer.for_method('pip'), PipInstaller)
        self.assertIsInstance(Installer.for_method('conda'), CondaInstaller)
        self.assertIsInstance(Installer.for_method('spack'), SpackInstaller)
        self.assertIsInstance(Installer.for_method('container'), ContainerInstaller)

    def test_for_method_unknown_raises(self):
        with self.assertRaises(ValueError):
            Installer.for_method('not_a_thing')

    def test_get_installers_groups_by_method(self):
        ppl = FakePipeline(packages=[
            pkg('pip', 'numpy', 'p1'),
            pkg('spack', 'lammps', 'p2'),
            pkg('pip', 'pandas', 'p3'),
        ])
        grouped = Installer.get_installers(ppl)
        self.assertEqual(set(grouped), {'pip', 'spack'})
        self.assertEqual([p['pkg_name'] for p in grouped['pip']], ['p1', 'p3'])
        self.assertEqual([p['pkg_name'] for p in grouped['spack']], ['p2'])

    def test_get_installers_falls_back_to_base_deploy_mode(self):
        """Pkgs without install_method inherit container/spack from base_deploy_mode."""
        legacy = {
            'pkg_type': 'builtin.ior',
            'pkg_id': 'ior',
            'pkg_name': 'ior',
            'config': {},  # no install_method set
        }
        ppl = FakePipeline(packages=[legacy], base_deploy_mode='container')
        grouped = Installer.get_installers(ppl)
        self.assertEqual(list(grouped), ['container'])
        self.assertEqual(grouped['container'], [legacy])

    def test_get_installers_skips_packages_with_no_method(self):
        legacy = {
            'pkg_type': 'builtin.ior', 'pkg_id': 'ior', 'pkg_name': 'ior',
            'config': {},
        }
        ppl = FakePipeline(packages=[legacy], base_deploy_mode=None)
        grouped = Installer.get_installers(ppl)
        self.assertEqual(grouped, {})

    def test_get_installers_prefers_install_method_over_base(self):
        ppl = FakePipeline(
            packages=[pkg('pip', 'numpy')],
            base_deploy_mode='container',
        )
        grouped = Installer.get_installers(ppl)
        self.assertEqual(list(grouped), ['pip'])


class TestAggregateQueries(unittest.TestCase):
    """The shared install_query aggregation helper."""

    def test_uses_install_query(self):
        pkg_list = [pkg('pip', 'numpy'), pkg('pip', 'pandas')]
        self.assertEqual(
            Installer._aggregate_queries(pkg_list),
            ['numpy', 'pandas'],
        )

    def test_falls_back_to_legacy_install_key(self):
        legacy = {
            'pkg_type': 'builtin.ior', 'pkg_id': 'ior', 'pkg_name': 'ior',
            'config': {'install': 'ior'},
        }
        self.assertEqual(Installer._aggregate_queries([legacy]), ['ior'])

    def test_skips_empty_queries(self):
        empty = {
            'pkg_type': 'builtin.x', 'pkg_id': 'x', 'pkg_name': 'x',
            'config': {'install_query': ''},
        }
        self.assertEqual(Installer._aggregate_queries([empty]), [])


class _BaseRunMock(unittest.TestCase):
    """Patches the Exec/LocalExecInfo helper inside installer.py so
    concrete installers can be exercised without invoking real tools."""

    def setUp(self):
        patcher = mock.patch('jarvis_cd.core.installer._run_local',
                             return_value=0)
        self.addCleanup(patcher.stop)
        self.mock_run = patcher.start()


class TestPipInstaller(_BaseRunMock):

    def test_runs_aggregated_pip_install(self):
        ppl = FakePipeline()
        pkgs = [pkg('pip', 'numpy', 'a'), pkg('pip', 'pandas==2.1', 'b')]
        PipInstaller().install(ppl, pkgs)
        self.mock_run.assert_called_once()
        cmd = self.mock_run.call_args[0][0]
        self.assertIn('pip install', cmd)
        self.assertIn('numpy', cmd)
        self.assertIn('pandas==2.1', cmd)

    def test_no_queries_is_noop(self):
        ppl = FakePipeline()
        PipInstaller().install(ppl, [])
        self.mock_run.assert_not_called()

    def test_failure_raises(self):
        self.mock_run.return_value = 1
        ppl = FakePipeline()
        with self.assertRaises(RuntimeError):
            PipInstaller().install(ppl, [pkg('pip', 'numpy')])


class TestCondaInstaller(_BaseRunMock):

    def test_runs_aggregated_conda_install(self):
        ppl = FakePipeline()
        CondaInstaller().install(
            ppl, [pkg('conda', 'numpy'), pkg('conda', 'scipy')]
        )
        self.mock_run.assert_called_once()
        cmd = self.mock_run.call_args[0][0]
        self.assertTrue(cmd.startswith('conda install -y'))
        self.assertIn('numpy', cmd)
        self.assertIn('scipy', cmd)


class TestSpackInstaller(_BaseRunMock):

    def test_runs_spack_install_and_merges_env(self):
        ppl = FakePipeline()
        ppl.jarvis = object()  # EnvironmentManager only needs the handle
        captured_env = {'NEW_VAR': 'value'}

        with mock.patch(
            'jarvis_cd.core.environment.EnvironmentManager'
        ) as MockEM:
            MockEM.return_value.capture_spack_environment.return_value = captured_env
            SpackInstaller().install(
                ppl, [pkg('spack', 'lammps'), pkg('spack', 'ior')]
            )

        self.mock_run.assert_called_once()
        cmd = self.mock_run.call_args[0][0]
        self.assertIn('spack install', cmd)
        self.assertIn('lammps', cmd)
        self.assertIn('ior', cmd)
        self.assertEqual(ppl.env['NEW_VAR'], 'value')


class TestContainerInstaller(unittest.TestCase):
    """ContainerInstaller exercise that avoids real builds.

    The prebuilt-URI path is the easiest to verify end-to-end without a
    container engine present: with a uri set and the image flagged as
    locally available, install() just sets ppl.container_image and
    generates the runtime artifacts.
    """

    def test_prebuilt_uri_path(self):
        ppl = FakePipeline(
            container_uri='docker.io/example/foo:latest',
            container_engine='podman',
            packages=[pkg('container', '', 'app')],
        )
        with mock.patch.object(
            ContainerInstaller, '_ensure_container_uri_available',
            return_value=True,
        ) as mock_ensure:
            ContainerInstaller().install(ppl, ppl.packages)
        mock_ensure.assert_called_once_with(ppl)
        self.assertEqual(ppl.container_image,
                         'docker.io/example/foo:latest')
        self.assertTrue(ppl.saved)
        self.assertTrue(ppl.generated_yaml)
        self.assertTrue(ppl.generated_compose)

    def test_apptainer_skips_compose_generation(self):
        ppl = FakePipeline(
            container_uri='oras://example/foo.sif',
            container_engine='apptainer',
            packages=[pkg('container', '', 'app')],
        )
        with mock.patch.object(
            ContainerInstaller, '_ensure_container_uri_available',
            return_value=True,
        ):
            ContainerInstaller().install(ppl, ppl.packages)
        self.assertTrue(ppl.generated_yaml)
        self.assertFalse(ppl.generated_compose)

    def test_prebuilt_uri_unavailable_raises(self):
        ppl = FakePipeline(
            container_uri='docker.io/nope/missing:tag',
            container_engine='podman',
            packages=[pkg('container', '', 'app')],
        )
        with mock.patch.object(
            ContainerInstaller, '_ensure_container_uri_available',
            return_value=False,
        ):
            with self.assertRaises(RuntimeError):
                ContainerInstaller().install(ppl, ppl.packages)


class TestInstallAll(_BaseRunMock):
    """Integration smoke test: install_all routes packages to each
    installer and updates env."""

    def test_dispatch_runs_pip_and_spack(self):
        ppl = FakePipeline(
            packages=[pkg('pip', 'numpy'), pkg('spack', 'ior')]
        )
        ppl.jarvis = object()
        with mock.patch(
            'jarvis_cd.core.environment.EnvironmentManager'
        ) as MockEM:
            MockEM.return_value.capture_spack_environment.return_value = {}
            Installer.install_all(ppl)

        # Two install invocations total: pip + spack
        self.assertEqual(self.mock_run.call_count, 2)
        cmds = [c[0][0] for c in self.mock_run.call_args_list]
        self.assertTrue(any('pip install' in c for c in cmds))
        self.assertTrue(any('spack install' in c for c in cmds))


if __name__ == '__main__':
    unittest.main()
