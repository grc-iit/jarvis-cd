from jarvis_util.shell.local_exec import LocalExec, LocalExecInfo
from jarvis_util.shell.exec import Exec
from jarvis_cd.basic.jarvis_manager import JarvisManager
import pathlib
from unittest import TestCase
import os
import shutil


class TestCli(TestCase):
    def add_test_repo(self):
        self.jarvis = JarvisManager.get_instance()
        path = f'{self.jarvis.jarvis_root}/test/unit/test_repo'
        Exec(f'jarvis repo add {path}')
        self.jarvis.load()

    def rm_test_repo(self):
        self.jarvis = JarvisManager.get_instance()
        Exec(f'jarvis repo remove test_repo')
        self.jarvis.load()

    def test_jarvis_repo(self):
        # Add repo
        self.add_test_repo()
        repo = self.get_repo('test_repo')
        self.assertEqual(repo['name'], 'test_repo')

        # Promote repo
        self.assertEqual(self.jarvis.repos[0], 'test_repo')
        Exec(f'jarvis repo promote builtin')
        self.assertEqual(self.jarvis.repos[0], 'buitlin')

        # Remove repo
        self.rm_test_repo()
        repo = self.get_repo('test_repo')
        self.assertTrue(repo is None)

    def test_jarvis_create_cd_rm(self):
        self.jarvis = JarvisManager.get_instance()
        # Create pipelines
        Exec('jarvis create test_pipeline')
        Exec('jarvis create test_pipeline2')
        self.assertTrue(os.path.exists(
            f'{self.jarvis.config_dir}/test_pipeline'))
        self.assertTrue(os.path.exists(
            f'{self.jarvis.config_dir}/test_pipeline/test_pipeline.yaml'))
        self.jarvis.load()

        # Cd into test_pipeline
        self.assertEqual(self.jarvis.cur_pipeline, 'test_pipeline2')
        Exec('jarvis cd test_pipeline')
        self.load()
        self.assertEqual(self.jarvis.cur_pipeline, 'test_pipeline')

        # Get path to the pipeline
        node = Exec('jarvis path test_pipeline',
                    LocalExecInfo(collect_output=True))
        path = node.stdout.strip()
        self.assertEqual(path, f'{self.jarvis.config_dir}/test_pipeline')

        # Delete the pipelines
        Exec('jarvis destroy test_pipeline')
        Exec('jarvis destroy test_pipeline2')
        self.assertFalse(
            os.path.exists(f'{self.jarvis_config_dir}/test_pipeline'))
        self.assertFalse(
            os.path.exists(f'{self.jarvis_config_dir}/test_pipeline2'))

    def test_jarvis_append(self):
        self.add_test_repo()
        Exec('jarvis create test_pipeline')
        Exec('jarvis append first')
        Exec('jarvis append second')
        Exec('jarvis append third')
        Exec('jarvis start')
        Exec('jarvis stop')
        Exec('jarvis clean')
        Exec('jarvis status')
        self.rm_test_repo()
