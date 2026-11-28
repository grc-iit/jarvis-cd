"""
Tests for the Installer factory, its grouping, and the spack installer.

The pip, conda and container installers are covered in
``test_method_install.py``. These tests stub out subprocess execution so
they run fast and don't need the underlying tooling present.
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


def _spec(*nodes):
    """``spack spec --json`` for ``(name@version, hash letter, status,
    virtuals)`` rows, root first; the root depends on every other row."""
    import json
    rows = [{'name': spec.split('@')[0], 'version': spec.split('@')[1],
             'hash': letter * 32, 'status': status, 'virtuals': list(virtuals)}
            for spec, letter, status, *virtuals in nodes]
    documents = [
        {'name': row['name'], 'version': row['version'], 'hash': row['hash'],
         'compiler': {'name': 'gcc', 'version': '11'}, 'parameters': {},
         'dependencies': [
             {'name': dep['name'], 'hash': dep['hash'],
              'parameters': {'deptypes': ['link'], 'virtuals': dep['virtuals']}}
             for dep in (rows[1:] if index == 0 else [])]}
        for index, row in enumerate(rows)]
    return {'json': json.dumps({'spec': {'_meta': {}, 'nodes': documents}}),
            'installed': {r['hash'] for r in rows if r['status'] != 'missing'},
            'upstream': {r['hash'] for r in rows if r['status'] == 'upstream'}}


_SPEC_ALL_PRESENT = _spec(('lammps@2023', 'a', 'installed'),
                          ('fftw@3.3', 'b', 'upstream', 'fftw-api'))
_SPEC_ONE_MISSING = _spec(('ior@3.3', 'c', 'missing'), ('fftw@3.3', 'b', 'installed'))
_SPEC_WITH_MPI = _spec(('xcompact3d@4', 'd', 'missing'),
                       ('openmpi@5.0.5', 'e', 'upstream', 'mpi'))


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
        if arguments[:2] == ['spec', '--json']:
            out = self.specs.get(' '.join(arguments[2:]), {}).get('json', '')
        elif arguments[:1] == ['find'] and '{hash}' in arguments:
            tree = 'upstream' if '--install-tree' in arguments else 'installed'
            hashes = set().union(*(spec[tree] for spec in self.specs.values()))
            if self.installed and tree == 'installed':
                hashes |= {spec_hash for spec in self.specs.values()
                           for spec_hash in json_hashes(spec['json'])}
            out = '\n'.join(sorted(hashes))
        elif arguments[:1] == ['find']:
            out = ''.join(self.find.values())
            rc = 0 if out else 1
        elif arguments[:1] == ['providers']:
            out = 'mpich\nopenmpi\n'
        elif arguments[:1] == ['install']:
            rc = self.install_rc
            self.installed = rc == 0
        return subprocess.CompletedProcess(arguments, rc, out, '')


def json_hashes(text):
    import json
    return [node['hash'] for node in json.loads(text)['spec']['nodes']]


class TestSpackInstaller(unittest.TestCase):
    """The per-package ``spack install`` path (``JARVIS_SPACK_ENV=off``)."""

    def setUp(self):
        import tempfile
        from jarvis_cd.core.spack_install import InstallContext
        env = mock.patch.dict(os.environ, {'JARVIS_SPACK_ENV': 'off'})
        env.start()
        self.addCleanup(env.stop)
        # SpackRunner.run is faked, so the faked Spack counts as available.
        available = mock.patch(
            'jarvis_cd.core.spack_install.spack_unavailable_reason',
            return_value=None)
        available.start()
        self.addCleanup(available.stop)
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
            ['/' + 'a' * 32], config_scopes=())
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
            'xcompact3d ^mpich/fffffff': _spec(('xcompact3d@4', 'd', 'missing'),
                                               ('mpich@4.1.1', 'f', 'upstream', 'mpi')),
        }
        needed, _, _ = self._run(_FakeSpack(specs, find),
                                 [pkg('spack', 'xcompact3d')])
        self.assertIsInstance(needed, InstallDecisionNeeded)
        issue = needed.issues[0]
        self.assertEqual(issue['kind'], 'ambiguous_provider')
        self.assertEqual(len(issue['candidates']), 2)
        self.assertIn('(MPI providers found: mpich, openmpi)', issue['evidence'][0])
        self.assertNotIn('cray-mpich', issue['evidence'][0])

        self.context.issues = []
        self.context.answers = {issue['issue_id']: {'constraint': '^mpich/fffffff'}}
        fake = _FakeSpack(specs, find)
        report, _, _ = self._run(fake, [pkg('spack', 'xcompact3d')])
        self.assertIn(['install', '--fail-fast', 'xcompact3d', '^mpich/fffffff'],
                      fake.calls)
        self.assertTrue(report['satisfied'])
        with open(self.context.decisions_path) as stream:
            self.assertIn('^mpich/fffffff', stream.read())

    def test_mpi_discovery_asks_spack_for_the_mpi_virtual_once(self):
        from jarvis_cd.core.spack_install import SpackPlanner, SpackRunner
        fake = _FakeSpack({}, {
            'openmpi': 'openmpi@5.0.5 ' + 'e' * 32 + ' /site/openmpi external=\n',
        })
        with mock.patch('jarvis_cd.core.spack_install.SpackRunner.run',
                        autospec=True, side_effect=fake):
            planner = SpackPlanner(self.context, SpackRunner(self.context.log_stream))
            candidates = planner.mpi_candidates()
        self.assertEqual([c['name'] for c in candidates], ['openmpi'])
        finds = [c for c in fake.calls if c[0] == 'find']
        self.assertEqual(len(finds), 1)
        self.assertEqual(finds[0][-1], 'mpi')

    def test_check_reports_the_provider_of_every_virtual_edge(self):
        from jarvis_cd.core.spack_install import InstallDecisionNeeded
        fake = _FakeSpack({'ior': _spec(('ior@3.3', 'c', 'missing'),
                                        ('fftw@2.1.5', 'b', 'installed', 'fftw-api'),
                                        ('openmpi@5.0.5', 'e', 'upstream', 'mpi'))})
        needed, _, _ = self._run(fake, [pkg('spack', 'ior')], mode='check')
        self.assertIsInstance(needed, InstallDecisionNeeded)
        entry = needed.report['installers']['spack']['packages']['p']
        self.assertEqual(entry['providers'], [
            {'virtual': 'fftw-api', 'name': 'fftw', 'version': '2.1.5',
             'hash': 'b' * 32, 'status': 'installed'},
            {'virtual': 'mpi', 'name': 'openmpi', 'version': '5.0.5',
             'hash': 'e' * 32, 'status': 'upstream'}])
        self.assertEqual(entry['mpi']['name'], 'openmpi')


class TestInstallAll(unittest.TestCase):
    """Integration smoke test: install_all routes packages to each
    installer and updates env."""

    def setUp(self):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.jarvis = FakeJarvis(tmp.name)

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
            out = ''
            if arguments[0] == 'spec':
                out = _SPEC_ALL_PRESENT['json']
            elif arguments[0] == 'find' and '--install-tree' not in arguments:
                out = '\n'.join(_SPEC_ALL_PRESENT['installed'])
            return subprocess.CompletedProcess(arguments, 0, out, '')

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {'JARVIS_SPACK_ENV': 'off'}
        ), mock.patch(
            'jarvis_cd.core.spack_install.spack_unavailable_reason',
            return_value=None,
        ), mock.patch(
            'jarvis_cd.core.environment.EnvironmentManager'
        ) as MockEM, mock.patch(
            'jarvis_cd.core.spack_install.SpackRunner.run',
            autospec=True, side_effect=fake_spack,
        ), mock.patch(
            'jarvis_cd.core.method_install.pip_run',
            return_value={'method': 'pip', 'status': 'installed'},
        ) as pip_run:
            MockEM.return_value.capture_spack_environment.return_value = {}
            context = InstallContext(
                install_dir=tmp, decisions_path=os.path.join(tmp, 'd.yaml'))
            context.open()
            try:
                report = Installer.install_all(ppl, context=context)
            finally:
                context.close()

        # pip runs its install; spack only resolves (everything is present)
        pip_run.assert_called_once()
        self.assertEqual(pip_run.call_args[0][3], 'install')
        self.assertEqual(report['installers']['pip']['status'], 'installed')
        # spack only reads its config and resolves (everything is present)
        self.assertTrue(all(c[:2] == ['config', 'get']
                            for c in spack_calls if c[0] == 'config'))
        self.assertEqual([c for c in spack_calls if c[0] == 'spec'],
                         [['spec', '--json', 'ior']])
        self.assertEqual(report['installers']['spack']['status'], 'satisfied')


if __name__ == '__main__':
    unittest.main()
