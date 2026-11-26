"""A Spack that fails on its own: the check returns the error as it is."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

from jarvis_cd.core import spack_health as sh
from jarvis_cd.core import spack_install as si
from jarvis_cd.core.spack_install import InstallContext

ERROR = '==> Error: cannot unpack non-iterable NoneType object'
TRACE = 'Traceback (most recent call last):\n  File "spack/repo.py", line 1\n'


class FakeSpack:
    """``spack providers mpi`` fails unless the local config is disabled."""

    def __init__(self, rc: 'int | None' = 3):
        self.rc = rc
        self.calls = []

    def __call__(self, executable, args, env, timeout=None):
        self.calls.append((tuple(args), env.get('SPACK_DISABLE_LOCAL_CONFIG'),
                           env.get('SPACK_USER_CONFIG_PATH')))
        if self.rc is None:
            return None, '', 'timed out after 300 s'
        if env.get('SPACK_DISABLE_LOCAL_CONFIG') == '1':
            return 0, 'mpi: openmpi\n', ''
        return self.rc, 'partial stdout\n', TRACE + ERROR + '\n'


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

    def context(self):
        context = InstallContext(install_dir=self.tmp / 'install',
                                 decisions_path=self.tmp / 'decisions.yaml', answers={})
        context.open()
        self.addCleanup(context.close)
        return context

    def gate(self, spack, **kwargs):
        context = self.context()
        with mock.patch.object(sh, '_run', side_effect=spack):
            state = sh.gate(self.root, str(self.root / 'bin' / 'spack'), context, **kwargs)
        return state, context


class TestBroken(_Case):
    def test_the_record_is_the_error_with_its_full_output_in_a_file(self):
        state, context = self.gate(FakeSpack(), packages=['ior'])
        self.assertEqual(state['status'], 'broken')
        [issue] = context.issues
        self.assertEqual(state['issue_id'], issue['issue_id'])
        self.assertEqual((issue['kind'], issue['answerable'], issue['stage']),
                         ('failure', False, 'spack_health'))
        self.assertEqual(issue['candidates'], [])
        self.assertNotIn('recommended', issue)
        self.assertNotIn('remedy', issue)
        root = sh.root_key(self.root)
        self.assertEqual(issue['spack_root'], root)
        self.assertEqual(issue['exit_code'], 3)
        self.assertEqual(issue['command'], f'{self.root / "bin" / "spack"} providers mpi')
        self.assertEqual(issue['packages'], ['ior'])
        self.assertEqual(issue['config_env']['SPACK_USER_CONFIG_PATH'], str(self.user))
        self.assertEqual(issue['paths']['scopes']['user'], str(self.user))
        self.assertIn(ERROR, issue['output_tail'])
        text = Path(issue['output_path']).read_text(encoding='utf-8')
        self.assertIn(TRACE, text)
        self.assertIn('partial stdout', text)
        self.assertIn(issue['output_path'], issue['question'])
        self.assertNotIn('jarvis ppl', issue['question'])

    def test_a_working_spack_raises_nothing(self):
        state, context = self.gate(FakeSpack(rc=0))
        self.assertEqual(state['status'], 'ok')
        self.assertEqual(context.issues, [])

    def test_a_probe_that_cannot_finish_is_not_judged(self):
        state, context = self.gate(FakeSpack(rc=None))
        self.assertEqual(state['status'], 'unknown')
        self.assertEqual(context.issues, [])

    def test_off_and_plan_only_do_not_probe(self):
        spack = FakeSpack()
        with mock.patch.dict(os.environ, {sh.SWITCH_ENV: 'off'}):
            self.assertEqual(self.gate(spack)[0]['status'], 'off')
        with mock.patch('jarvis_cd.core.installer.plan_only_active', return_value=True):
            self.assertEqual(self.gate(spack)[0]['status'], 'not_probed')
        self.assertEqual(spack.calls, [])

    def test_spack_own_config_variables_pass_through_and_key_the_probe(self):
        spack = FakeSpack()
        self.assertEqual(self.gate(spack)[0]['status'], 'broken')
        self.assertEqual(self.gate(spack)[0]['status'], 'broken')
        self.assertEqual(len(spack.calls), 1)  # cached
        with mock.patch.dict(os.environ, {'SPACK_DISABLE_LOCAL_CONFIG': '1'}):
            state, context = self.gate(spack)
            self.assertEqual(sh.environment()['SPACK_DISABLE_LOCAL_CONFIG'], '1')
        self.assertEqual(state['status'], 'ok')
        self.assertEqual(context.issues, [])
        self.assertEqual(spack.calls[-1][1], '1')
        self.assertEqual(sh.environment()['SPACK_USER_CONFIG_PATH'], str(self.user))

    def test_the_persons_config_is_not_touched(self):
        (self.user / 'repos.yaml').write_text('repos:\n- /bad\n', encoding='utf-8')
        self.gate(FakeSpack())
        self.assertEqual(sorted(p.name for p in self.user.iterdir()), ['repos.yaml'])
        self.assertEqual((self.user / 'repos.yaml').read_text(encoding='utf-8'),
                         'repos:\n- /bad\n')


class TestBuildSpack(_Case):
    def test_a_broken_build_spack_returns_the_record(self):
        context = self.context()
        with mock.patch.object(si, '_resolved_spack_root', return_value=self.root), \
                mock.patch.object(sh, '_run', side_effect=FakeSpack()):
            issue = si.ensure_build_spack_healthy(context, ['builtin.ior'])
        assert issue is not None
        self.assertEqual((issue['kind'], issue['answerable']), ('failure', False))
        self.assertEqual(issue['packages'], ['builtin.ior'])

    def test_a_working_build_spack_returns_none(self):
        context = self.context()
        with mock.patch.object(si, '_resolved_spack_root', return_value=self.root), \
                mock.patch.object(sh, '_run', side_effect=FakeSpack(rc=0)):
            self.assertIsNone(si.ensure_build_spack_healthy(context))


if __name__ == '__main__':
    unittest.main()
