"""
Test CLI unit test class
"""
from jarvis_util.shell.local_exec import LocalExecInfo
from jarvis_util.shell.exec import Exec
from jarvis_cd.basic.jarvis_manager import JarvisManager
from jarvis_cd.basic.pkg import Pipeline
from unittest import TestCase
import os
import yaml


class TestCli(TestCase):
    """
    Test jarvis CLI
    """
    def add_test_repo(self):
        self.jarvis = JarvisManager.get_instance()
        path = f'{self.jarvis.jarvis_root}/test/unit/test_repo'
        Exec(f'jarvis repo add {path}')
        self.jarvis.load()

    def rm_test_repo(self):
        self.jarvis = JarvisManager.get_instance()
        Exec('jarvis repo remove test_repo')
        self.jarvis.load()

    def test_jarvis_repo(self):
        # Add repo
        self.jarvis = JarvisManager.get_instance()
        self.add_test_repo()
        repo = self.jarvis.get_repo('test_repo')
        self.assertEqual(repo['name'], 'test_repo')

        # Promote repo
        self.assertEqual(self.jarvis.repos[0]['name'], 'test_repo')
        Exec('jarvis repo promote builtin')
        self.jarvis.load()
        self.assertEqual(self.jarvis.repos[0]['name'], 'builtin')

        # Remove repo
        self.rm_test_repo()
        repo = self.jarvis.get_repo('test_repo')
        self.assertTrue(repo is None)

    def test_jarvis_create_cd_rm(self):
        self.jarvis = JarvisManager.get_instance()
        # Create pipelines
        Exec('jarvis pipeline create test_pipeline')
        Exec('jarvis pipeline create test_pipeline2')
        self.assertTrue(os.path.exists(
            f'{self.jarvis.config_dir}/test_pipeline'))
        self.assertTrue(os.path.exists(
            f'{self.jarvis.config_dir}/test_pipeline/test_pipeline.yaml'))
        self.jarvis.load()

        # Cd into test_pipeline
        self.assertEqual(self.jarvis.cur_pipeline, 'test_pipeline2')
        Exec('jarvis cd test_pipeline')
        self.jarvis.load()
        self.assertEqual(self.jarvis.cur_pipeline, 'test_pipeline')

        # Get path to the pipeline
        pkg = Exec('jarvis path test_pipeline',
                    LocalExecInfo(collect_output=True))
        path = pkg.stdout['localhost'].strip()
        self.assertEqual(path, f'{self.jarvis.config_dir}/test_pipeline')

        # Delete the pipelines
        Exec('jarvis pipeline destroy test_pipeline')
        Exec('jarvis pipeline destroy test_pipeline2')
        self.assertFalse(
            os.path.exists(f'{self.jarvis.config_dir}/test_pipeline'))
        self.assertFalse(
            os.path.exists(f'{self.jarvis.config_dir}/test_pipeline2'))

    def test_jarvis_append(self):
        self.jarvis = JarvisManager.get_instance()

        # Remove the pipeline
        Exec('jarvis pipeline destroy test_pipeline')
        self.assertTrue(
            not os.path.exists(f'{self.jarvis.config_dir}/test_pipeline'))
        self.rm_test_repo()

        # Create the pipeline
        self.add_test_repo()
        Exec('jarvis pipeline create test_pipeline')
        self.assertTrue(
            os.path.exists(f'{self.jarvis.config_dir}/test_pipeline'))
        exec_pkg = Exec('jarvis pipeline append first '
                         '--port=22 --devices=[[nvme,1]]',
                         LocalExecInfo(collect_output=True))
        self.assertTrue(
            os.path.exists(f'{self.jarvis.config_dir}/test_pipeline/first'))
        config_dict = yaml.safe_load(exec_pkg.stdout['localhost'])
        self.assertTrue(config_dict['port'] == 22)
        self.assertTrue(config_dict['devices'] == [['nvme', 1]])
        Exec('jarvis pipeline append second')
        self.assertTrue(
            os.path.exists(f'{self.jarvis.config_dir}/test_pipeline/second'))
        Exec('jarvis pipeline append third')
        self.assertTrue(
            os.path.exists(f'{self.jarvis.config_dir}/test_pipeline/third'))

        # Start the pipeline
        exec_pkg = Exec('jarvis pipeline start',
                         LocalExecInfo(collect_output=True))
        expected_lines = ['first start',
                          'second modify_env',
                          'third start']
        self.verify_pipeline(exec_pkg.stdout, expected_lines)

        # Stop the pipeline
        exec_pkg = Exec('jarvis pipeline stop',
                         LocalExecInfo(collect_output=True))
        expected_lines = ['third stop',
                          'first stop']
        self.verify_pipeline(exec_pkg.stdout, expected_lines)

        # Clean the pipeline
        exec_pkg = Exec('jarvis pipeline clean',
                         LocalExecInfo(collect_output=True))
        expected_lines = ['third clean',
                          'first clean']
        self.verify_pipeline(exec_pkg.stdout, expected_lines)

        # Status the pipeline
        exec_pkg = Exec('jarvis pipeline status',
                         LocalExecInfo(collect_output=True))
        expected_lines = ['first status']
        self.verify_pipeline(exec_pkg.stdout, expected_lines)

        # Remove the pipeline
        Exec('jarvis pipeline destroy test_pipeline')
        self.assertTrue(
            not os.path.exists(f'{self.jarvis.config_dir}/test_pipeline'))
        self.rm_test_repo()

    def test_jarvis_load_yaml(self):
        self.jarvis = JarvisManager.get_instance()
        path = f'{self.jarvis.jarvis_root}/test/unit/pipeline.yaml'
        pipeline = Pipeline().from_yaml(path).save()
        self.jarvis.cd(pipeline.global_id)
        self.jarvis.save()

    def verify_pipeline(self, stdout, expected_lines):
        lines = stdout['localhost'].strip().splitlines()
        for line, expected_line in zip(lines, expected_lines):
            self.assertEqual(line, expected_line)


