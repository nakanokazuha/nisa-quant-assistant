import unittest
from pathlib import Path


ROOT = Path(__file__).parent.parent
DOCS = (
    ROOT / "NISA_QUANT_ASSISTANT_SPEC.md",
    ROOT / "NISA_QUANT_ASSISTANT_PLAN.md",
    ROOT / "NISA_AI_INTEGRATION_REPORT.md",
)
ACTIVE = "Active boundary: Phase 2 is a read-only evidence layer for US-listed S&P 500 constituent equities."
ACTIVE_PLAN = "Active boundary: implement the standalone read-only US S&P 500 evidence layer"
LEGACY_IMPLEMENTED = "Implemented now: local fixture/CSV/SQLite/CLI only."
LEGACY_DEFERRED = "Deferred roadmap: Hermes/live providers/scheduling/delivery."
LEGACY_PROHIBITED = "Prohibited: credentials, broker login/write/order execution, Discord delivery, and Yume iOS changes."
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
        for path in (ROOT / "NISA_QUANT_ASSISTANT_SPEC.md", ROOT / "NISA_QUANT_ASSISTANT_PLAN.md"):
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertIn(ACTIVE if path.name.endswith("SPEC.md") else ACTIVE_PLAN, text)
                if path.name.endswith("SPEC.md"):
                    self.assertIn("Phase 3", text)
                self.assertIn("Yume iOS", text)
        historical = (ROOT / "NISA_AI_INTEGRATION_REPORT.md").read_text(encoding="utf-8")
        self.assertIn(LEGACY_IMPLEMENTED, historical)
        self.assertIn(LEGACY_DEFERRED, historical)
        self.assertIn(LEGACY_PROHIBITED, historical)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("Phase 2 evidence slice", readme)
        self.assertIn("US-listed S&P 500 constituent equities", readme)
        self.assertIn("phase2-refresh-fixtures", readme)
        self.assertIn("phase2-refresh-config", readme)
        sources = (ROOT / "docs/phase2-sources.md").read_text(encoding="utf-8")
        for phrase in (
            "S&P 500", "Alpha Vantage", "SEC EDGAR", "RSS", "10 requests/second",
            "survivorship", "metadata_only", "large_flow_proxy", "evidence only",
            "Company Facts", "explicit ticker/CIK", "configuration-dependent",
        ):
            self.assertIn(phrase, sources)

    def test_every_checked_in_research_delegation_and_plan_artifact_has_local_boundary(self) -> None:
        for path in research_artifacts():
            with self.subTest(path=path.relative_to(ROOT)):
                text = path.read_text(encoding="utf-8")
                opening = "\n".join(text.splitlines()[:8])
                if path.name in {"NISA_QUANT_ASSISTANT_SPEC.md"}:
                    self.assertIn(ACTIVE, opening)
                    self.assertIn("Phase 3", opening)
                elif path.name in {"NISA_QUANT_ASSISTANT_PLAN.md"}:
                    self.assertIn(ACTIVE_PLAN, opening)
                    self.assertIn("Phase 2", opening)
                elif path.name in {"2026-09-01-nisa-phase2-r1.md"}:
                    self.assertIn("Phase 2", opening)
                    self.assertIn("read-only", opening)
                else:
                    self.assertIn(MARKER, opening)
                    self.assertIn("Release boundary:", opening)
                    self.assertIn("Implemented now:", opening)
                    self.assertIn("Deferred", opening)
                    self.assertIn("Prohibited", opening)


if __name__ == "__main__":
    unittest.main()
