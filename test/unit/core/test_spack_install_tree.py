"""The install tree of a jarvis Spack keeps Spack's sbang line under Linux's limit."""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jarvis_cd.core import spack_site
from jarvis_cd.core.issues import JarvisIssueError
from jarvis_cd.core.spack_install import InstallContext, prepare_chained_spack
from jarvis_cd.core.spack_site import (
    INSTALL_BASE_ENV,
    SHEBANG_LIMIT,
    SITE_PREP_RECORD,
    jarvis_install_tree,
)


class TestJarvisInstallTree(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.temp, ignore_errors=True)
        env = {key: value for key, value in os.environ.items() if key != INSTALL_BASE_ENV}
        env["HOME"] = str(self.temp / "h")
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _long_root(self):
        root = self.temp / ("adapter-" + "x" * 120) / "jarvis-cd" / "site" / "spacks" / "0.23.1-9f5c"
        self.assertGreater(len(spack_site.sbang_shebang(root / "opt" / "spack")), SHEBANG_LIMIT)
        return root

    def test_short_root_keeps_spack_default(self):
        tree = jarvis_install_tree(self.temp / "s")
        self.assertEqual(tree["source"], "default")
        self.assertEqual(tree["root"], str(self.temp / "s" / "opt" / "spack"))

    def test_long_root_moves_to_short_base_per_root(self):
        root = self._long_root()
        tree = jarvis_install_tree(root)
        self.assertEqual(tree["source"], "short_base")
        self.assertEqual(Path(tree["root"]).parent, self.temp / "h" / ".jarvis-spack")
        self.assertLessEqual(tree["sbang_line_length"], SHEBANG_LIMIT)
        self.assertGreater(tree["default_sbang_line_length"], SHEBANG_LIMIT)
        self.assertEqual(jarvis_install_tree(root)["root"], tree["root"])
        self.assertNotEqual(jarvis_install_tree(root.parent / "other")["root"], tree["root"])

    def test_base_from_environment_is_used(self):
        os.environ[INSTALL_BASE_ENV] = str(self.temp / "b")
        tree = jarvis_install_tree(self.temp / "s")
        self.assertEqual(tree["source"], INSTALL_BASE_ENV)
        self.assertEqual(Path(tree["root"]).parent, self.temp / "b")

    def test_too_long_base_is_refused_with_the_line(self):
        os.environ[INSTALL_BASE_ENV] = str(self.temp / ("b" * 130))
        with self.assertRaises(JarvisIssueError) as raised:
            jarvis_install_tree(self.temp / "s")
        issue = raised.exception.issues[0]
        self.assertEqual(issue["kind"], "spack_install_tree_too_long")
        self.assertEqual(issue["field"], INSTALL_BASE_ENV)
        self.assertGreater(issue["details"]["length"], SHEBANG_LIMIT)

    def test_root_prepared_with_the_old_tree_is_prepared_again(self):
        root = self._long_root()
        root.mkdir(parents=True)
        site = Path("/site/spack")
        (root / SITE_PREP_RECORD).write_text(json.dumps(
            {"status": "prepared", "site_spack": str(site)}), encoding="utf-8")
        context = InstallContext(install_dir=self.temp / "install").open()
        try:
            with mock.patch.object(spack_site, "prepare_site",
                                   return_value={"status": "prepared"}) as prepare:
                prepare_chained_spack(context, site, root)
                self.assertEqual(prepare.call_count, 1)
                (root / SITE_PREP_RECORD).write_text(json.dumps(
                    {"status": "prepared", "site_spack": str(site),
                     "install_tree": jarvis_install_tree(root)}), encoding="utf-8")
                self.assertIsNone(prepare_chained_spack(context, site, root))
                self.assertEqual(prepare.call_count, 1)
        finally:
            context.close()


if __name__ == "__main__":
    unittest.main()
