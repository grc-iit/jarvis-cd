"""Find-first pip, conda and container installs, and the install method choice.

No tool runs: ``run_logged`` and the host probes are replaced by fakes that
act on a temp directory the way pip, conda and podman would.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

from jarvis_cd.core import method_install as mi
from jarvis_cd.core.installer import (
    CondaInstaller,
    ContainerInstaller,
    Installer,
    PipInstaller,
)
from jarvis_cd.core.spack_install import (
    InstallContext,
    InstallDecisionNeeded,
    InstallFailed,
)


class FakeJarvis:
    def __init__(self, root):
        self.root = root
        self.jarvis_root = os.path.join(root, 'jarvis')

    def get_pipeline_shared_dir(self, name):
        return os.path.join(self.root, 'shared', name)

    def get_containers_dir(self):
        return Path(self.root) / 'containers'


class FakePipeline:
    def __init__(self, jarvis, packages=(), **extras):
        self.jarvis = jarvis
        self.packages = list(packages)
        self.env = {'PATH': '/usr/bin'}
        self.base_deploy_mode = None
        self.name = 'demo'
        self.container_image = ''
        self.container_uri = extras.get('container_uri', '')
        self.container_engine = extras.get('container_engine', 'podman')
        self.container_engine_source = extras.get('source', 'yaml')
        self.saved = False
        self.generated_yaml = False
        self.generated_compose = False

    def save(self):
        self.saved = True

    def is_containerized(self):
        return bool(self.container_image)

    def _generate_pipeline_container_yaml(self):
        self.generated_yaml = True

    def _generate_pipeline_compose_file(self):
        self.generated_compose = True


def pkg(method, query, name='p', **config):
    config.update(install_method=method, install_query=query)
    return {'pkg_type': f'builtin.{name}', 'pkg_id': name, 'pkg_name': name,
            'config': config}


def done(argv, out='', rc=0):
    return subprocess.CompletedProcess(list(argv), rc, out, None)


class _Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.jarvis = FakeJarvis(tmp.name)
        env = mock.patch.dict(os.environ, {
            'JARVIS_RESOURCE_GRAPH': str(self.tmp / 'site' / 'resource_graph.yaml'),
            'XDG_RUNTIME_DIR': str(self.tmp),
        })
        env.start()
        self.addCleanup(env.stop)
        self.context = self.new_context()

    def new_context(self, answers=None):
        context = InstallContext(install_dir=self.tmp / 'install',
                                 decisions_path=self.tmp / 'decisions.yaml',
                                 answers=answers or {})
        context.open()
        self.addCleanup(context.close)
        return context

    def kinds(self, context=None):
        return [issue['kind'] for issue in (context or self.context).issues]


class FakePip:
    """pip in a venv: ``-m venv`` makes it, ``pip install`` records names."""

    def __init__(self, fail_install=False):
        self.installed = {}
        self.calls = []
        self.fail_install = fail_install

    def __call__(self, argv, context, env=None, timeout=None):
        argv = [str(item) for item in argv]
        self.calls.append(argv)
        if argv[1:3] == ['-m', 'venv']:
            python = Path(argv[3]) / 'bin' / 'python'
            python.parent.mkdir(parents=True)
            python.write_text('')
            return done(argv)
        if argv[1:4] == ['-m', 'pip', 'install']:
            if self.fail_install:
                return done(argv, 'ERROR: No matching distribution', 1)
            for token in argv[4:]:
                if not token.startswith('-'):
                    self.installed[token.split('==')[0]] = '1.0'
            return done(argv)
        if argv[1] == '-c':
            import json
            out = []
            for requirement in json.loads(argv[3]):
                name = requirement.split('==')[0]
                status = 'satisfied' if name in self.installed else 'missing'
                out.append({'requirement': requirement, 'status': status})
            return done(argv, json.dumps(out))
        raise AssertionError(f'unexpected command {argv}')


PYTHON = [{'id': 'python-1', 'constraint': '/usr/bin/python3', 'name': 'python3',
           'version': '3.10.12', 'source': 'PATH'}]


class TestPip(_Case):
    def run_pip(self, ppl, mode, fake, pythons=PYTHON, context=None):
        with mock.patch.object(mi, 'run_logged', fake), \
                mock.patch.object(mi, 'find_pythons', return_value=pythons):
            return mi.pip_run(ppl, ppl.packages, context or self.context, mode)

    def test_check_reports_missing_without_installing(self):
        ppl = FakePipeline(self.jarvis, [pkg('pip', 'numpy', 'a')])
        fake = FakePip()
        result = self.run_pip(ppl, 'check', fake)
        self.assertEqual(result['status'], 'missing')
        self.assertEqual(self.kinds(), ['not_installed'])
        self.assertEqual(self.context.issues[0]['method'], 'pip')
        self.assertEqual(fake.calls, [])

    def test_install_makes_venv_under_jarvis_root_and_prepends_path(self):
        ppl = FakePipeline(self.jarvis, [pkg('pip', 'numpy', 'a'),
                                         pkg('pip', 'rich==13.7', 'b')])
        fake = FakePip()
        result = self.run_pip(ppl, 'install', fake)
        prefix = Path(self.jarvis.jarvis_root) / 'envs' / 'pip' / 'demo'
        self.assertEqual(result['status'], 'installed')
        self.assertEqual(result['env'], str(prefix))
        self.assertEqual(fake.calls[0][:3], ['/usr/bin/python3', '-m', 'venv'])
        installs = [call for call in fake.calls if call[1:4] == ['-m', 'pip', 'install']]
        self.assertEqual([call[-1] for call in installs], ['numpy', 'rich==13.7'])
        self.assertTrue(installs[0][0].startswith(str(prefix)))
        self.assertEqual(ppl.env['PATH'].split(os.pathsep)[0], str(prefix / 'bin'))
        self.assertEqual(ppl.env['VIRTUAL_ENV'], str(prefix))
        self.assertEqual(self.context.issues, [])
        # find-first: a second install has nothing to do
        fake.calls.clear()
        again = self.run_pip(ppl, 'install', fake, context=self.new_context())
        self.assertEqual(again['status'], 'satisfied')
        self.assertFalse([call for call in fake.calls if 'install' in call])
        self.assertEqual(ppl.env['PATH'].count(str(prefix / 'bin')), 1)

    def test_install_env_names_the_venv(self):
        ppl = FakePipeline(self.jarvis, [pkg('pip', 'numpy', 'a', install_env='tools')])
        result = self.run_pip(ppl, 'install', FakePip())
        self.assertTrue(result['env'].endswith(os.path.join('envs', 'pip', 'tools')))

    def test_mpi_is_refused(self):
        ppl = FakePipeline(self.jarvis, [pkg('pip', 'mpich==4.2', 'a')])
        fake = FakePip()
        result = self.run_pip(ppl, 'install', fake)
        self.assertEqual(result['status'], 'refused')
        self.assertEqual(self.kinds(), ['mpi_from_package_manager'])
        self.assertFalse(self.context.issues[0]['answerable'])
        self.assertEqual(fake.calls, [])

    def test_no_python_is_a_question_not_a_default(self):
        ppl = FakePipeline(self.jarvis, [pkg('pip', 'numpy', 'a')])
        result = self.run_pip(ppl, 'install', FakePip(), pythons=[])
        self.assertEqual(result['status'], 'needs_decision')
        issue = self.context.issues[0]
        self.assertEqual((issue['kind'], issue['scope'], issue['answerable']),
                         ('python_missing', 'site', True))

    def test_jarvis_python_is_found_first_and_used(self):
        def which(name):
            return '/usr/bin/python3' if name == 'python3' else None

        with mock.patch.object(mi, 'jarvis_python', return_value='/site/uv/python3.12'), \
                mock.patch.object(mi.shutil, 'which', side_effect=which), \
                mock.patch.object(mi.os.path, 'realpath', side_effect=lambda p: p), \
                mock.patch.object(mi.subprocess, 'run', return_value=done([], '3.12.7')):
            found = mi.find_pythons()
        self.assertEqual([(item['name'], item['constraint']) for item in found],
                         [('jarvis', '/site/uv/python3.12'), ('python3', '/usr/bin/python3')])
        ppl = FakePipeline(self.jarvis, [pkg('pip', 'numpy', 'a')])
        fake = FakePip()
        self.run_pip(ppl, 'install', fake, pythons=found)
        self.assertEqual(fake.calls[0][:3], ['/site/uv/python3.12', '-m', 'venv'])

    def held_job_question(self, phase='needs_decision'):
        """An install job that stopped on a compute node's Python question."""
        import json
        job = self.tmp / 'shared' / 'demo' / 'executions' / 'e1' / 'install'
        job.mkdir(parents=True)
        question = {'schema': 'jarvis.install-issue.v1', 'issue_id': 'sha256:py',
                    'kind': 'python_missing', 'method': 'pip', 'scope': 'site',
                    'package': '*', 'answerable': True,
                    'candidates': [{'id': 'python-1', 'constraint': '/site/uv/python3'}]}
        (job / 'issues.jsonl').write_text(json.dumps(question) + '\n')
        shared = self.tmp / 'shared' / 'demo' / 'install'
        shared.mkdir(parents=True, exist_ok=True)
        (shared / 'last-install.json').write_text(json.dumps({
            'schema_version': 'jarvis.install-pointer.v1', 'execution_id': 'e1',
            'phase': phase, 'record': str(job / 'install-record.json'),
            'issues': str(job / 'issues.jsonl'), 'log': str(job / 'install.log')}))
        return question

    def check_all(self, ppl, context):
        with mock.patch.object(mi, 'run_logged', FakePip()), \
                mock.patch.object(mi, 'find_pythons', return_value=PYTHON):
            with self.assertRaises(InstallDecisionNeeded) as caught:
                Installer.install_all(ppl, mode='check', context=context)
        return caught.exception.report

    def test_check_keeps_an_install_jobs_question_this_host_cannot_raise(self):
        question = self.held_job_question()
        ppl = FakePipeline(self.jarvis, [pkg('pip', 'cowsay', 'a')])
        report = self.check_all(ppl, self.new_context())
        held = [issue for issue in report['issues'] if issue['issue_id'] == 'sha256:py']
        self.assertEqual(len(held), 1)
        self.assertEqual((held[0]['raised_by'], held[0]['execution_id']),
                         ('install_job', 'e1'))
        self.assertIn('sha256:py', report['open_issues'])
        self.assertFalse(report['satisfied'])
        self.assertFalse(report['last_install']['record_readable'])
        # this round's answer resolves it, so the check asks it no more
        answered = self.check_all(
            ppl, self.new_context({question['issue_id']: {'constraint': '/site/uv/python3'}}))
        self.assertEqual([issue['kind'] for issue in answered['issues']], ['not_installed'])
        self.assertEqual(answered['last_install']['resolved_issues'][0]['resolved_by'],
                         'answer')

    def test_a_finished_install_holds_no_question(self):
        self.held_job_question(phase='done')
        ppl = FakePipeline(self.jarvis, [pkg('pip', 'cowsay', 'a')])
        report = self.check_all(ppl, self.new_context())
        self.assertEqual([issue['kind'] for issue in report['issues']], ['not_installed'])

    def test_failed_pip_is_typed(self):
        ppl = FakePipeline(self.jarvis, [pkg('pip', 'nope', 'a')])
        with self.assertRaises(InstallFailed) as caught:
            self.run_pip(ppl, 'install', FakePip(fail_install=True))
        self.assertEqual(caught.exception.issues[0]['kind'], 'pip_install_failed')
        self.assertIn('No matching distribution', caught.exception.issues[0]['detail'])
        # the fields Spack's typed failures carry, so relay reads every method alike
        failure = caught.exception.issues[0]
        self.assertEqual((failure['stage'], failure['packages']), ('install', ['a']))
        self.assertTrue(failure['message'] and failure['remedy'] and failure['host'])
        self.assertIn('No matching distribution', failure['log_tail'])

    def test_installer_class_goes_through_install_all(self):
        ppl = FakePipeline(self.jarvis, [pkg('pip', 'numpy', 'a')])
        with mock.patch.object(mi, 'run_logged', FakePip()), \
                mock.patch.object(mi, 'find_pythons', return_value=PYTHON):
            with self.assertRaises(InstallDecisionNeeded) as caught:
                Installer.install_all(ppl, mode='check', context=self.context)
            self.assertEqual(caught.exception.report['installers']['pip']['status'],
                             'missing')
            report = Installer.install_all(ppl, mode='install', context=self.new_context())
        self.assertTrue(report['satisfied'])
        self.assertIsInstance(Installer.for_method('pip'), PipInstaller)


class FakeConda:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, context, env=None, timeout=None):
        argv = [str(item) for item in argv]
        self.calls.append(argv)
        if argv[1] in ('create', 'install'):
            (Path(argv[argv.index('-p') + 1]) / 'conda-meta').mkdir(parents=True,
                                                                    exist_ok=True)
            return done(argv)
        raise AssertionError(f'unexpected command {argv}')


class TestConda(_Case):
    def setUp(self):
        super().setUp()
        self.installed = set()
        self.fake = FakeConda()

    def probe(self, exe, prefix, specs, context):
        return [{'requirement': spec,
                 'status': 'satisfied' if spec in self.installed else 'missing'}
                for spec in specs]

    def run_conda(self, ppl, mode, condas, context=None):
        def record(argv, context, env=None, timeout=None):
            result = self.fake(argv, context)
            self.installed.update(token for token in argv[5:] if not token.startswith('-'))
            return result

        with mock.patch.object(mi, 'run_logged', record), \
                mock.patch.object(mi, 'find_condas', return_value=condas), \
                mock.patch.object(mi, '_conda_probe', self.probe):
            return mi.conda_run(ppl, ppl.packages, context or self.context, mode)

    def test_no_conda_asks_to_install_miniforge_in_the_site_home(self):
        ppl = FakePipeline(self.jarvis, [pkg('conda', 'zstd', 'a')])
        result = self.run_conda(ppl, 'check', [])
        self.assertEqual(result['status'], 'needs_decision')
        issue = self.context.issues[0]
        self.assertEqual((issue['kind'], issue['scope']), ('conda_missing', 'site'))
        candidate = issue['candidates'][0]
        self.assertEqual(candidate['id'], 'miniforge')
        self.assertEqual(Path(candidate['root']),
                         (self.tmp / 'site').resolve() / 'miniforge3')
        self.assertIn('Miniforge3-', candidate['url'])

        # the person confirms: the check then asks for an install job ...
        answers = {issue['issue_id']: {'constraint': 'miniforge'}}
        checked = self.new_context(answers)
        self.assertEqual(self.run_conda(ppl, 'check', [], checked)['status'], 'missing')
        self.assertEqual(self.kinds(checked), ['not_installed'])
        self.assertIn('miniforge', checked.issues[0]['missing'])

        # ... and the job (the answer now recorded) installs Miniforge, then the env
        def bootstrap(context, exe):
            exe.parent.mkdir(parents=True)
            exe.write_text('')

        with mock.patch.object(mi, 'install_miniforge', side_effect=bootstrap) as boot:
            result = self.run_conda(ppl, 'install', [], self.new_context())
        boot.assert_called_once()
        self.assertEqual(result['status'], 'installed')
        prefix = Path(self.jarvis.jarvis_root) / 'envs' / 'conda' / 'demo'
        self.assertEqual(self.fake.calls[0][1:5], ['create', '-y', '-p', str(prefix)])
        self.assertEqual(ppl.env['CONDA_PREFIX'], str(prefix))

    def test_channels_override_the_defaults(self):
        exe = self.tmp / 'conda' / 'bin' / 'conda'
        exe.parent.mkdir(parents=True)
        exe.write_text('')
        ppl = FakePipeline(self.jarvis, [pkg('conda', 'zstd', 'a',
                                             install_channels=['conda-forge'])])
        condas = [{'id': 'conda-1', 'constraint': str(exe)}]
        self.run_conda(ppl, 'install', condas)
        self.assertEqual(self.fake.calls[0][5:9],
                         ['-c', 'conda-forge', '--override-channels', 'zstd'])

    def test_several_condas_are_a_question(self):
        condas = [{'id': 'conda-1', 'constraint': '/a/bin/conda'},
                  {'id': 'conda-2', 'constraint': '/b/bin/conda'}]
        ppl = FakePipeline(self.jarvis, [pkg('conda', 'zstd', 'a')])
        self.assertEqual(self.run_conda(ppl, 'install', condas)['status'],
                         'needs_decision')
        self.assertEqual(self.kinds(), ['conda_select'])

    def test_conda_mpi_and_mpi4py_are_refused(self):
        ppl = FakePipeline(self.jarvis, [pkg('conda', 'conda-forge::openmpi', 'a'),
                                         pkg('conda', 'mpi4py', 'b')])
        result = self.run_conda(ppl, 'install', [])
        self.assertEqual(result['status'], 'refused')
        self.assertEqual(self.kinds(), ['mpi_from_package_manager'] * 2)
        self.assertEqual(self.fake.calls, [])

    def test_installer_class(self):
        self.assertIsInstance(Installer.for_method('conda'), CondaInstaller)


class TestContainer(_Case):
    def install(self, ppl, mode='install', present=True, pull_rc=0, context=None):
        calls = []

        def run(argv, context, env=None, timeout=None):
            calls.append([str(item) for item in argv])
            return done(argv, 'pull output', pull_rc)

        state = {'present': present}

        def image_present(ppl, engine, image):
            return state['present'] or (bool(calls) and pull_rc == 0)

        installer = ContainerInstaller()
        installer.context = context or self.context
        with mock.patch.object(mi, 'run_logged', run), \
                mock.patch.object(mi, 'image_present', image_present), \
                mock.patch('shutil.which', return_value='/usr/bin/podman'):
            if mode == 'check':
                return installer.check(ppl, ppl.packages), calls
            return installer.install(ppl, ppl.packages), calls

    def test_present_image_is_satisfied_in_check(self):
        ppl = FakePipeline(self.jarvis, [pkg('container', '', 'app')],
                           container_uri='docker.io/example/foo:1')
        result, calls = self.install(ppl, 'check')
        self.assertEqual(result['status'], 'satisfied')
        self.assertEqual(calls, [])
        self.assertEqual(ppl.container_image, 'docker.io/example/foo:1')
        self.assertTrue(ppl.generated_compose)
        self.assertEqual(ppl.env['XDG_RUNTIME_DIR'], str(self.tmp))

    def test_missing_image_is_not_installed_in_check(self):
        ppl = FakePipeline(self.jarvis, [pkg('container', 'docker.io/example/foo:1', 'app')])
        result, calls = self.install(ppl, 'check', present=False)
        self.assertEqual(result['status'], 'missing')
        self.assertEqual(self.kinds(), ['not_installed'])
        self.assertEqual(calls, [])

    def test_install_pulls_the_query_image(self):
        ppl = FakePipeline(self.jarvis, [pkg('container', 'docker.io/example/foo:1', 'app')])
        result, calls = self.install(ppl, present=False)
        self.assertEqual(result['status'], 'installed')
        self.assertEqual(calls, [['podman', 'pull', 'docker.io/example/foo:1']])
        self.assertEqual(ppl.container_image, 'docker.io/example/foo:1')

    def test_failed_pull_is_typed(self):
        ppl = FakePipeline(self.jarvis, [pkg('container', 'docker.io/nope/x:1', 'app')])
        with self.assertRaises(InstallFailed) as caught:
            self.install(ppl, present=False, pull_rc=125)
        self.assertEqual(caught.exception.issues[0]['kind'], 'container_pull_failed')

    def test_apptainer_skips_compose(self):
        ppl = FakePipeline(self.jarvis, [pkg('container', '', 'app')],
                           container_uri='oras://example/foo.sif',
                           container_engine='apptainer')
        self.install(ppl)
        self.assertTrue(ppl.generated_yaml)
        self.assertFalse(ppl.generated_compose)

    def test_unnamed_engine_is_detected_or_asked(self):
        ppl = FakePipeline(self.jarvis, [pkg('container', 'docker.io/x/y:1', 'app')],
                           source='default')
        both = [{'id': 'apptainer', 'constraint': 'apptainer'},
                {'id': 'podman', 'constraint': 'podman'}]
        with mock.patch.object(mi, 'detect_engines', return_value=(both, [])):
            result, _ = self.install(ppl, 'check')
        self.assertEqual(result['status'], 'needs_decision')
        self.assertEqual(self.kinds(), ['container_engine'])
        with mock.patch.object(mi, 'detect_engines',
                               return_value=(both[1:], ['docker: rootful'])):
            result, _ = self.install(ppl, 'check', context=self.new_context())
        self.assertEqual(result['status'], 'satisfied')
        self.assertEqual((ppl.container_engine, ppl.container_engine_source),
                         ('podman', 'detected'))

    def test_rootful_docker_is_left_out(self):
        def which(name):
            return '/usr/bin/docker' if name == 'docker' else None

        with mock.patch('shutil.which', side_effect=which), \
                mock.patch.object(mi, '_engine_field', return_value='[name=seccomp]'):
            found, excluded = mi.detect_engines()
        self.assertEqual(found, [])
        self.assertIn('rootful', excluded[0])

    def test_podman_gets_a_runtime_dir(self):
        with mock.patch.dict(os.environ, {'XDG_RUNTIME_DIR': str(self.tmp / 'nope')}), \
                mock.patch('tempfile.gettempdir', return_value=str(self.tmp)):
            made = mi.podman_runtime_dir()
            self.assertEqual(os.environ['XDG_RUNTIME_DIR'], made)
        self.assertTrue(Path(made).is_dir())


class TestMethodChoice(_Case):
    def choose(self, fits, context=None):
        ppl = FakePipeline(self.jarvis, [{
            'pkg_type': 'builtin.tool', 'pkg_id': 'tool', 'pkg_name': 'tool',
            'config': {'install_methods': [
                {'method': 'spack', 'query': 'py-rich'},
                {'method': 'pip', 'query': 'rich==13.7'},
            ]},
        }])
        with mock.patch.object(mi, 'method_fits',
                               side_effect=lambda method: (fits[method], 'probe')):
            chosen = mi.resolve_method_choices(ppl, context or self.context)
        return ppl, chosen

    def test_the_only_fitting_method_is_used(self):
        ppl, chosen = self.choose({'spack': False, 'pip': True})
        self.assertEqual(chosen, {'tool': 'pip'})
        config = ppl.packages[0]['config']
        self.assertEqual((config['install_method'], config['install_query']),
                         ('pip', 'rich==13.7'))
        self.assertEqual(Installer.get_installers(ppl), {'pip': ppl.packages})

    def test_several_fitting_methods_are_a_recorded_question(self):
        ppl, chosen = self.choose({'spack': True, 'pip': True})
        self.assertEqual(chosen, {})
        issue = self.context.issues[0]
        self.assertEqual((issue['kind'], issue['scope'], issue['package']),
                         ('install_method', 'package', 'tool'))
        self.assertEqual([c['id'] for c in issue['candidates']], ['spack', 'pip'])

        answered = self.new_context({issue['issue_id']: {'constraint': 'spack'}})
        ppl, chosen = self.choose({'spack': True, 'pip': True}, answered)
        self.assertEqual(chosen, {'tool': 'spack'})
        self.assertEqual(ppl.packages[0]['config']['install_query'], 'py-rich')
        # replayed from decisions.yaml with no answers file
        ppl, chosen = self.choose({'spack': True, 'pip': True}, self.new_context())
        self.assertEqual(chosen, {'tool': 'spack'})

    def test_no_fitting_method_offers_them_all(self):
        self.choose({'spack': False, 'pip': False})
        self.assertEqual([c['id'] for c in self.context.issues[0]['candidates']],
                         ['spack', 'pip'])

    def test_install_all_raises_the_choice(self):
        ppl = FakePipeline(self.jarvis, [{
            'pkg_type': 'builtin.tool', 'pkg_id': 'tool', 'pkg_name': 'tool',
            'config': {'install_methods': ['spack', 'pip']},
        }])
        with mock.patch.object(mi, 'method_fits', return_value=(True, 'probe')):
            with self.assertRaises(InstallDecisionNeeded) as caught:
                Installer.install_all(ppl, mode='check', context=self.context)
        self.assertEqual(caught.exception.issues[0]['kind'], 'install_method')

    def test_bad_entries_are_refused(self):
        with self.assertRaises(ValueError):
            mi.method_options({'pkg_id': 'x', 'config': {'install_methods': ['brew']}})

    def test_a_site_spack_jarvis_can_find_fits(self):
        from jarvis_cd.core import spack_install

        site = [{'root': '/site/spack'}]
        with mock.patch.object(spack_install, 'spack_unavailable_reason',
                               return_value='no spack on PATH'), \
                mock.patch.object(spack_install, 'discover_site_spacks', return_value=site):
            self.assertEqual(mi.method_fits('spack'), (True, 'site Spack /site/spack'))
        with mock.patch.object(spack_install, 'spack_unavailable_reason',
                               return_value='no spack on PATH'), \
                mock.patch.object(spack_install, 'discover_site_spacks', return_value=[]):
            self.assertEqual(mi.method_fits('spack'), (False, 'no spack on PATH'))


class TestHelpers(unittest.TestCase):
    def test_mpi_named(self):
        self.assertEqual(mi.mpi_named('pip', ['mpich>=4']), 'mpich')
        self.assertEqual(mi.mpi_named('conda', ['conda-forge::openmpi=5']), 'openmpi')
        self.assertEqual(mi.mpi_named('conda', ['mpi4py']), 'mpi4py')
        self.assertIsNone(mi.mpi_named('pip', ['mpi4py']))
        self.assertIsNone(mi.mpi_named('pip', ['numpy', '--index-url', 'x']))

    def test_env_name_must_be_plain(self):
        ppl = FakePipeline(FakeJarvis(tempfile.gettempdir()),
                           [pkg('pip', 'x', 'a', install_env='../up')])
        with self.assertRaises(ValueError):
            mi.env_prefix(ppl, 'pip', ppl.packages)

    def test_shared_env_dir_wins_over_the_jarvis_root(self):
        # relay gives each job its own JARVIS root; the env must outlive them
        ppl = FakePipeline(FakeJarvis(tempfile.gettempdir()), [pkg('pip', 'x', 'a')])
        with mock.patch.dict(os.environ, {'JARVIS_INSTALL_ENV_DIR': '/site/envs'}):
            self.assertEqual(mi.env_prefix(ppl, 'pip', ppl.packages),
                             Path('/site/envs') / 'pip' / 'env')
        named = FakePipeline(FakeJarvis(tempfile.gettempdir()),
                             [pkg('conda', 'x', 'a', install_env='tools')])
        with mock.patch.dict(os.environ, {'JARVIS_INSTALL_ENV_DIR': '/site/envs'}):
            self.assertEqual(mi.env_prefix(named, 'conda', named.packages),
                             Path('/site/envs') / 'conda' / 'tools')


if __name__ == '__main__':
    unittest.main()
