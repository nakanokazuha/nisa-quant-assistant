import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).parent.parent
LEGACY_MODULES = (
    "nisa_quant.phase3" + "_producer",
    "nisa_quant.phase3" + "_reporting",
)


def current_repository_paths() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return [ROOT / relative for relative in result.stdout.splitlines() if (ROOT / relative).is_file()]


class RepositoryNamingTests(unittest.TestCase):
    def test_current_repository_has_no_phase3_underscore_basenames(self) -> None:
        paths = current_repository_paths()
        offenders = [path.relative_to(ROOT) for path in paths if "phase3_" in path.name]
        self.assertEqual([], offenders)

    def test_python_sources_have_no_legacy_phase3_module_imports(self) -> None:
        for path in current_repository_paths():
            if path.suffix != ".py":
                continue
            source = path.read_text(encoding="utf-8")
            for module in LEGACY_MODULES:
                self.assertNotIn(f"from {module} ", source, path)
                self.assertNotIn(f"import {module}", source, path)


if __name__ == "__main__":
    unittest.main()
