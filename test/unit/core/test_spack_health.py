"""A Spack that fails on its own: the typed, answerable ``spack_broken``."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

from jarvis_cd.core import spack_health as sh
from jarvis_cd.core import spack_install as si
from jarvis_cd.core.spack_install import Decisions, InstallContext

ERROR = '==> Error: cannot unpack non-iterable NoneType object'


class FakeSpack:
    """``spack providers mpi`` fails while ``bad_repo`` is configured."""

    def __init__(self, user, bad_repo, own_scope_broken=False, rc_on_probe=3):
        self.user = user
        self.bad_repo = bad_repo
        self.own_scope_broken = own_scope_broken
        self.rc_on_probe = rc_on_probe
        self.calls = []

    def __call__(self, executable, args, env, timeout=None):
        args = list(args)
        isolated = env.get('SPACK_DISABLE_LOCAL_CONFIG') == '1'
        self.calls.append((tuple(args), isolated))
        if args[:3] == ['config', 'blame', 'repos']:
            return 0, (f'{self.user}/repos.yaml:2    - {self.bad_repo}\n'
                       f'{Path(executable).parent.parent}/var/spack/repos/builtin:1  '
                       '  - $spack/var/spack/repos/builtin\n'), ''
        if self.rc_on_probe is None:
            return None, '', 'timed out after 300 s'
        if self.own_scope_broken:
            return 3, '', ERROR
        if args[:1] == ['-C']:
            text = Path(args[1], 'repos.yaml').read_text(encoding='utf-8')
            return (3, '', ERROR) if self.bad_repo in text else (0, 'mpi: openmpi\n', '')
        return (0, 'mpi: openmpi\n', '') if isolated else (self.rc_on_probe, '', ERROR)

    def probes(self):
        return [call for call in self.calls if call[0] == sh.PROBE]


class _Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.root = self.tmp / 'spack'
        (self.root / 'bin').mkdir(parents=True)
        (self.root / 'bin' / 'spack').write_text('#!/bin/sh\n', encoding='utf-8')
        self.user = self.tmp / 'user'
        self.user.mkdir()
        (self.user / 'repos.yaml').write_text('repos:\n- x\n', encoding='utf-8')
        (self.user / 'packages.yaml').write_text('packages: {}\n', encoding='utf-8')
        self.bad = self.tmp / 'coeus'
        self.bad.mkdir()
        (self.bad / 'repo.yaml').write_text('repo:\n  namespace: coeus\n', encoding='utf-8')
        env = mock.patch.dict(os.environ, {
            sh.SWITCH_ENV: 'on',
            'SPACK_USER_CONFIG_PATH': str(self.user),
            'SPACK_SYSTEM_CONFIG_PATH': str(self.tmp / 'etc-spack'),
        })
        env.start()
        self.addCleanup(env.stop)
        for name in ('SPACK_DISABLE_LOCAL_CONFIG', 'JARVIS_SPACK_ROOT', 'JARVIS_SPACK_CONFIG'):
            os.environ.pop(name, None)
        sh.reset()
        self.addCleanup(sh.reset)

    def context(self, answers=None):
        context = InstallContext(install_dir=self.tmp / 'install',
                                 decisions_path=self.tmp / 'decisions.yaml',
                                 answers=answers or {})
        context.open()
        self.addCleanup(context.close)
        return context

    def decisions(self):
        return Decisions(self.tmp / 'decisions.yaml')

    def gate(self, spack, answers=None, **kwargs):
        context = self.context(answers)
        with mock.patch.object(sh, '_run', side_effect=spack):
            state = sh.gate(self.root, str(self.root / 'bin' / 'spack'), context,
                            self.decisions(), **kwargs)
        return state, context

    def fake(self, **kwargs):
        return FakeSpack(str(self.user), str(self.bad), **kwargs)


class TestBroken(_Case):
    def test_the_issue_names_the_spack_the_repo_its_file_and_the_error(self):
        state, context = self.gate(self.fake())
        self.assertEqual(state['status'], 'broken')
        issue = context.issues[0]
        self.assertEqual((issue['kind'], issue['scope'], issue['answerable']),
                         ('spack_broken', 'site', True))
        root = sh.root_key(self.root)
        self.assertEqual(issue['subject'], f'spack:{root}')
        self.assertEqual(issue['spack_root'], root)
        self.assertEqual(issue['probe']['exit'], 3)
        self.assertIn('cannot unpack non-iterable NoneType object', issue['probe']['error'])
        self.assertEqual(issue['failing_scope'], 'user')
        self.assertEqual(issue['failing_file'], f'{self.user}/repos.yaml')
        self.assertEqual([(r['repo'], r['namespace']) for r in issue['failing_repos']],
                         [(str(self.bad), 'coeus')])
        self.assertIn(f'the repo {self.bad} (namespace coeus)', issue['question'])
        self.assertEqual([c['id'] for c in issue['candidates']],
                         ['no_user_config', 'skip', 'recheck'])
        self.assertEqual(issue['recommended'], 'no_user_config')
        leaves_out = issue['candidates'][0]['leaves_out']
        self.assertIn(f'the user scope {self.user} (packages.yaml, repos.yaml)', leaves_out)
        self.assertIn('Repos left out: coeus', leaves_out)
        self.assertIn('is not edited', leaves_out)
        for text in (issue['question'], issue['hint']):
            self.assertNotIn('jarvis ppl', text)

    def test_a_spack_broken_in_its_own_scopes_cannot_run_without_user_config(self):
        state, context = self.gate(self.fake(own_scope_broken=True))
        issue = state['issue']
        self.assertEqual([c['id'] for c in issue['candidates']], ['skip', 'recheck'])
        self.assertEqual(issue['recommended'], 'skip')
        self.assertIn("in the Spack's own scopes", issue['cause'])
        self.assertEqual(issue['failing_repos'], [])
        # an answer that is not offered is refused
        issue_id = issue['issue_id']
        with self.assertRaises(ValueError):
            self.gate(self.fake(own_scope_broken=True),
                      {issue_id: {'constraint': 'no_user_config'}})
        self.assertFalse(sh.isolated(self.root))

    def test_a_probe_that_cannot_run_is_not_judged(self):
        state, context = self.gate(self.fake(rc_on_probe=None))
        self.assertEqual(state['status'], 'unknown')
        self.assertEqual(context.issues, [])

    def test_off_probes_nothing(self):
        spack = self.fake()
        with mock.patch.dict(os.environ, {sh.SWITCH_ENV: 'off'}):
            state, _ = self.gate(spack)
        self.assertEqual(state['status'], 'off')
        self.assertEqual(spack.calls, [])

    def test_a_working_spack_is_probed_once(self):
        spack = mock.Mock(return_value=(0, 'mpi: openmpi\n', ''))
        for _ in range(2):
            state, context = self.gate(spack)
            self.assertEqual(state['status'], 'ok')
            self.assertEqual(context.issues, [])
        self.assertEqual(spack.call_count, 1)


class TestAnswers(_Case):
    def open_issue(self):
        _, context = self.gate(self.fake())
        return context.issues[0]['issue_id']

    def test_no_user_config_is_recorded_for_the_site_and_replays(self):
        issue_id = self.open_issue()
        state, context = self.gate(self.fake(), {issue_id: {'constraint': 'no_user_config'}})
        self.assertEqual(state['status'], 'isolated')
        self.assertEqual(context.issues, [])
        recorded = self.decisions().site(sh.subject_for(self.root))
        assert recorded is not None
        self.assertEqual((recorded['kind'], recorded['constraint'], recorded['spack_root']),
                         ('spack_broken', 'no_user_config', sh.root_key(self.root)))
        self.assertTrue(sh.isolated(self.root))
        self.assertEqual(sh.environment(self.root)['SPACK_DISABLE_LOCAL_CONFIG'], '1')
        with mock.patch.object(si, '_resolved_spack_root', return_value=self.root):
            self.assertEqual(si.spack_subprocess_environment()['SPACK_DISABLE_LOCAL_CONFIG'],
                             '1')

        # a later run: no question, the recorded answer applies
        sh.reset()
        spack = self.fake()
        state, context = self.gate(spack)
        self.assertEqual(state['status'], 'isolated')
        self.assertEqual(context.issues, [])
        self.assertEqual(spack.probes(), [(sh.PROBE, True)])
        self.assertTrue(sh.isolated(self.root))

    def test_skip_is_recorded_and_replays_without_probing(self):
        issue_id = self.open_issue()
        state, _ = self.gate(self.fake(), {issue_id: {'constraint': 'skip'}})
        self.assertEqual(state['status'], 'skipped')
        sh.reset()
        spack = self.fake()
        state, context = self.gate(spack)
        self.assertEqual(state['status'], 'skipped')
        self.assertEqual(spack.calls, [])
        self.assertTrue(sh.skipped(self.root))
        self.assertFalse(sh.isolated(self.root))
        self.assertEqual(context.issues, [])

    def test_recheck_records_nothing_and_asks_again_while_broken(self):
        issue_id = self.open_issue()
        state, context = self.gate(self.fake(), {issue_id: {'constraint': 'recheck'}})
        self.assertEqual(state['status'], 'broken')
        self.assertIn('rechecked_at', context.issues[0])
        self.assertIsNone(self.decisions().site(sh.subject_for(self.root)))

        fixed = mock.Mock(return_value=(0, 'mpi: openmpi\n', ''))
        state, context = self.gate(fixed, {issue_id: {'constraint': 'recheck'}})
        self.assertEqual(state['status'], 'ok')
        self.assertIsNone(self.decisions().site(sh.subject_for(self.root)))


class TestBuildSpack(_Case):
    def test_a_broken_build_spack_returns_the_issue(self):
        context = self.context()
        with mock.patch.object(si, '_resolved_spack_root', return_value=self.root), \
                mock.patch.object(sh, '_run', side_effect=self.fake()):
            issue = si.ensure_build_spack_healthy(context, self.decisions())
        assert issue is not None
        self.assertEqual(issue['kind'], 'spack_broken')
        self.assertTrue(issue['builds_with'])
        skip = next(c for c in issue['candidates'] if c['id'] == 'skip')
        self.assertIn('builds with its own Spack', skip['does'])

    def test_skip_builds_with_jarvis_own_spack_chained_to_it(self):
        _, context = self.gate(self.fake(), builds_with=True)
        issue_id = context.issues[0]['issue_id']
        context = self.context({issue_id: {'constraint': 'skip'}})
        chained = self.tmp / 'chained'
        with mock.patch.object(si, '_resolved_spack_root', return_value=self.root), \
                mock.patch.object(sh, '_run', side_effect=self.fake()), \
                mock.patch.object(si, 'chained_spack_root', return_value=chained), \
                mock.patch.object(si, 'prepare_chained_spack', return_value=None) as prepare, \
                mock.patch.dict(os.environ):
            self.assertIsNone(si.ensure_build_spack_healthy(context, self.decisions()))
            self.assertEqual(os.environ['JARVIS_SPACK_ROOT'], str(chained))
        prepare.assert_called_once()

    def test_a_skipped_explicit_spack_is_a_fact(self):
        _, context = self.gate(self.fake(), builds_with=False)
        issue_id = context.issues[0]['issue_id']
        state, _ = self.gate(self.fake(), {issue_id: {'constraint': 'skip'}})
        self.assertEqual(state['status'], 'skipped')
        sh.reset()
        context = self.context()
        with mock.patch.object(si, '_resolved_spack_root', return_value=self.root), \
                mock.patch.object(sh, '_run', side_effect=self.fake()), \
                mock.patch.dict(os.environ, {'JARVIS_SPACK_ROOT': str(self.root)}):
            issue = si.ensure_build_spack_healthy(context, self.decisions())
        assert issue is not None
        self.assertEqual((issue['kind'], issue['answerable']), ('spack_broken_skipped', False))


if __name__ == '__main__':
    unittest.main()
