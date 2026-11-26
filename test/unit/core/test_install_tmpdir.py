"""The install temp dir is a per-site install setting: JARVIS sets TMPDIR for
the install job and writes Spack's build stage into the pipeline's Spack
environment, never into the person's Spack scopes."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

from jarvis_cd.core import spack_install as si
from jarvis_cd.core.spack_install import Decisions, InstallContext, SpackEnvironment

USER_CONFIG = 'config:\n  build_stage:\n  - $tempdir/$user/spack-stage\n'


class _Planner:
    def __init__(self, decisions_path):
        self.decisions = Decisions(decisions_path)
        self.runner = si.SpackRunner(None)


class TestInstallTmpdir(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(setattr, tempfile, 'tempdir', None)
        self.tmp = Path(tmp.name)
        self.home = self.tmp / 'home'
        (self.home / '.spack').mkdir(parents=True)
        self.user_config = self.home / '.spack' / 'config.yaml'
        self.user_config.write_text(USER_CONFIG, encoding='utf-8')
        patcher = mock.patch.dict(os.environ, {'HOME': str(self.home),
                                               'USERPROFILE': str(self.home),
                                               'TMPDIR': str(self.tmp / 'jobtmp')})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.decisions_path = self.tmp / 'site' / 'decisions.yaml'
        self.issue_id = si.install_tmpdir_issue()['issue_id']
        self.scratch = str(self.tmp / 'scratch' / 'tmp')
        self.stage = self.scratch + '/spack-stage'

    def run_tmpdir(self, answers=None, name='install'):
        context = InstallContext(install_dir=self.tmp / name,
                                 decisions_path=self.decisions_path,
                                 answers=answers or {})
        context.open()
        self.addCleanup(context.close)
        planner = _Planner(self.decisions_path)
        record = {}
        sections = si.apply_install_tmpdir(planner, context, record)  # type: ignore[arg-type]
        return context, planner, record, sections

    def test_the_answer_sets_tmpdir_and_the_env_carries_the_stage(self):
        before = self.user_config.read_bytes()
        context, planner, record, sections = self.run_tmpdir(
            {self.issue_id: {'constraint': self.scratch, 'answered_by': 'person'}})
        self.assertEqual(sections, {'config': {'build_stage:': [self.stage]}})
        entry = Decisions(self.decisions_path).site('install_tmpdir')
        assert entry is not None
        self.assertEqual((entry['constraint'], entry['issue_id']), (self.scratch, self.issue_id))
        setting = record['install_settings']['install_tmpdir']
        self.assertEqual((setting['value'], setting['tmpdir'], setting['build_stage']),
                         (self.scratch, self.scratch, self.stage))
        self.assertEqual(context.used_answers, [self.issue_id])
        # the compiler check and Spack (subprocesses) write here
        self.assertEqual(os.environ['TMPDIR'], self.scratch)
        self.assertEqual(Path(tempfile.gettempdir()), Path(self.scratch))
        self.assertEqual(si.spack_subprocess_environment()['TMPDIR'], self.scratch)

        env = SpackEnvironment(self.tmp / 'env', planner.runner, sections=sections)
        env.write_manifest(['fio@3.36'])
        manifest = yaml.safe_load(env.manifest_path.read_text(encoding='utf-8'))
        # an override, so Spack's own list cannot fall back to $tempdir
        self.assertEqual(manifest['spack']['config'], {'build_stage:': [self.stage]})
        scope = context.install_dir / si.STAGE_SCOPE_NAME
        self.assertIn(str(scope), planner.runner.config_scopes)
        self.assertEqual(yaml.safe_load((scope / 'config.yaml').read_text(encoding='utf-8')),
                         {'config': {'build_stage:': [self.stage]}})
        self.assertEqual(self.user_config.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in (self.home / '.spack').iterdir()),
                         ['config.yaml'])

    def test_variables_are_expanded_where_the_job_runs(self):
        with mock.patch.dict(os.environ, {'JARVIS_TEST_SCRATCH': str(self.tmp / 's')}):
            _context, _planner, record, sections = self.run_tmpdir(
                {self.issue_id: {'constraint': '$JARVIS_TEST_SCRATCH/tmp'}})
        setting = record['install_settings']['install_tmpdir']
        self.assertEqual(setting['value'], '$JARVIS_TEST_SCRATCH/tmp')
        self.assertEqual(setting['tmpdir'], str(self.tmp / 's') + '/tmp')
        assert sections is not None
        self.assertEqual(sections['config']['build_stage:'], [setting['tmpdir'] + '/spack-stage'])

    def test_a_later_install_replays_it_and_resending_changes_nothing(self):
        self.run_tmpdir({self.issue_id: {'constraint': self.scratch}})
        recorded = self.decisions_path.read_bytes()
        context, _planner, record, sections = self.run_tmpdir(name='second')
        assert sections is not None
        self.assertEqual(sections['config']['build_stage:'], [self.stage])
        self.assertEqual(record['install_settings']['install_tmpdir']['source'],
                         'decisions_file')
        self.assertEqual(context.applied_decisions[0]['subject'], 'install_tmpdir')
        self.run_tmpdir({self.issue_id: {'constraint': self.scratch}}, name='third')
        self.assertEqual(self.decisions_path.read_bytes(), recorded)

    def test_nothing_recorded_means_nothing_written_and_tmpdir_kept(self):
        context, planner, record, sections = self.run_tmpdir()
        self.assertEqual(sections, {})
        setting = record['install_settings']['install_tmpdir']
        self.assertIsNone(setting['value'])
        self.assertIn('TMPDIR', setting['unset'])
        self.assertEqual(planner.runner.config_scopes, [])
        self.assertEqual(os.environ['TMPDIR'], str(self.tmp / 'jobtmp'))
        self.assertFalse(self.decisions_path.exists())
        self.assertEqual(context.install_settings[0]['issue_id'], self.issue_id)

    def test_a_relative_directory_is_rejected_and_not_recorded(self):
        context, _planner, _record, sections = self.run_tmpdir(
            {self.issue_id: {'constraint': 'tmp'}})
        self.assertEqual(sections, {})
        self.assertEqual(context.rejected_answers[0]['issue_id'], self.issue_id)
        self.assertIsNone(Decisions(self.decisions_path).site('install_tmpdir'))

    def test_an_unusable_directory_raises_the_error_itself(self):
        blocker = self.tmp / 'a-file'
        blocker.write_text('x', encoding='utf-8')
        context, planner, _record, sections = self.run_tmpdir(
            {self.issue_id: {'constraint': str(blocker / 'tmp')}})
        self.assertIsNone(sections)
        [issue] = context.issues
        self.assertEqual((issue['kind'], issue['answerable'], issue['stage']),
                         ('failure', False, 'install_tmpdir'))
        self.assertIn('Error', issue['error'])
        self.assertEqual(issue['settings'][0]['issue_id'], self.issue_id)
        self.assertEqual(planner.runner.config_scopes, [])
        self.assertEqual(os.environ['TMPDIR'], str(self.tmp / 'jobtmp'))

    def test_forgetting_it_drops_the_setting(self):
        self.run_tmpdir({self.issue_id: {'constraint': self.scratch}})
        with mock.patch('jarvis_cd.core.install_placement.forget_graph_answers',
                        return_value=[]):
            si.forget_decisions(self.decisions_path, ['install_tmpdir'])
        context, _planner, _record, sections = self.run_tmpdir(name='install')
        self.assertEqual(sections, {})
        self.assertFalse((context.install_dir / si.STAGE_SCOPE_NAME / 'config.yaml').exists())

    def test_run_spack_install_restores_tmpdir(self):
        def fake(pkg_list, context, runner, env_dir):
            os.environ['TMPDIR'] = self.scratch
            return {}
        with mock.patch.object(si, '_run_spack_install', side_effect=fake):
            si.run_spack_install([], mock.Mock())
        self.assertEqual(os.environ['TMPDIR'], str(self.tmp / 'jobtmp'))

    def test_a_stage_change_alone_keeps_the_lock(self):
        runner = si.SpackRunner(None)
        env_dir = self.tmp / 'env'
        SpackEnvironment(env_dir, runner, sections={}).write_manifest(['fio@3.36'])
        (env_dir / 'spack.lock').write_text(json.dumps(
            {'roots': [{'hash': 'vlseq2g', 'spec': 'fio@3.36'}]}), encoding='utf-8')
        staged = SpackEnvironment(env_dir, runner,
                                  sections={'config': {'build_stage:': [self.stage]}})
        self.assertEqual(staged.current_roots(['fio@3.36']),
                         [{'hash': 'vlseq2g', 'spec': 'fio@3.36'}])

    def test_failure_records_offer_the_setting_with_the_tools_own_text(self):
        context, *_rest = self.run_tmpdir()
        output = ('==> Installing openblas-0.3.28-abc\n'
                  "OSError: [Errno 28] No space left on device: '/tmp/u/spack-stage/x'\n")
        issue = si.spack_failure(context, output, stage='install', packages=['qe'],
                                 command='spack install qe', exit_code=1)
        self.assertEqual((issue['kind'], issue['answerable']), ('failure', False))
        self.assertIn('No space left on device', issue['output_tail'])
        [setting] = issue['settings']
        self.assertEqual((setting['setting'], setting['issue_id']),
                         ('install_tmpdir', self.issue_id))
        probe = si.failure_record(
            context, stage='compiler_probe', doing='the c test program for gcc@12.3.0',
            command='gcc-12 probe.c', exit_code=1,
            output='Error: error closing /tmp/ccSN2wEK.s: No space left on device\n')
        self.assertIn('No space left on device', probe['output_tail'])
        self.assertEqual(probe['settings'][0]['issue_id'], self.issue_id)
        self.assertNotIn('candidates', probe['settings'][0])


if __name__ == '__main__':
    unittest.main()
