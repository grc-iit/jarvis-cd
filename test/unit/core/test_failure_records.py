"""Failed probes, checks and builds come back as the error itself (exit 6)."""

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

from jarvis_cd.core import spack_install as si
from jarvis_cd.core.spack_install import Decisions, InstallContext, SpecNode

ENOSPC = 'cc1: error closing /tmp/ccSN2wEK.s: No space left on device'


class _Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def context(self, answers=None):
        context = InstallContext(install_dir=self.tmp / 'install',
                                 decisions_path=self.tmp / 'decisions.yaml',
                                 answers=answers or {})
        context.open()
        self.addCleanup(context.close)
        return context

    def script(self, name, body):
        path = self.tmp / name
        path.write_text('#!/bin/sh\n' + body, encoding='utf-8')
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return str(path)


class TestFailureRecord(_Case):
    def test_the_record_keeps_everything_and_the_file_has_the_whole_output(self):
        context = self.context()
        output = ''.join(f'line {n}\n' for n in range(500)) + ENOSPC + '\n'
        issue = si.failure_record(
            context, stage='install', doing='spack install of ior',
            command=['spack', 'install', 'ior'], exit_code=1, output=output,
            host='node7', cwd='/work', packages=['ior'], paths={'build_log': '/b.log'},
            extra={'failed_node': 'ior@3.3.0'})
        self.assertEqual((issue['kind'], issue['answerable'], issue['scope']),
                         ('failure', False, 'run'))
        self.assertEqual(issue['failure_schema'], si.FAILURE_SCHEMA)
        self.assertEqual((issue['host'], issue['command'], issue['cwd'], issue['exit_code']),
                         ('node7', 'spack install ior', '/work', 1))
        self.assertEqual(issue['candidates'], [])
        for key in ('remedy', 'recommended', 'hint', 'output'):
            self.assertNotIn(key, issue)
        self.assertEqual(issue['paths']['build_log'], '/b.log')
        self.assertEqual(issue['paths']['install_log'], str(context.log_path))
        self.assertEqual(issue['failed_node'], 'ior@3.3.0')
        self.assertIn(ENOSPC, issue['output_tail'])
        self.assertNotIn('line 0\n', issue['output_tail'])
        written = Path(issue['output_path']).read_text(encoding='utf-8')
        self.assertIn('line 0\n', written)
        self.assertIn(ENOSPC, written)
        self.assertEqual(issue['output_bytes'], len(output.encode()))
        self.assertTrue(issue['output_path'].startswith(str(context.install_dir)))
        self.assertIn('node7', issue['question'])
        self.assertIn(issue['output_path'], issue['question'])

    def test_another_node_is_another_record(self):
        first = si.failure_record(None, stage='x', doing='x', command='cc', exit_code=1,
                                  output='', host='a')
        second = si.failure_record(None, stage='x', doing='x', command='cc', exit_code=1,
                                   output='', host='b')
        self.assertNotEqual(first['issue_id'], second['issue_id'])

    def test_an_unwritable_output_file_keeps_the_output_in_the_record(self):
        context = self.context()
        with mock.patch.object(Path, 'open', side_effect=OSError(28, 'No space left')):
            issue = si.failure_record(context, stage='x', doing='x', command='cc',
                                      exit_code=1, output=ENOSPC, host='a')
        self.assertIsNone(issue['output_path'])
        self.assertIn('No space left', issue['output_write_error'])
        self.assertEqual(issue['output'], ENOSPC)


class TestSpackFailure(_Case):
    def test_missing_compiler_executables_are_compiler_unavailable_with_the_error(self):
        output = ("==> Installing ior-3.3.0-abc\n==> Error: CompilerAccessError: Compiler "
                  "'gcc@=11.4.0' has executables that are missing or are not executable: "
                  "['/opt/gcc/bin/gcc']\n")
        issue = si.spack_failure(self.context(), output, stage='install', packages=['ior'],
                                 command='spack install ior', exit_code=1,
                                 usable_compilers=[{'spec': 'gcc@12.3.0', 'paths': {
                                     'cc': '/usr/bin/gcc'}, 'full': False}])
        self.assertEqual(issue['kind'], 'compiler_unavailable')
        self.assertEqual(issue['failed_node'], 'ior-3.3.0-abc')
        self.assertEqual(issue['exit_code'], 1)
        self.assertIn('CompilerAccessError', Path(issue['output_path']).read_text())

    def test_anything_else_is_the_failure_record(self):
        output = ("==> Installing ior-3.3.0-abc\nconfigure: error: C compiler cannot create "
                  "executables\nSee build log for details:\n  /stage/spack-build-out.txt\n")
        issue = si.spack_failure(self.context(), output, stage='install', packages=['ior'],
                                 command='spack install ior', exit_code=1)
        self.assertEqual((issue['kind'], issue['stage']), ('failure', 'install'))
        self.assertEqual(issue['failed_node'], 'ior-3.3.0-abc')
        self.assertTrue(issue['question'].startswith(
            'spack install of ior at ior-3.3.0-abc failed on '))
        self.assertEqual(issue['paths']['build_log'], '/stage/spack-build-out.txt')
        self.assertNotIn('remedy', issue)


@unittest.skipIf(os.name == 'nt', 'POSIX compilers')
class TestCompilerProbe(_Case):
    def test_a_failing_test_program_is_evidence_not_a_missing_compiler(self):
        cc = self.script('cc', f'echo "{ENOSPC}" >&2\nexit 1\n')
        with mock.patch.dict(os.environ, {'JARVIS_COMPILER_PROBE': 'on'}):
            doc = si.verify_compilers([{'spec': 'gcc@11.4.0', 'paths': {'cc': cc}}], probe=True)
        [item] = doc['compilers']
        self.assertTrue(item['usable'])
        self.assertEqual(doc['usable'], ['gcc@11.4.0'])
        self.assertEqual(doc['unusable'], [])
        self.assertEqual(doc['probe_failed'], [{'spec': 'gcc@11.4.0', 'languages': ['cc']}])
        result = item['probe_results']['cc']
        self.assertEqual((result['ok'], result['step'], result['exit_code']),
                         (False, 'compile', 1))
        self.assertIn(ENOSPC, result['output'])
        self.assertTrue(result['command'].startswith(cc))

    def test_a_probe_that_cannot_make_its_directory_says_so(self):
        cc = self.script('cc', 'exit 0\n')
        with mock.patch.object(si.tempfile, 'TemporaryDirectory',
                               side_effect=OSError(28, 'No space left on device')):
            results = si.probe_compiler({'cc': cc}, {})
        self.assertEqual((results['cc']['step'], results['cc']['exit_code']), ('setup', None))
        self.assertIn('No space left on device', results['cc']['error'])

    def test_no_usable_temp_dir_at_all_is_returned_too(self):
        cc = self.script('cc', 'exit 0\n')
        gone = FileNotFoundError(2, 'No usable temporary directory found in /full/tmp')
        with mock.patch.object(si.tempfile, 'TemporaryDirectory', side_effect=gone), \
                mock.patch.object(si.tempfile, 'gettempdir', side_effect=gone), \
                mock.patch.dict(os.environ, {'TMPDIR': '/full/tmp'}):
            results = si.probe_compiler({'cc': cc}, {})
        self.assertEqual((results['cc']['step'], results['cc']['cwd']), ('setup', '/full/tmp'))
        self.assertIn('No usable temporary directory', results['cc']['error'])

    def test_a_missing_executable_is_the_fact(self):
        doc = si.verify_compilers([{'spec': 'gcc@11.4.0',
                                    'paths': {'cc': str(self.tmp / 'gone')}}])
        self.assertEqual(doc['unusable'], [{'spec': 'gcc@11.4.0',
                                            'missing': [str(self.tmp / 'gone')]}])


class TestCompilerGuard(_Case):
    def planner(self, probe_failed=None, unusable=()):
        planner = si.SpackPlanner.__new__(si.SpackPlanner)
        planner.context = self.context()
        planner.build_host = 'ares-comp-19'
        planner.unusable_compilers = list(unusable)
        planner.usable_compilers = [{'spec': 'gcc@11.4.0', 'paths': {'cc': '/usr/bin/gcc'}}]
        planner.probe_failed = probe_failed or {}
        return planner

    def nodes(self):
        return [SpecNode(status='missing', hash='abc1234', name='ior',
                         spec='ior@3.3.0%gcc@11.4.0 arch=linux')]

    def test_a_failed_c_probe_returns_the_probe_error_not_a_compiler_question(self):
        failed = {'spec': 'gcc@11.4.0', 'probe_failed': ['cc'], 'probe_results': {'cc': {
            'ok': False, 'step': 'compile', 'command': 'gcc probe.c -o probe',
            'cwd': '/tmp/jarvis-compiler-probe-x', 'exit_code': 1, 'output': ENOSPC,
            'error': None}}}
        planner = self.planner({'gcc@11.4.0': failed})
        issue = planner.compiler_guard(self.nodes(), 'spack spec')
        assert issue is not None
        self.assertEqual((issue['kind'], issue['stage'], issue['answerable']),
                         ('failure', 'compiler_probe', False))
        self.assertEqual((issue['host'], issue['command'], issue['exit_code']),
                         ('ares-comp-19', 'gcc probe.c -o probe', 1))
        self.assertEqual(issue['paths']['probe_dir'], '/tmp/jarvis-compiler-probe-x')
        self.assertEqual((issue['compiler'], issue['language'], issue['probe_step']),
                         ('gcc@11.4.0', 'cc', 'compile'))
        self.assertIn(ENOSPC, issue['output_tail'])
        self.assertEqual(issue['candidates'], [])

    def test_installed_nodes_and_cxx_only_failures_do_not_stop_the_build(self):
        failed = {'spec': 'gcc@11.4.0', 'probe_failed': ['cxx'],
                  'probe_results': {'cxx': {'ok': False}}}
        self.assertIsNone(self.planner({'gcc@11.4.0': failed})
                          .compiler_guard(self.nodes(), 'spack spec'))
        present = [SpecNode(status='installed', hash='abc', name='ior',
                            spec='ior@3.3.0%gcc@11.4.0')]
        failed = {'spec': 'gcc@11.4.0', 'probe_failed': ['cc'],
                  'probe_results': {'cc': {'ok': False}}}
        self.assertIsNone(self.planner({'gcc@11.4.0': failed})
                          .compiler_guard(present, 'spack spec'))


class TestAnswers(_Case):
    def issue(self, scope='package'):
        return si.make_issue(kind='variant', package='ior', subject='variant',
                             scope=scope, question='which?', answerable=True,
                             candidates=[{'id': 'v-1', 'constraint': '+hdf5'},
                                         {'id': 'v-2', 'constraint': '~hdf5'}])

    def test_resending_the_same_answer_leaves_the_file_alone(self):
        for scope in ('package', 'site'):
            path = self.tmp / f'{scope}.yaml'
            issue = self.issue(scope)
            Decisions(path).record(issue, {'constraint': '+hdf5',
                                           'answered_at': '2026-11-25T10:00:00Z'})
            before = path.read_bytes()
            mtime = path.stat().st_mtime_ns
            Decisions(path).record(issue, {'constraint': '+hdf5',
                                           'answered_at': '2026-11-26T10:00:00Z',
                                           'answered_by': 'relay'})
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(path.stat().st_mtime_ns, mtime)
            Decisions(path).record(issue, {'constraint': '~hdf5'})
            self.assertIn('~hdf5', path.read_text(encoding='utf-8'))

    def test_an_answer_that_is_no_candidate_is_reported_and_the_question_stays(self):
        issue = self.issue()
        context = self.context({issue['issue_id']: {'constraint': '+mpi'}})
        self.assertIsNone(context.answer_for(issue))
        [rejected] = context.rejected_answers
        self.assertEqual((rejected['issue_id'], rejected['constraint']),
                         (issue['issue_id'], '+mpi'))

    def test_an_answer_for_an_issue_not_raised_is_ignored(self):
        context = self.context({'install-issue:unrelated': {'constraint': 'x'}})
        self.assertIsNone(context.answer_for(self.issue()))
        self.assertEqual(context.rejected_answers, [])


class TestPerHostBuildCompilers(_Case):
    def test_the_newest_host_check_is_read(self):
        directory = self.tmp / 'var' / 'jarvis' / si.BUILD_COMPILERS_DIR_NAME
        directory.mkdir(parents=True)
        for host, at in (('comp-10', '2026-11-25T10:00:00Z'), ('comp-19', '2026-11-25T11:00:00Z')):
            (directory / f'{host}.json').write_text(
                json.dumps({'host': host, 'checked_at': at}), encoding='utf-8')
        with mock.patch.object(si, 'explicit_spack_root', return_value=str(self.tmp)):
            latest = si.latest_build_compilers()
        assert latest is not None
        self.assertEqual(latest['host'], 'comp-19')
        self.assertEqual(latest['path'], str(directory / 'comp-19.json'))


if __name__ == '__main__':
    unittest.main()
