"""Unit tests for SBATCH directive rendering in jarvis_cd.core.scheduler.

Regression guard: slurm does NOT expand env vars
in #SBATCH directives, so an unexpanded ``${HOME}`` in output:/error: was
taken literally (relative to WorkDir), the log file could not be opened, and
the job failed at launch with no logs. jarvis must expand ${VAR} host-side
while leaving slurm patterns (%j, %x) and unset vars untouched.
"""
import os
import unittest

from jarvis_cd.core.scheduler import make_scheduler


def _render(spec, tmp_path='/tmp/jarvis-sched-test'):
    sched = make_scheduler(spec, tmp_path, pipeline_yaml='/x/ppl.yaml')
    return sched.render()


class TestSchedulerDirectiveExpansion(unittest.TestCase):
    def setUp(self):
        os.environ['JARVIS_SCHED_TEST_HOME'] = '/home/tester'

    def tearDown(self):
        os.environ.pop('JARVIS_SCHED_TEST_HOME', None)

    def test_expands_env_var_in_output_error(self):
        text = _render({
            'name': 'slurm',
            'job_name': 'smoke',
            'output': '${JARVIS_SCHED_TEST_HOME}/smoke-%j.out',
            'error': '${JARVIS_SCHED_TEST_HOME}/smoke-%j.err',
        })
        # ${VAR} expanded to an absolute path...
        self.assertIn('#SBATCH --output=/home/tester/smoke-%j.out', text)
        self.assertIn('#SBATCH --error=/home/tester/smoke-%j.err', text)
        # ...and the literal (broken) form is gone.
        self.assertNotIn('${JARVIS_SCHED_TEST_HOME}', text)

    def test_preserves_slurm_patterns(self):
        text = _render({
            'name': 'slurm',
            'output': '/logs/%x-%j.out',
        })
        self.assertIn('#SBATCH --output=/logs/%x-%j.out', text)

    def test_unset_var_left_literal_not_blanked(self):
        # os.path.expandvars leaves unknown vars untouched rather than
        # blanking them (so a user's ${SLURM_JOB_ID} typo stays visible
        # instead of silently collapsing the path).
        text = _render({
            'name': 'slurm',
            'output': '${DEFINITELY_UNSET_XYZ}/o-%j.out',
        })
        self.assertIn('#SBATCH --output=${DEFINITELY_UNSET_XYZ}/o-%j.out', text)

    def test_expands_in_sbatch_args_passthrough(self):
        text = _render({
            'name': 'slurm',
            'sbatch_args': ['--comment=${JARVIS_SCHED_TEST_HOME}/note'],
        })
        self.assertIn('#SBATCH --comment=/home/tester/note', text)

    def test_partition_value_without_var_unchanged(self):
        text = _render({'name': 'slurm', 'partition': 'compute'})
        self.assertIn('#SBATCH --partition=compute', text)


if __name__ == '__main__':
    unittest.main()
