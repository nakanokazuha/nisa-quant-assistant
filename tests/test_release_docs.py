import unittest
from pathlib import Path


ROOT = Path(__file__).parent.parent
DOCS = (
    ROOT / "NISA_QUANT_ASSISTANT_SPEC.md",
    ROOT / "NISA_QUANT_ASSISTANT_PLAN.md",
    ROOT / "NISA_AI_INTEGRATION_REPORT.md",
)
IMPLEMENTED = "Implemented now: local fixture/CSV/SQLite/CLI only."
DEFERRED = "Deferred/not implemented: Hermes runtime integration, live providers, scheduling, and delivery."
PROHIBITED = "Prohibited boundaries: credentials, broker login/write/order execution, Discord delivery, and Yume iOS changes."
MARKER = "Historical research / deferred — not active implementation instructions"


def research_artifacts() -> list[Path]:
    paths = {
        path
        for pattern in ("NISA_*.md", "deleg*.*", "subagent*.*", "verified-repos.md")
        for path in ROOT.glob(pattern)
        if path.is_file()
    }
    paths.update(path for path in (ROOT / "docs/superpowers/plans").glob("*") if path.is_file())
    return sorted(paths)


class ReleaseDocumentationTests(unittest.TestCase):
    def test_release_docs_share_the_local_only_boundary(self) -> None:
        for path in DOCS:
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertIn(IMPLEMENTED, text)
                self.assertIn(DEFERRED, text)
                self.assertIn(PROHIBITED, text)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("This standalone Python package is a local-first", readme)
        self.assertIn("It has no network client", readme)

    def test_every_checked_in_research_delegation_and_plan_artifact_has_local_boundary(self) -> None:
        for path in research_artifacts():
            with self.subTest(path=path.relative_to(ROOT)):
                text = path.read_text(encoding="utf-8")
                opening = "\n".join(text.splitlines()[:5])
                self.assertIn(MARKER, opening)
                self.assertIn("Release boundary:", opening)
                self.assertIn("Implemented now:", opening)
                self.assertIn("Deferred", opening)
                self.assertIn("Prohibited", opening)


if __name__ == "__main__":
    unittest.main()
