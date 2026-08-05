"""
Unit tests for the apptainer branch of Pipeline._start_containerized_pipeline.
Verifies the `apptainer instance start` command construction:
  - Back-compat golden: defaults produce the exact current command string
  - container_overlay_root relocates the overlay upper dir per host and
    fans a mkdir to every node
  - container_overlay: false omits --overlay (and --no-mount tmp)
  - Multi-node + default shared overlay raises instead of racing NFS
  - Per-host instance-start failures raise naming the host
  - Both keys round-trip save() -> load-from-config and YAML load
"""
import unittest
import sys
import os
import tempfile
import shutil
import yaml
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

from jarvis_cd.core.config import Jarvis
from jarvis_cd.core.pipeline import Pipeline
from jarvis_cd.util.hostfile import Hostfile
from jarvis_cd.shell import LocalExecInfo, PsshExecInfo


def initialize_jarvis_for_test(config_dir, private_dir, shared_dir):
    jarvis = Jarvis.get_instance()
    # Save original config so we can restore it after the test
    saved_config = None
    if jarvis.config_file.exists():
        import yaml
        with open(jarvis.config_file, 'r') as f:
            saved_config = yaml.safe_load(f)
    jarvis.initialize(config_dir, private_dir, shared_dir, force=False)
    return jarvis, saved_config


class ApptainerStartTestBase(unittest.TestCase):
    """Base class with shared setUp/tearDown for apptainer start tests."""

    REMOTE_HOSTS = ['node-01', 'node-02', 'node-03', 'node-04']

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix='jarvis_test_apptainer_')
        self.config_dir = os.path.join(self.test_dir, 'config')
        self.private_dir = os.path.join(self.test_dir, 'private')
        self.shared_dir = os.path.join(self.test_dir, 'shared')
        os.makedirs(self.config_dir, exist_ok=True)
        os.makedirs(self.private_dir, exist_ok=True)
        os.makedirs(self.shared_dir, exist_ok=True)

        os.environ['JARVIS_CONFIG'] = self.config_dir
        os.environ['JARVIS_PRIVATE'] = self.private_dir
        os.environ['JARVIS_SHARED'] = self.shared_dir

        self.jarvis, self._saved_config = initialize_jarvis_for_test(
            self.config_dir, self.private_dir, self.shared_dir
        )

    def tearDown(self):
        # Restore the original jarvis config so tests don't clobber the user's setup
        if self._saved_config:
            import yaml
            jarvis = Jarvis.get_instance()
            jarvis.save_config(self._saved_config)
            jarvis.config_dir = self._saved_config.get('config_dir', jarvis.config_dir)
            jarvis.private_dir = self._saved_config.get('private_dir', jarvis.private_dir)
            jarvis.shared_dir = self._saved_config.get('shared_dir', jarvis.shared_dir)
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)

    def _create_pipeline(self, name, hosts=None, **attrs):
        """Create an apptainer container pipeline; hosts=None keeps the
        default (local-only) jarvis hostfile."""
        pipeline = Pipeline()
        pipeline.create(name)
        pipeline.base_deploy_mode = 'container'
        pipeline.container_engine = 'apptainer'
        if hosts is not None:
            pipeline.hostfile = Hostfile(hosts=hosts, find_ips=False)
        for key, value in attrs.items():
            setattr(pipeline, key, value)
        pipeline.save()
        return pipeline

    def _start(self, pipeline, exit_codes=None):
        """Run _start_containerized_pipeline with Exec mocked; returns the
        mock. exit_codes is the per-host dict every Exec().run() reports."""
        with patch('jarvis_cd.shell.Exec') as MockExec:
            result = MockExec.return_value.run.return_value
            result.exit_code = exit_codes if exit_codes is not None else {}
            pipeline._start_containerized_pipeline()
        return MockExec

    @staticmethod
    def _cmds(MockExec):
        """Command strings passed to Exec, in call order."""
        return [c.args[0] for c in MockExec.call_args_list]


class TestApptainerStartCmd(ApptainerStartTestBase):

    def test_default_single_host_golden(self):
        """Back-compat: with neither overlay key set and a local-only
        hostfile, the start command is byte-identical to the historical
        one (shared-dir overlay, --no-mount tmp, LocalExecInfo)."""
        pipeline = self._create_pipeline('apt_golden')
        MockExec = self._start(pipeline)

        shared = self.jarvis.get_pipeline_shared_dir('apt_golden')
        private = self.jarvis.get_pipeline_private_dir('apt_golden')
        sif = self.jarvis.get_containers_dir() / 'apt_golden.sif'
        expected = (
            f"apptainer instance start "
            f"--bind {shared}:{shared} --bind {private}:{private} "
            f"--no-mount tmp --overlay {shared / 'overlay'} "
            f"{sif} apt_golden"
            f" && apptainer exec instance://apt_golden"
            f" /usr/sbin/sshd -p 2222 -o StrictModes=no -o UsePAM=no"
        )
        self.assertEqual(MockExec.call_count, 1)
        cmd, exec_info = MockExec.call_args.args
        self.assertEqual(cmd, expected)
        self.assertIsInstance(exec_info, LocalExecInfo)
        self.assertTrue((shared / 'overlay').is_dir())

    def test_overlay_root_relocates_overlay_and_fans_mkdir(self):
        """container_overlay_root moves the overlay upper dir to a
        per-host path and fans a mkdir to every node before start."""
        pipeline = self._create_pipeline('apt_root', hosts=self.REMOTE_HOSTS,
                                         container_overlay_root='/tmp')
        MockExec = self._start(
            pipeline, exit_codes={h: 0 for h in self.REMOTE_HOSTS})

        cmds = self._cmds(MockExec)
        self.assertIn('mkdir -p /tmp/apt_root/overlay', cmds)
        mkdir_idx = cmds.index('mkdir -p /tmp/apt_root/overlay')
        start_idx = next(i for i, c in enumerate(cmds)
                         if c.startswith('apptainer instance start'))
        self.assertLess(mkdir_idx, start_idx)

        start_cmd = cmds[start_idx]
        self.assertIn('--overlay /tmp/apt_root/overlay ', start_cmd)
        shared = self.jarvis.get_pipeline_shared_dir('apt_root')
        self.assertNotIn(str(shared / 'overlay'), start_cmd)
        # Shared overlay dir must NOT be created head-side in this mode
        self.assertFalse((shared / 'overlay').exists())
        # Both the mkdir and the start are fanned to all hosts
        for call in MockExec.call_args_list:
            self.assertIsInstance(call.args[1], PsshExecInfo)

    def test_overlay_disabled_omits_flag_and_mounts_host_tmp(self):
        """container_overlay: false drops --overlay entirely and lets the
        host /tmp mount normally (no --no-mount tmp)."""
        pipeline = self._create_pipeline('apt_nool', container_overlay=False)
        MockExec = self._start(pipeline)

        self.assertEqual(MockExec.call_count, 1)
        cmd = MockExec.call_args.args[0]
        self.assertNotIn('--overlay', cmd)
        self.assertNotIn('--no-mount', cmd)
        shared = self.jarvis.get_pipeline_shared_dir('apt_nool')
        self.assertFalse((shared / 'overlay').exists())

    def test_overlay_disabled_keeps_tmp_bind(self):
        """tmp_bind_root still applies when the overlay is disabled."""
        pipeline = self._create_pipeline('apt_notmp', container_overlay=False,
                                         tmp_bind_root='/scratch')
        MockExec = self._start(pipeline)

        cmds = self._cmds(MockExec)
        self.assertIn('mkdir -p /scratch/apt_notmp/tmp', cmds)
        start_cmd = cmds[-1]
        self.assertIn('--bind /scratch/apt_notmp/tmp:/tmp ', start_cmd)
        self.assertNotIn('--overlay', start_cmd)
        self.assertNotIn('--no-mount', start_cmd)

    def test_overlay_false_wins_over_overlay_root(self):
        """container_overlay: false gates the whole overlay feature,
        including the per-host mkdir fan-out for container_overlay_root."""
        pipeline = self._create_pipeline('apt_both', container_overlay=False,
                                         container_overlay_root='/tmp')
        MockExec = self._start(pipeline)

        for cmd in self._cmds(MockExec):
            self.assertNotIn('--overlay', cmd)
            self.assertNotIn('/overlay', cmd)

    def test_multi_node_default_overlay_raises(self):
        """A multi-host hostfile with the default shared overlay is the
        known-broken configuration: refuse to start, before any command
        runs or the overlay dir is created."""
        pipeline = self._create_pipeline('apt_guard', hosts=self.REMOTE_HOSTS)

        with patch('jarvis_cd.shell.Exec') as MockExec:
            with self.assertRaises(RuntimeError) as ctx:
                pipeline._start_containerized_pipeline()

        msg = str(ctx.exception)
        self.assertIn('container_overlay_root', msg)
        self.assertIn('container_overlay', msg)
        MockExec.assert_not_called()
        shared = self.jarvis.get_pipeline_shared_dir('apt_guard')
        self.assertFalse((shared / 'overlay').exists())

    def test_multi_node_overlay_disabled_no_raise(self):
        """Multi-host is fine once the shared overlay is out of the
        picture; the start command fans out via PsshExecInfo."""
        pipeline = self._create_pipeline('apt_mnool', hosts=self.REMOTE_HOSTS,
                                         container_overlay=False)
        MockExec = self._start(
            pipeline, exit_codes={h: 0 for h in self.REMOTE_HOSTS})

        self.assertEqual(MockExec.call_count, 1)
        cmd, exec_info = MockExec.call_args.args
        self.assertNotIn('--overlay', cmd)
        self.assertIsInstance(exec_info, PsshExecInfo)

    def test_partial_start_failure_raises_naming_host(self):
        """An instance start that fails on a subset of hosts raises
        immediately, naming the failed host, instead of hanging later."""
        pipeline = self._create_pipeline('apt_fail', hosts=self.REMOTE_HOSTS,
                                         container_overlay_root='/tmp')
        codes = {'node-01': 0, 'node-02': 255, 'node-03': 0, 'node-04': 0}

        with self.assertRaises(RuntimeError) as ctx:
            self._start(pipeline, exit_codes=codes)

        self.assertIn('node-02', str(ctx.exception))
        self.assertIn('255', str(ctx.exception))


class TestApptainerConfigRoundTrip(ApptainerStartTestBase):

    def test_overlay_keys_roundtrip_config(self):
        """Non-default values survive save() -> load-from-config. The
        default-True container_overlay needs an inverted save idiom: a
        truthy-only save would silently drop False."""
        self._create_pipeline('apt_rt', container_overlay=False,
                              container_overlay_root='/mnt/nvme/me')

        p2 = Pipeline('apt_rt')
        self.assertIs(p2.container_overlay, False)
        self.assertEqual(p2.container_overlay_root, '/mnt/nvme/me')

    def test_overlay_defaults_roundtrip_config(self):
        """Defaults survive a round trip and stay out of the saved file."""
        self._create_pipeline('apt_rtdef')

        p2 = Pipeline('apt_rtdef')
        self.assertIs(p2.container_overlay, True)
        self.assertIsNone(p2.container_overlay_root)

        config_file = self.jarvis.get_pipeline_dir('apt_rtdef') / 'pipeline.yaml'
        with open(config_file) as f:
            saved = yaml.safe_load(f)
        self.assertNotIn('container_overlay', saved)
        self.assertNotIn('container_overlay_root', saved)

    def test_tmp_bind_root_roundtrip_config(self):
        """tmp_bind_root survives save() -> load-from-config, so a
        pipeline rebuilt from its saved config (ppl start/stop/run)
        keeps its per-host /tmp bind."""
        self._create_pipeline('apt_tbr', tmp_bind_root='/mnt/nvme/me')

        p2 = Pipeline('apt_tbr')
        self.assertEqual(p2.tmp_bind_root, '/mnt/nvme/me')

    def test_container_gpu_roundtrip_config(self):
        """container_gpu survives save() -> load-from-config."""
        self._create_pipeline('apt_gpu', container_gpu=True)

        p2 = Pipeline('apt_gpu')
        self.assertIs(p2.container_gpu, True)

    def test_yaml_load_overlay_keys_expandvars(self):
        """YAML load honors both keys and expands env vars in the root
        path, mirroring the tmp_bind_root contract."""
        os.environ['JARVIS_TEST_OVERLAY'] = '/mnt/testnvme'
        try:
            yaml_path = os.path.join(self.test_dir, 'apt_yaml.yaml')
            with open(yaml_path, 'w') as f:
                f.write(
                    'name: apt_yaml\n'
                    'container_engine: apptainer\n'
                    'container_overlay: false\n'
                    'container_overlay_root: $JARVIS_TEST_OVERLAY/root\n'
                    'pkgs: []\n'
                )
            pipeline = Pipeline()
            pipeline.load('yaml', yaml_path)
            self.assertIs(pipeline.container_overlay, False)
            self.assertEqual(pipeline.container_overlay_root,
                             '/mnt/testnvme/root')
        finally:
            del os.environ['JARVIS_TEST_OVERLAY']


if __name__ == '__main__':
    unittest.main()
