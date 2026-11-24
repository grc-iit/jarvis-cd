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


class FakeJarvis:
    """The Jarvis lookups the installers make: the pipeline's shared dir."""

    def __init__(self, root):
        self.root = root

    def get_pipeline_shared_dir(self, name):
        return os.path.join(self.root, 'shared', name)


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
        import tempfile
        patcher = mock.patch('jarvis_cd.core.installer._run_local',
                             return_value=0)
        self.addCleanup(patcher.stop)
        self.mock_run = patcher.start()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.jarvis = FakeJarvis(tmp.name)


class TestPipInstaller(_BaseRunMock):

    def test_runs_aggregated_pip_install(self):
        ppl = FakePipeline(jarvis=self.jarvis)
        pkgs = [pkg('pip', 'numpy', 'a'), pkg('pip', 'pandas==2.1', 'b')]
        PipInstaller().install(ppl, pkgs)
        self.mock_run.assert_called_once()
        cmd = self.mock_run.call_args[0][0]
        self.assertIn('pip install', cmd)
        self.assertIn('numpy', cmd)
        self.assertIn('pandas==2.1', cmd)

    def test_no_queries_is_noop(self):
        ppl = FakePipeline(jarvis=self.jarvis)
        PipInstaller().install(ppl, [])
        self.mock_run.assert_not_called()

    def test_failure_raises(self):
        self.mock_run.return_value = 1
        ppl = FakePipeline(jarvis=self.jarvis)
        with self.assertRaises(RuntimeError):
            PipInstaller().install(ppl, [pkg('pip', 'numpy')])


class TestCondaInstaller(_BaseRunMock):

    def test_runs_aggregated_conda_install(self):
        ppl = FakePipeline(jarvis=self.jarvis)
        CondaInstaller().install(
            ppl, [pkg('conda', 'numpy'), pkg('conda', 'scipy')]
        )
        self.mock_run.assert_called_once()
        cmd = self.mock_run.call_args[0][0]
        self.assertTrue(cmd.startswith('conda install -y'))
        self.assertIn('numpy', cmd)
        self.assertIn('scipy', cmd)


_SPEC_ALL_PRESENT = (
    "[+]  aaaaaaa  lammps@2023%gcc@11 arch=linux\n"
    "[^]  bbbbbbb      ^fftw@3.3%gcc@11 arch=linux\n"
)
_SPEC_ONE_MISSING = (
    "[+]  bbbbbbb  fftw@3.3%gcc@11 arch=linux\n"
    " -   ccccccc  ior@3.3%gcc@11 arch=linux\n"
)
_SPEC_WITH_MPI = (
    " -   ddddddd  xcompact3d@4%gcc@11 arch=linux\n"
    "[^]  eeeeeee      ^openmpi@5.0.5%gcc@11 arch=linux\n"
)


class _FakeSpack:
    """Answers spack commands from a script and records every call."""

    def __init__(self, specs, find=None, install_rc=0):
        self.specs = specs
        self.find = find or {}
        self.install_rc = install_rc
        self.calls = []
        self.installed = False

    def __call__(self, runner, arguments, stream=False):
        import subprocess
        arguments = list(arguments)
        self.calls.append(arguments)
        out = ''
        rc = 0
        if arguments[:1] == ['spec']:
            key = ' '.join(arguments[3:])
            out = self.specs.get(key, '')
            if self.installed:
                out = out.replace(' -  ', '[+] ')
        elif arguments[:1] == ['find']:
            out = self.find.get(arguments[-1], '')
            rc = 0 if out else 1
        elif arguments[:1] == ['install']:
            rc = self.install_rc
            self.installed = rc == 0
        return subprocess.CompletedProcess(arguments, rc, out, '')


class TestSpackInstaller(unittest.TestCase):
    """The per-package ``spack install`` path (``JARVIS_SPACK_ENV=off``)."""

    def setUp(self):
        import tempfile
        from jarvis_cd.core.spack_install import InstallContext
        env = mock.patch.dict(os.environ, {'JARVIS_SPACK_ENV': 'off'})
        env.start()
        self.addCleanup(env.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.context = InstallContext(
            install_dir=self.tmp.name,
            decisions_path=os.path.join(self.tmp.name, 'd.yaml'))
        self.context.open()

    def tearDown(self):
        self.context.close()
        self.tmp.cleanup()

    def _run(self, fake, packages, mode='install'):
        from jarvis_cd.core.spack_install import InstallDecisionNeeded
        self.context.mode = mode
        ppl = FakePipeline(packages=packages, jarvis=FakeJarvis(self.tmp.name))
        with mock.patch('jarvis_cd.core.spack_install.SpackRunner.run',
                        autospec=True, side_effect=fake), \
                mock.patch('jarvis_cd.core.environment.EnvironmentManager') as em:
            em.return_value.capture_spack_environment.return_value = {'V': '1'}
            try:
                return Installer.install_all(ppl, mode=mode,
                                             context=self.context), ppl, em
            except InstallDecisionNeeded as needed:
                return needed, ppl, em

    def test_skips_install_when_everything_is_present(self):
        fake = _FakeSpack({'lammps': _SPEC_ALL_PRESENT})
        report, ppl, em = self._run(fake, [pkg('spack', 'lammps')])
        self.assertTrue(report['satisfied'])
        self.assertFalse(any(c[0] == 'install' for c in fake.calls))
        em.return_value.capture_spack_environment.assert_called_once_with(
            ['/aaaaaaa'])
        self.assertEqual(ppl.env['V'], '1')

    def test_installs_only_missing_spec(self):
        fake = _FakeSpack({'ior': _SPEC_ONE_MISSING})
        report, _, _ = self._run(fake, [pkg('spack', 'ior')])
        installs = [c for c in fake.calls if c[0] == 'install']
        self.assertEqual(installs, [['install', '--fail-fast', 'ior']])
        entry = report['installers']['spack']['packages']['p']
        self.assertEqual(entry['status'], 'installed')
        self.assertEqual(entry['built'], ['ior'])

    def test_check_mode_raises_not_installed_without_building(self):
        from jarvis_cd.core.spack_install import InstallDecisionNeeded
        fake = _FakeSpack({'ior': _SPEC_ONE_MISSING})
        needed, _, _ = self._run(fake, [pkg('spack', 'ior')], mode='check')
        self.assertIsInstance(needed, InstallDecisionNeeded)
        self.assertEqual(needed.issues[0]['kind'], 'not_installed')
        self.assertFalse(any(c[0] == 'install' for c in fake.calls))

    def test_ambiguous_mpi_asks_then_applies_answer(self):
        from jarvis_cd.core.spack_install import InstallDecisionNeeded
        find = {
            'openmpi': 'openmpi@5.0.5 ' + 'e' * 32 + ' /site/openmpi\n',
            'mpich': 'mpich@4.1.1 ' + 'f' * 32 + ' /site/mpich\n',
        }
        specs = {
            'xcompact3d': _SPEC_WITH_MPI,
            'xcompact3d ^mpich/fffffff': _SPEC_WITH_MPI.replace(
                'openmpi@5.0.5', 'mpich@4.1.1'),
        }
        needed, _, _ = self._run(_FakeSpack(specs, find),
                                 [pkg('spack', 'xcompact3d')])
        self.assertIsInstance(needed, InstallDecisionNeeded)
        issue = needed.issues[0]
        self.assertEqual(issue['kind'], 'ambiguous_provider')
        self.assertEqual(len(issue['candidates']), 2)

        self.context.issues = []
        self.context.answers = {issue['issue_id']: {'constraint': '^mpich/fffffff'}}
        fake = _FakeSpack(specs, find)
        report, _, _ = self._run(fake, [pkg('spack', 'xcompact3d')])
        self.assertIn(['install', '--fail-fast', 'xcompact3d', '^mpich/fffffff'],
                      fake.calls)
        self.assertTrue(report['satisfied'])
        with open(self.context.decisions_path) as stream:
            self.assertIn('^mpich/fffffff', stream.read())


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
        import subprocess
        import tempfile
        from jarvis_cd.core.spack_install import InstallContext
        ppl = FakePipeline(
            packages=[pkg('pip', 'numpy'), pkg('spack', 'ior')],
            jarvis=self.jarvis,
        )
        spack_calls = []

        def fake_spack(runner, arguments, stream=False):
            spack_calls.append(list(arguments))
            out = _SPEC_ALL_PRESENT if arguments[0] == 'spec' else ''
            return subprocess.CompletedProcess(arguments, 0, out, '')

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {'JARVIS_SPACK_ENV': 'off'}
        ), mock.patch(
            'jarvis_cd.core.environment.EnvironmentManager'
        ) as MockEM, mock.patch(
            'jarvis_cd.core.spack_install.SpackRunner.run',
            autospec=True, side_effect=fake_spack,
        ):
            MockEM.return_value.capture_spack_environment.return_value = {}
            context = InstallContext(
                install_dir=tmp, decisions_path=os.path.join(tmp, 'd.yaml'))
            context.open()
            try:
                report = Installer.install_all(ppl, context=context)
            finally:
                context.close()

        # pip runs its install; spack only resolves (everything is present)
        self.assertEqual(self.mock_run.call_count, 1)
        self.assertIn('pip install', self.mock_run.call_args[0][0])
        self.assertEqual(spack_calls, [['spec', '-I', '-l', 'ior']])
        self.assertEqual(report['installers']['spack']['status'], 'satisfied')


if __name__ == '__main__':
    unittest.main()
