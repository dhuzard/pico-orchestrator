"""Every first-party Python source must compile, so corruption (for example a
literal backslash-n spliced into code) fails tests with a precise location."""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class PythonSourcesCompileTest(unittest.TestCase):
    def test_tools_and_tests_compile(self):
        sources = sorted([*ROOT.glob("tools/*.py"), *ROOT.glob("tests/*.py")])
        self.assertTrue(sources)
        for path in sources:
            with self.subTest(path=path.relative_to(ROOT)):
                compile(path.read_text(encoding="utf-8"), str(path), "exec")

    def test_launcher_scripts_have_valid_bash_syntax(self):
        import subprocess
        for path in sorted(ROOT.glob("scripts/*.sh")):
            with self.subTest(path=path.relative_to(ROOT)):
                result = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
