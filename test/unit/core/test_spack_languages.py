"""LANGUAGE_SCRIPT reads the languages a package needs from every Spack layout."""
import contextlib
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from jarvis_cd.core.spack_install import LANGUAGE_SCRIPT


class _Spec:
    def __init__(self, text=""):
        self.text = str(text).strip()
        head = self.text.split()[0] if self.text else ""
        self.name = head if head and head[0].isalpha() and "=" not in head else ""
        for mark in "@%+~^":
            self.name = self.name.split(mark)[0]

    def _tokens(self):
        tokens = set()
        for word in self.text.replace("+", " +").replace("~", " ~").split():
            tokens.add(word)
        return tokens

    def intersects(self, other):
        return other._tokens() <= self._tokens() | {self.name}

    def __str__(self):
        return self.text


def _fake_spack(classes):
    spack = types.ModuleType("spack")
    repo = types.ModuleType("spack.repo")
    spec = types.ModuleType("spack.spec")
    setattr(spec, "Spec", _Spec)
    setattr(repo, "PATH", types.SimpleNamespace(
        is_virtual=lambda name: name in ("c", "cxx", "fortran"),
        get_pkg_class=lambda name: classes[name],
    ))
    setattr(spack, "repo", repo)
    setattr(spack, "spec", spec)
    return {"spack": spack, "spack.repo": repo, "spack.spec": spec}


def _run(classes, nodes):
    with tempfile.TemporaryDirectory() as tmp:
        request = Path(tmp) / "in.json"
        request.write_text(json.dumps(nodes), encoding="utf-8")
        out = io.StringIO()
        with mock.patch.dict(sys.modules, _fake_spack(classes)), \
                mock.patch.object(sys, "argv", ["spack-languages.py", str(request)]), \
                contextlib.redirect_stdout(out):
            exec(compile(LANGUAGE_SCRIPT, "spack-languages.py", "exec"), {"__name__": "__main__"})
    line = [line for line in out.getvalue().splitlines() if line.startswith("JARVIS_LANGUAGES ")][-1]
    return json.loads(line.split(" ", 1)[1])["nodes"]


class TestLanguageScript(unittest.TestCase):
    def test_spack_023_languages_directive(self):
        # Spack 0.22/0.23: depends_on("fortran") lands in cls.languages,
        # and cls.dependencies holds none of the language virtuals
        xcompact3d = types.SimpleNamespace(
            dependencies={_Spec(""): {"cmake": object(), "mpi": object()}},
            languages={_Spec(""): {"c", "cxx", "fortran"}, _Spec("+gpu"): {"cuda"}},
        )
        cpp_only = types.SimpleNamespace(
            dependencies={_Spec(""): {"cmake": object()}},
            languages={_Spec(""): {"cxx"}, _Spec("+fortran"): {"fortran"}},
        )
        nodes = _run(
            {"xcompact3d": xcompact3d, "lib": cpp_only},
            [{"name": "xcompact3d", "hash": "jep2ktg",
              "spec": "xcompact3d@5.0%gcc@12.3.0 +clio"},
             {"name": "lib", "hash": "aaaaaaa", "spec": "lib@1.0%gcc@12.3.0 ~fortran"}],
        )
        self.assertEqual(nodes["jep2ktg"], ["c", "cxx", "fortran"])
        self.assertEqual(nodes["aaaaaaa"], ["cxx"])

    def test_spack_10_language_dependencies(self):
        decomp = types.SimpleNamespace(
            dependencies={_Spec(""): {"fortran": object(), "c": object(), "mpi": object()}},
        )
        nodes = _run({"decomp2d-fft": decomp},
                     [{"name": "decomp2d-fft", "hash": "l2llq26", "spec": "decomp2d-fft@2.0"}])
        self.assertEqual(nodes["l2llq26"], ["c", "fortran"])


class TestNoLanguageCompiler(unittest.TestCase):
    def test_names_missing_language_and_checked_compilers(self):
        from jarvis_cd.core.spack_install import no_language_compiler_issue

        issue = no_language_compiler_issue(
            host="ares-comp-11",
            needs={"c": ["xcompact3d"], "fortran": ["xcompact3d", "decomp2d-fft"]},
            usable=[{"spec": "gcc@12.3.0", "languages": ["c"]},
                    {"spec": "clang@14.0.0", "languages": ["c", "cxx"]}],
            origin="build host check (probed)",
        )
        self.assertEqual(issue["kind"], "compiler_unavailable")
        self.assertFalse(issue["answerable"])
        self.assertEqual(issue["missing_languages"], ["fortran"])
        for text in ("No fortran compiler on ares-comp-11", "gcc@12.3.0 has c",
                     "clang@14.0.0 has c,cxx", "decomp2d-fft"):
            self.assertIn(text, issue["message"])


if __name__ == "__main__":
    unittest.main()
