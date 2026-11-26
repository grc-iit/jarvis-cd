"""The site-copy search before a Spack build, and the ``site`` install method.

No module system or Spack runs: the login-shell and ``spack`` calls are
replaced by fakes that answer the way Lmod 8.7 and Spack 0.23 do on a site
whose Spack also writes an Lmod tree.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

from jarvis_cd.core import site_copies as sc
from jarvis_cd.core.installer import Installer, SiteInstaller
from jarvis_cd.core.spack_install import (
    Decisions,
    InstallContext,
    InstallDecisionNeeded,
    InstallFailed,
)

LMOD_TREE = '/site/spack/share/spack/lmod/linux-x86_64'
IOR_HASH = 'fihmxebyyrja5qsjnliud5eygvzcbln7'
IOR_PREFIX = f'/site/spack/opt/linux-x86_64/gcc-11.4.0/ior-3.3.0-{IOR_HASH}'
MPI_HASH = 'x2kvx5lbuxtfnpqi4j3fsyoahgr6w2wz'
MPI_PREFIX = f'/site/spack/opt/linux-x86_64/gcc-11.4.0/openmpi-5.0.5-{MPI_HASH}'

AVAIL = f"""{LMOD_TREE}/Core:
{LMOD_TREE}/openmpi/5.0.5-x2kvx5l/gcc/11.4.0:
openmpi/5.0.5-x2kvx5l/gcc/11.4.0/ior/3.3.0-fihmxeb
"""
SPIDER_TERSE = "ior/3.3.0-fihmxeb\n"
SPIDER_DETAIL = """
----------------------------------------------------------------------------
  ior: ior/3.3.0-fihmxeb
----------------------------------------------------------------------------

    You will need to load all module(s) on any one of the lines below before the "ior/3.3.0-fihmxeb" module is available to load.

      gcc/11.4.0/openmpi/5.0.5-x2kvx5l
      openmpi/5.0.5-x2kvx5l
 
    Help:
      The Parallel HDF5 IO Benchmark
"""
SHOW = f"""----------------------------------------------------------------------------
   {LMOD_TREE}/openmpi/5.0.5-x2kvx5l/gcc/11.4.0/ior/3.3.0-fihmxeb.lua:
----------------------------------------------------------------------------
whatis("Name : ior")
whatis("Version : 3.3.0")
depends_on("openmpi/5.0.5-x2kvx5l")
prepend_path("PATH","{IOR_PREFIX}/bin")
prepend_path("CMAKE_PREFIX_PATH","{IOR_PREFIX}/.")
"""


def fake_login(avail=AVAIL, spider=SPIDER_TERSE, detail=SPIDER_DETAIL, show=SHOW,
               lmod='/usr/share/lmod/lmod/libexec/lmod'):
    def login(script, args, timeout=None):
        if script is sc._MODULE_LIST_SCRIPT:
            text = f"\n@@LMOD={lmod}\n@@MODULEPATH={LMOD_TREE}/Core\n@@AVAIL\n{avail}"
            if lmod:
                text += f"\n@@SPIDER\n{spider}"
            return 0, text + "\n@@END\n"
        if script is sc._MODULE_SPIDER_SCRIPT:
            return 0, "".join(f"\n@@MOD={m}\n{detail}" for m in args) + "\n@@END\n"
        if script is sc._MODULE_SHOW_SCRIPT:
            names = args[0::2]
            return 0, "".join(f"\n@@MOD={m}\n{show}" for m in names) + "\n@@END\n"
        raise AssertionError(script)

    return login


def spack_find_json(version='3.3.0'):
    return json.dumps([{
        'name': 'ior', 'version': version, 'hash': IOR_HASH,
        'compiler': {'name': 'gcc', 'version': '11.4.0'},
        'parameters': {'build_system': 'autotools', 'cflags': [], 'hdf5': False},
        'dependencies': [{'name': 'openmpi', 'hash': MPI_HASH,
                          'parameters': {'deptypes': ['build', 'link'],
                                         'virtuals': ['mpi']}}],
    }])


SPACK_FIND_D = (f"{IOR_HASH} ior 3.3.0 {IOR_PREFIX}\n"
                f"    {MPI_HASH} openmpi 5.0.5 {MPI_PREFIX}\n")


def fake_spack(found=True, version='3.3.0'):
    def run(executable, args, root=None):
        if '--json' in args:
            return (0, spack_find_json(version)) if found else (3, '')
        return (0, SPACK_FIND_D) if found else (3, '')
    return run


SITE_SPACK = [{'root': '/site/spack', 'executable': '/site/spack/bin/spack'}]


class FakeJarvis:
    def __init__(self, root):
        self.root = root
        self.jarvis_root = os.path.join(root, 'jarvis')

    def get_pipeline_shared_dir(self, name):
        return os.path.join(self.root, 'shared', name)


class FakePipeline:
    def __init__(self, jarvis, packages):
        self.jarvis = jarvis
        self.packages = list(packages)
        self.env = {'PATH': '/usr/bin'}
        self.base_deploy_mode = None
        self.name = 'demo'
        self.container_image = ''

    def save(self):
        pass

    def is_containerized(self):
        return False


def spack_pkg(query, name='bench'):
    return {'pkg_type': f'builtin.{name}', 'pkg_id': name, 'pkg_name': name,
            'config': {'install_method': 'spack', 'install_query': query}}


class TestParsing(unittest.TestCase):
    def test_spack_version_semantics(self):
        cases = [
            ('3.3.0', '3.3', True), ('3.30', '3.3', False), ('3.3.0', '=3.3', False),
            ('3.3', '=3.3', True), ('5.0.5', '4:5', True), ('5.1', '4:5.0', False),
            ('2.0', '3:', False), ('develop', '3:', True), ('4.0', ':3', False),
            ('3.9.1', ':3', True), (None, '3', False),
        ]
        for version, constraint, expected in cases:
            self.assertEqual(sc.version_satisfies(version, constraint), expected,
                             (version, constraint))
        self.assertTrue(sc.versions_satisfied('3.3.0', []))
        self.assertTrue(sc.versions_satisfied('3.3.0', ['4', '3.3']))

    def test_needs_from_a_query(self):
        needs = sc.parse_needs('ior@3.3: +hdf5 api=posix ^openmpi@5 ^zlib@1.3')
        self.assertEqual(needs.name, 'ior')
        self.assertEqual(needs.root.versions, ['3.3:'])
        self.assertEqual(needs.root.variants, {'hdf5': True, 'api': 'posix'})
        mpi = needs.mpi
        assert mpi is not None
        self.assertEqual((mpi.name, mpi.versions), ('openmpi', ['5']))
        self.assertEqual(needs.to_dict()['depends_on'], ['zlib@1.3'])
        recorded = sc.parse_needs('ior', '^openmpi/abcdefg', '/opt/mpi')
        assert recorded.site_mpi is not None
        self.assertEqual(recorded.site_mpi.name, 'openmpi')
        self.assertEqual(recorded.site_mpi_prefix, '/opt/mpi')
        self.assertEqual(recorded.key(), 'ior [site mpi openmpi/abcdefg]')

    def test_module_names_and_versions(self):
        self.assertEqual(sc.module_version('3.3.0-fihmxeb'), ('3.3.0', 'fihmxeb'))
        self.assertEqual(sc.module_version('2023.1'), ('2023.1', None))
        self.assertEqual(sc._module_names(AVAIL.splitlines()),
                         ['openmpi/5.0.5-x2kvx5l/gcc/11.4.0/ior/3.3.0-fihmxeb'])
        self.assertEqual(sc._module_names(['  ior: ior/3.3.0-fihmxeb']), ['ior/3.3.0-fihmxeb'])
        entry = sc.module_entry('openmpi/5.0.5-x2kvx5l/gcc/11.4.0/ior/3.3.0-fihmxeb', 'ior')
        assert entry is not None
        self.assertEqual((entry['short'], entry['version'], entry['flavor']),
                         ('ior/3.3.0-fihmxeb', '3.3.0', []))
        self.assertIsNone(sc.module_entry('iozone/3.4', 'ior'))

    def test_spider_prereqs_and_show(self):
        self.assertEqual(sc.parse_spider_prereqs(SPIDER_DETAIL.splitlines()),
                         ['openmpi/5.0.5-x2kvx5l'])
        self.assertIsNone(sc.parse_spider_prereqs(['  ior: ior/3.3.0', '', '  Help:']))
        show = sc.parse_module_show(SHOW.splitlines())
        self.assertEqual(show['prefix'], IOR_PREFIX)
        self.assertEqual(show['loads'], ['openmpi/5.0.5-x2kvx5l'])
        self.assertTrue(show['file'].endswith('ior/3.3.0-fihmxeb.lua'))
        self.assertFalse(show['failed'])

    def test_spack_find_outputs(self):
        nodes = sc._spack_nodes(json.loads(spack_find_json()))
        self.assertEqual(nodes[0]['_deps'][0]['name'], 'openmpi')
        newer = sc._spack_nodes({'spec': {'nodes': [
            {'name': 'ior', 'hash': 'h1', 'dependencies': [{'name': 'mpich', 'hash': 'h2'}]},
            {'name': 'mpich', 'hash': 'h2', 'version': '4.1'}]}})
        self.assertEqual(newer[0]['_deps'][0]['version'], '4.1')
        self.assertEqual(sc.parse_spack_find_d(SPACK_FIND_D)[MPI_HASH]['prefix'], MPI_PREFIX)


class TestDiscovery(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.graph = Path(tmp.name) / 'resource_graph.yaml'

    def find(self, login=None, spack=None, roots=SITE_SPACK):
        with mock.patch.object(sc, '_login', side_effect=login or fake_login()), \
                mock.patch.object(sc, '_run_spack', side_effect=spack or fake_spack()):
            return sc.find_site_copies('ior', graph=self.graph, spack_roots=roots)

    def test_a_site_spack_module_is_one_copy_with_its_mpi(self):
        copies, searched = self.find()
        self.assertEqual(len(copies), 1, copies)
        copy = copies[0]
        self.assertEqual(copy['source'], 'module')
        self.assertEqual(copy['modules'], ['openmpi/5.0.5-x2kvx5l', 'ior/3.3.0-fihmxeb'])
        self.assertEqual((copy['version'], copy['prefix']), ('3.3.0', IOR_PREFIX))
        self.assertEqual(copy['implies']['mpi']['name'], 'openmpi')
        self.assertEqual(copy['implies']['mpi']['version'], '5.0.5')
        # the site Spack's record of the same install adds its variants
        self.assertEqual(copy['spack'], {'root': '/site/spack', 'hash': IOR_HASH})
        self.assertIs(copy['variants']['hdf5'], False)
        self.assertTrue(any('module -t spider ior' in item for item in searched))
        self.assertTrue(any('site Spack /site/spack' in item for item in searched))

    def test_no_module_and_no_spack_install_finds_nothing(self):
        copies, searched = self.find(fake_login(avail='', spider=''), fake_spack(found=False))
        self.assertEqual(copies, [])
        self.assertTrue(any('found none (exit 3)' in item for item in searched))

    def test_a_site_spack_install_without_a_module(self):
        copies, _ = self.find(fake_login(avail='', spider=''))
        self.assertEqual([c['source'] for c in copies], ['spack'])
        self.assertEqual(copies[0]['implies']['mpi']['prefix'], MPI_PREFIX)

    def test_no_module_command(self):
        def login(script, args, timeout=None):
            return 0, '\n@@NOMODULE\n'
        copies, searched = self.find(login, roots=[])
        self.assertEqual(copies, [])
        self.assertIn('modules: no `module` command in a login shell', searched)

    def test_the_resource_graph_software_section(self):
        self.graph.write_text(
            'software:\n'
            '- {name: ior, version: 3.3.0, label: "IOR (site build)",'
            ' modules: [openmpi/5.0.5-x2kvx5l, ior/3.3.0-fihmxeb], implies: {mpi: openmpi/5.0.5}}\n',
            encoding='utf-8')
        copies, _ = self.find(roots=[])
        self.assertEqual(len(copies), 1)
        self.assertEqual((copies[0]['source'], copies[0]['label']),
                         ('resource_graph', 'IOR (site build)'))
        self.assertEqual(copies[0]['discovered'], ['module'])


class TestRecommendation(unittest.TestCase):
    def copies(self):
        with mock.patch.object(sc, '_login', side_effect=fake_login()), \
                mock.patch.object(sc, '_run_spack', side_effect=fake_spack()):
            return sc.find_site_copies('ior', graph=Path('/nonexistent.yaml'),
                                       spack_roots=SITE_SPACK)[0]

    def recommend(self, query, site_mpi=None, prefix=None):
        return sc.candidates_for(self.copies(), sc.parse_needs(query, site_mpi, prefix))

    def test_a_fitting_copy_is_recommended(self):
        candidates, recommended, reason = self.recommend('ior@3.3')
        self.assertEqual(recommended, 'site-1')
        self.assertEqual([c['id'] for c in candidates], ['site-1', 'spack_build'])
        site = candidates[0]
        self.assertEqual(site['constraint'], 'module:ior/3.3.0-fihmxeb')
        self.assertTrue(site['fits'] and site['recommended'])
        self.assertIn('version 3.3.0 satisfies @3.3', site['why'])
        self.assertIn('it uses openmpi@5.0.5', site['why'])
        self.assertIn('module load openmpi/5.0.5-x2kvx5l ior/3.3.0-fihmxeb', site['does'])
        self.assertIn('fits', reason)

    def test_a_version_outside_the_range_recommends_spack(self):
        candidates, recommended, reason = self.recommend('ior@4:')
        self.assertEqual(recommended, 'spack_build')
        self.assertFalse(candidates[0]['fits'])
        self.assertIn('version 3.3.0 is outside @4:', reason)
        self.assertTrue(candidates[-1]['recommended'])

    def test_variants_and_mpi_must_match(self):
        _, recommended, reason = self.recommend('ior+hdf5')
        self.assertEqual(recommended, 'spack_build')
        self.assertIn('the package needs +hdf5', reason)
        _, recommended, reason = self.recommend('ior ^mpich')
        self.assertEqual(recommended, 'spack_build')
        self.assertIn('it uses openmpi, the package needs ^mpich', reason)
        self.assertEqual(self.recommend('ior ^openmpi@5:')[1], 'site-1')
        self.assertEqual(self.recommend('ior ^mpi')[1], 'site-1')

    def test_the_sites_recorded_mpi(self):
        self.assertEqual(self.recommend('ior', '^openmpi/zzzzzzz')[1], 'site-1')
        _, recommended, reason = self.recommend('ior', '^mpich/zzzzzzz')
        self.assertEqual(recommended, 'spack_build')
        self.assertIn("the site's recorded MPI is mpich/zzzzzzz", reason)
        # a module copy names no MPI prefix, so only name and version count
        self.assertEqual(self.recommend('ior', '^openmpi/zzzzzzz', '/elsewhere')[1], 'site-1')

    def test_a_site_spack_mpi_at_another_prefix_does_not_fit(self):
        copy = dict(self.copies()[0])
        copy['implies'] = {**copy['implies'],
                           'mpi': {'name': 'openmpi', 'version': '5.0.5', 'prefix': MPI_PREFIX}}
        fits, why = sc.evaluate(copy, sc.parse_needs('ior', '^openmpi/z', '/other/openmpi'))
        self.assertFalse(fits)
        self.assertIn('/other/openmpi', why)
        self.assertTrue(sc.evaluate(copy, sc.parse_needs('ior', '^openmpi/z', MPI_PREFIX))[0])


class _Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.jarvis = FakeJarvis(tmp.name)
        env = mock.patch.dict(os.environ, {
            'JARVIS_SITE_COPIES': 'on',
            'JARVIS_RESOURCE_GRAPH': str(self.tmp / 'site' / 'resource_graph.yaml'),
        })
        env.start()
        self.addCleanup(env.stop)

    def context(self, answers=None):
        context = InstallContext(install_dir=self.tmp / 'install',
                                 decisions_path=self.tmp / 'decisions.yaml',
                                 answers=answers or {})
        context.open()
        self.addCleanup(context.close)
        return context

    def resolve(self, ppl, context, login=None, spack=None):
        with mock.patch.object(sc, '_login', side_effect=login or fake_login()), \
                mock.patch.object(sc, '_run_spack', side_effect=spack or fake_spack()), \
                mock.patch.object(sc, 'site_spack_roots', return_value=SITE_SPACK):
            return sc.resolve_site_copies(ppl, context)


class TestResolution(_Case):
    def test_a_copy_is_a_question_and_holds_the_package(self):
        ppl = FakePipeline(self.jarvis, [spack_pkg('ior@3.3')])
        context = self.context()
        summary = self.resolve(ppl, context)
        self.assertEqual(summary['held'], ['bench'])
        issue = context.issues[0]
        self.assertEqual((issue['kind'], issue['scope'], issue['package']),
                         ('site_copy', 'package', 'ior'))
        self.assertTrue(issue['answerable'])
        self.assertEqual(issue['recommended'], 'site-1')
        self.assertTrue(issue['question'].startswith('This site already has 1 copy of ior'))
        self.assertEqual(issue['needs']['versions'], ['3.3'])
        self.assertEqual(ppl.packages[0]['config']['install_method'], 'spack')

    def test_choosing_the_copy_records_it_and_replays_without_a_search(self):
        ppl = FakePipeline(self.jarvis, [spack_pkg('ior@3.3')])
        first = self.context()
        self.resolve(ppl, first)
        issue_id = first.issues[0]['issue_id']

        ppl = FakePipeline(self.jarvis, [spack_pkg('ior@3.3')])
        summary = self.resolve(ppl, self.context({issue_id: {'constraint': 'site-1'}}))
        self.assertEqual(summary['packages']['bench']['status'], 'answered')
        config = ppl.packages[0]['config']
        self.assertEqual(config['install_method'], 'site')
        self.assertEqual(config['install_site']['modules'],
                         ['openmpi/5.0.5-x2kvx5l', 'ior/3.3.0-fihmxeb'])
        self.assertEqual(config['install_site_choice']['issue_id'], issue_id)
        entry = Decisions(self.tmp / 'decisions.yaml').package_entries('ior')[0]
        self.assertEqual((entry['kind'], entry['constraint'], entry['candidate_id']),
                         ('site_copy', 'module:ior/3.3.0-fihmxeb', 'site-1'))

        def no_search(*_args, **_kwargs):
            raise AssertionError('a recorded answer must not search again')

        ppl = FakePipeline(self.jarvis, [spack_pkg('ior@3.3')])
        summary = self.resolve(ppl, self.context(), login=no_search, spack=no_search)
        self.assertEqual(summary['packages']['bench']['status'], 'recorded')
        self.assertEqual(ppl.packages[0]['config']['install_method'], 'site')
        # different needs are a new question
        ppl = FakePipeline(self.jarvis, [spack_pkg('ior@3.3 ^openmpi')])
        context = self.context()
        self.resolve(ppl, context)
        self.assertEqual(context.issues[0]['supersedes'], issue_id)

    def test_forgetting_the_answer_resets_the_saved_choice(self):
        from jarvis_cd.core.spack_install import forget_decisions

        ppl = FakePipeline(self.jarvis, [spack_pkg('ior@3.3')])
        first = self.context()
        self.resolve(ppl, first)
        issue_id = first.issues[0]['issue_id']
        ppl = FakePipeline(self.jarvis, [spack_pkg('ior@3.3')])
        self.resolve(ppl, self.context({issue_id: {'constraint': 'site-1'}}))
        forgotten = forget_decisions(self.tmp / 'decisions.yaml', ['ior'])
        self.assertEqual(sc.forget_choices(ppl, forgotten['forgotten']), ['bench'])
        config = ppl.packages[0]['config']
        self.assertEqual(config['install_method'], 'spack')
        self.assertNotIn('install_site', config)
        self.assertTrue(sc.needs_search(ppl, ppl.packages[0]))

    def test_nothing_found_is_a_fact_and_spack_runs(self):
        ppl = FakePipeline(self.jarvis, [spack_pkg('lammps', name='md')])
        context = self.context()
        summary = self.resolve(ppl, context, fake_login(avail='', spider=''),
                               fake_spack(found=False))
        self.assertEqual(summary['held'], [])
        self.assertEqual(context.issues, [])
        fact = summary['facts'][0]
        self.assertEqual((fact['kind'], fact['answerable'], fact['method']),
                         ('site_copy_none', False, 'spack'))
        self.assertTrue(fact['question'].startswith('No site copy of lammps was found'))
        self.assertIn('lammps', context.site_copy_facts['md'])
        self.assertEqual(ppl.packages[0]['config'], spack_pkg('lammps', name='md')['config'])

    def test_the_search_can_be_turned_off(self):
        package = spack_pkg('ior')
        self.assertTrue(sc.needs_search(None, package))
        package['config']['install_site_search'] = False
        self.assertFalse(sc.needs_search(None, package))
        with mock.patch.dict(os.environ, {'JARVIS_SITE_COPIES': 'off'}):
            self.assertFalse(sc.needs_search(None, spack_pkg('ior')))
        self.assertFalse(sc.needs_search(None, {'config': {'install_method': 'pip',
                                                           'install_query': 'rich'}}))

    def test_install_all_holds_the_package_and_raises_the_question(self):
        ppl = FakePipeline(self.jarvis, [spack_pkg('ior@3.3')])
        with mock.patch.object(sc, '_login', side_effect=fake_login()), \
                mock.patch.object(sc, '_run_spack', side_effect=fake_spack()), \
                mock.patch.object(sc, 'site_spack_roots', return_value=SITE_SPACK), \
                mock.patch('jarvis_cd.core.installer.SpackInstaller.check',
                           side_effect=AssertionError('held: Spack must not plan it')):
            with self.assertRaises(InstallDecisionNeeded) as caught:
                Installer.install_all(ppl, mode='check', context=self.context())
        self.assertEqual(caught.exception.issues[0]['kind'], 'site_copy')
        report = cast(Any, caught.exception).report
        self.assertEqual(report['site_copies']['held'], ['bench'])


def env0(env):
    return ''.join(f'{key}={value}\0' for key, value in env.items())


class TestSiteMethod(_Case):
    def site_pkg(self):
        package = spack_pkg('ior@3.3')
        sc.apply_choice(package, 'module:ior/3.3.0-fihmxeb',
                        {'source': 'module', 'modules': ['openmpi/5.0.5-x2kvx5l',
                                                         'ior/3.3.0-fihmxeb'],
                         'prefix': str(self.tmp), 'constraint': 'module:ior/3.3.0-fihmxeb'},
                        'the person', 'site_copy-x')
        return package

    def test_the_site_method_loads_the_modules_into_the_pipeline_env(self):
        before = {'PATH': '/usr/bin', 'HOME': '/h'}
        after = {'PATH': f'{IOR_PREFIX}/bin:/mpi/bin:/usr/bin', 'HOME': '/h',
                 'LOADEDMODULES': 'openmpi/5.0.5-x2kvx5l:ior/3.3.0-fihmxeb'}
        out = env0(before) + '\0@@SPLIT@@\0' + env0(after)
        ppl = FakePipeline(self.jarvis, [self.site_pkg()])
        self.assertEqual(Installer.get_installers(ppl), {'site': ppl.packages})
        installer = SiteInstaller()
        installer.context = cast(Any, self.context())
        with mock.patch.object(sc.subprocess, 'run',
                               return_value=subprocess.CompletedProcess([], 0, out, '')) as run:
            result = installer.check(ppl, ppl.packages)
        argv = run.call_args.args[0]
        self.assertEqual(argv[5], 'openmpi/5.0.5-x2kvx5l ior/3.3.0-fihmxeb')
        self.assertEqual(result['status'], 'satisfied')
        self.assertEqual(ppl.env['PATH'], f'{IOR_PREFIX}/bin:/mpi/bin:/usr/bin')
        self.assertEqual(ppl.env['LOADEDMODULES'], 'openmpi/5.0.5-x2kvx5l:ior/3.3.0-fihmxeb')
        self.assertEqual(result['packages']['bench']['env_changed'], ['LOADEDMODULES', 'PATH'])

    def test_a_module_that_does_not_load_is_the_failure_record(self):
        ppl = FakePipeline(self.jarvis, [self.site_pkg()])
        installer = SiteInstaller()
        context = self.context()
        installer.context = cast(Any, context)
        failed = subprocess.CompletedProcess(
            [], 3, '', 'Lmod has detected the following error: ior/3.3.0 not found\n')
        with mock.patch.object(sc.subprocess, 'run', return_value=failed):
            result = installer.install(ppl, ppl.packages)
        self.assertEqual(result['status'], 'failed')
        issue = context.issues[-1]
        self.assertEqual((issue['kind'], issue['answerable'], issue['stage']),
                         ('failure', False, 'site_copy_load'))
        self.assertEqual(result['packages']['bench']['issue_id'], issue['issue_id'])
        self.assertEqual((issue['failing_kind'], issue['load_step'], issue['exit_code']),
                         ('module', 'module load', 3))
        self.assertIn('module load openmpi/5.0.5-x2kvx5l ior/3.3.0-fihmxeb', issue['command'])
        self.assertNotIn('spack', issue['command'])
        self.assertEqual(issue['packages'], ['bench'])
        self.assertEqual(issue['candidates'], [])
        self.assertIn('Lmod has detected', issue['output_tail'])
        self.assertIn('Lmod has detected',
                      Path(issue['output_path']).read_text(encoding='utf-8'))
        self.assertEqual(issue['site']['modules'], ['openmpi/5.0.5-x2kvx5l', 'ior/3.3.0-fihmxeb'])
        self.assertNotIn('jarvis ppl', issue['question'])

    def test_install_modules_in_the_config(self):
        site = sc.site_of({'config': {'install_method': 'site',
                                      'install_modules': 'gcc/12 hdf5/1.14',
                                      'install_modulepath': '/opt/modules'}})
        self.assertEqual(site['modules'], ['gcc/12', 'hdf5/1.14'])
        self.assertEqual(site['modulepath'], '/opt/modules')


class TestSiteCopyLoadFailure(_Case):
    """A chosen copy that does not load here: the error, never a question."""

    def choose(self, constraint, site):
        package = spack_pkg('ior@3.3')
        sc.apply_choice(package, constraint, site, 'the person', 'site_copy-x')
        return package

    def verify(self, ppl, context, returncode, stderr):
        failed = subprocess.CompletedProcess([], returncode, '', stderr)
        with mock.patch.object(sc.subprocess, 'run', return_value=failed):
            return sc.verify_site_choices(ppl, context)

    def spack_copy(self):
        constraint = f'spack:/{IOR_HASH[:7]}'
        return self.choose(constraint, {
            'source': 'spack', 'spack_root': '/site/spack', 'spack_hash': IOR_HASH,
            'constraint': constraint, 'label': 'ior 3.3.0 in the site Spack /site/spack'})

    def test_a_spack_copy_names_its_spack_load(self):
        ppl = FakePipeline(self.jarvis, [self.spack_copy()])
        context = self.context()
        error = '==> Error: cannot unpack non-iterable NoneType object'
        summary = self.verify(ppl, context, 5, error)
        self.assertEqual(summary['held'], ['bench'])
        issue = context.issues[-1]
        self.assertEqual(summary['packages']['bench'],
                         {'status': 'failed', 'issue_id': issue['issue_id'],
                          'choice': f'spack:/{IOR_HASH[:7]}'})
        self.assertEqual((issue['kind'], issue['answerable'], issue['failing_kind'],
                          issue['load_step']), ('failure', False, 'spack', 'spack load'))
        self.assertIn(f'load /{IOR_HASH}', issue['command'])
        self.assertNotIn('module load', issue['command'])
        self.assertEqual(issue['site']['spack_root'], '/site/spack')
        self.assertEqual(issue['paths']['spack_root'], '/site/spack')
        self.assertIn(error, issue['output_tail'])

    def test_an_answer_to_the_record_changes_nothing(self):
        ppl = FakePipeline(self.jarvis, [self.spack_copy()])
        context = self.context()
        self.verify(ppl, context, 5, '==> Error: boom')
        issue_id = context.issues[-1]['issue_id']
        ppl = FakePipeline(self.jarvis, [self.spack_copy()])
        context = self.context({issue_id: {'constraint': 'spack_build'}})
        summary = self.verify(ppl, context, 5, '==> Error: boom')
        self.assertEqual(summary['held'], ['bench'])
        self.assertEqual(context.issues[-1]['issue_id'], issue_id)
        self.assertEqual(ppl.packages[0]['config']['install_method'], 'site')

    def test_a_prefix_missing_here_is_the_record_without_a_load(self):
        package = self.choose('module:ior/3.3.0-fihmxeb', {
            'source': 'module', 'modules': ['ior/3.3.0-fihmxeb'],
            'prefix': str(self.tmp / 'gone'), 'constraint': 'module:ior/3.3.0-fihmxeb'})
        ppl = FakePipeline(self.jarvis, [package])
        context = self.context()
        with mock.patch.object(sc.subprocess, 'run',
                               side_effect=AssertionError('not loaded')):
            summary = sc.verify_site_choices(ppl, context)
        self.assertEqual(summary['held'], ['bench'])
        issue = context.issues[-1]
        self.assertEqual((issue['kind'], issue['exit_code'], issue['command']),
                         ('failure', None, None))
        self.assertIn('is not a directory on this host', issue['error'])

    def test_install_all_exits_6_with_the_record(self):
        package = spack_pkg('ior@3.3')
        sc.apply_choice(package, 'module:ior/3.3.0-fihmxeb',
                        {'source': 'module', 'modules': ['ior/3.3.0-fihmxeb'],
                         'constraint': 'module:ior/3.3.0-fihmxeb'}, 'the person', 'x')
        ppl = FakePipeline(self.jarvis, [package])
        failed = subprocess.CompletedProcess([], 3, '', 'Lmod has detected the following error')
        with mock.patch.object(sc.subprocess, 'run', return_value=failed), \
                mock.patch('jarvis_cd.core.installer.SiteInstaller.check',
                           side_effect=AssertionError('held: the site method must not run')):
            with self.assertRaises(InstallFailed) as caught:
                Installer.install_all(ppl, mode='check', context=self.context())
        self.assertEqual(caught.exception.exit_code, 6)
        self.assertEqual(caught.exception.issues[0]['kind'], 'failure')
        report = cast(Any, caught.exception).report
        self.assertEqual(report['site_copies']['held'], ['bench'])
        self.assertEqual(caught.exception.to_dict()['failures'],
                         [caught.exception.issues[0]['issue_id']])

    def test_a_copy_set_in_the_pipeline_config_fails_the_same_way(self):
        package = {'pkg_type': 'builtin.bench', 'pkg_id': 'bench', 'pkg_name': 'bench',
                   'config': {'install_method': 'site', 'install_modules': 'ior/9'}}
        ppl = FakePipeline(self.jarvis, [package])
        context = self.context()
        summary = self.verify(ppl, context, 3, 'Lmod has detected the following error')
        self.assertEqual(summary['held'], ['bench'])
        issue = context.issues[-1]
        self.assertEqual((issue['kind'], issue['answerable'], issue['failing_kind']),
                         ('failure', False, 'module'))
        self.assertEqual(issue['candidates'], [])


def broken_spack_run():
    error = '==> Error: cannot unpack non-iterable NoneType object'

    def run(executable, args, env, timeout=None):
        if env.get('SPACK_DISABLE_LOCAL_CONFIG') == '1':
            return 0, 'mpi: openmpi\n', ''
        return 3, '', error

    return run


class TestBrokenSiteSpack(_Case):
    def setUp(self):
        super().setUp()
        from jarvis_cd.core import spack_health

        self.health = spack_health
        env = mock.patch.dict(os.environ, {'JARVIS_SPACK_HEALTH': 'on',
                                           'SPACK_USER_CONFIG_PATH': '/home/u/.spack'})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop('SPACK_DISABLE_LOCAL_CONFIG', None)
        spack_health.reset()
        self.addCleanup(spack_health.reset)

    def test_a_broken_site_spack_holds_the_package_and_offers_nothing_from_it(self):
        ppl = FakePipeline(self.jarvis, [spack_pkg('ior@3.3')])
        context = self.context()
        with mock.patch.object(self.health, '_run', side_effect=broken_spack_run()):
            summary = self.resolve(ppl, context)
        self.assertEqual(summary['held'], ['bench'])
        self.assertEqual((summary['packages']['bench']['status'],
                          summary['packages']['bench']['blocked_by']),
                         ('failed', 'spack_health'))
        self.assertEqual([(i['kind'], i['stage']) for i in context.issues],
                         [('failure', 'spack_health')])
        issue = context.issues[0]
        self.assertEqual(issue['spack_root'], self.health.root_key('/site/spack'))
        self.assertFalse(issue['answerable'])
        self.assertEqual(summary['packages']['bench']['issues'], [issue['issue_id']])

        # the caller's own Spack setting (here: no user config) is used as is
        ppl = FakePipeline(self.jarvis, [spack_pkg('ior@3.3')])
        context = self.context()
        with mock.patch.dict(os.environ, {'SPACK_DISABLE_LOCAL_CONFIG': '1'}), \
                mock.patch.object(self.health, '_run', side_effect=broken_spack_run()):
            summary = self.resolve(ppl, context)
        self.assertEqual(summary['packages']['bench']['status'], 'question')
        self.assertEqual([i['kind'] for i in context.issues], ['site_copy'])

    def test_a_spack_copy_of_a_broken_spack_is_held_before_loading(self):
        constraint = f'spack:/{IOR_HASH[:7]}'
        package = spack_pkg('ior@3.3')
        sc.apply_choice(package, constraint, {
            'source': 'spack', 'spack_root': '/site/spack', 'spack_hash': IOR_HASH,
            'constraint': constraint}, 'the person', 'x')
        ppl = FakePipeline(self.jarvis, [package])
        context = self.context()
        with mock.patch.object(self.health, '_run', side_effect=broken_spack_run()), \
                mock.patch.object(sc.subprocess, 'run',
                                  side_effect=AssertionError('not loaded while broken')):
            summary = sc.verify_site_choices(ppl, context)
        self.assertEqual(summary['packages']['bench']['status'], 'failed')
        self.assertEqual([(i['kind'], i['stage']) for i in context.issues],
                         [('failure', 'spack_health')])
        self.assertEqual(context.issues[0]['packages'], ['bench'])


class TestResourceGraphSoftware(unittest.TestCase):
    def test_the_software_section_round_trips(self):
        from jarvis_cd.util.resource_graph import ResourceGraph

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'graph.yaml'
            graph = ResourceGraph()
            graph.software = [{'name': 'ior', 'version': '3.3.0', 'modules': ['ior/3.3.0']}]
            graph.save_to_file(path)
            loaded = ResourceGraph()
            loaded.load_from_file(path)
            self.assertEqual(loaded.software, graph.software)


if __name__ == '__main__':
    unittest.main()
