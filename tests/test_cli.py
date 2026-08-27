import os
import subprocess
import sys
import unittest
from pathlib import Path


class CliTests(unittest.TestCase):
    def test_help_is_available_from_documented_src_layout_command(self) -> None:
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(Path(__file__).parent.parent / "src")
        result = subprocess.run(
            [sys.executable, "-m", "nisa_quant", "--help"],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("import-csv", result.stdout)
        self.assertIn("report", result.stdout)


if __name__ == "__main__":
    unittest.main()
