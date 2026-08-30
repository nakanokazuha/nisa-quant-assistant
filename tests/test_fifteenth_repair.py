import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nisa_quant.schema import connect_database, initialize_database
from nisa_quant.reports import validate_report
from nisa_quant.watchlist import materialize_current, watchlist_as_of


SOURCE_ID = "SRC-abcdef123456"


def new_connection() -> sqlite3.Connection:
    connection = connect_database(":memory:")
    initialize_database(connection)
    return connection


def minimal_report(prose: str) -> str:
    return f"""# NISA Quant Assistant Report
## Data cutoffs
- prices: `2026-01-01`
## Data warnings
- None
## Ranked candidates
| Instrument | Account | Label | Evidence | Metrics | Sources |
|---|---|---|---|---|---|
| VT | watchlist | WATCH | limited | current_price=100 | [{SOURCE_ID}] |
**VT — WATCH**
- Reason: {prose}
## Source list
- [{SOURCE_ID}] local
Manual review required; no order was placed.
"""


def renderer_report() -> str:
    return f"""# NISA Quant Assistant Report

Report generated from local snapshot as of `2026-01-01`.
Provider/model identifier: `local`; template: `v1`.
Report contract fingerprint: `{'0' * 64}`.

## Data cutoffs
- prices: `2026-01-01`
## Data warnings
- None
## Ranked candidates
| Instrument | Account | Label | Evidence | Metrics | Sources |
|---|---|---|---|---|---|
| VT | watchlist | WATCH | limited | current_price=100 | [{SOURCE_ID}] |
**VT — WATCH**
- Reason: stable evidence
## Source list
- [{SOURCE_ID}] local
Manual review required; no order was placed.
"""


class FifteenthRepairWatchlistTests(unittest.TestCase):
    def test_newer_partial_version_cannot_replace_legacy_current_projection(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        connection.execute("ALTER TABLE watchlist ADD COLUMN effective_date TEXT")
        connection.execute("ALTER TABLE watchlist ADD COLUMN observed_at TEXT")
        connection.execute(
            """
            INSERT INTO watchlist(
                identifier_value, identifier_type, display_name, asset_type,
                market, currency, benchmark, notes, effective_date, observed_at
            ) VALUES ('SAME', 'other', 'CURRENT NAME', 'ETF', 'JPX', 'JPY',
                      'NIKKEI', 'legacy', '2026-01-01',
                      '2026-01-01T00:00:00+00:00')
            """
        )
        connection.execute(
            """
            INSERT INTO watchlist_versions(
                identifier_value, identifier_type, display_name, asset_type,
                market, currency, benchmark, notes, effective_from, observed_at
            ) VALUES ('SAME', 'other', 'PARTIAL NEWER', 'ETF', 'NYSE', 'USD',
                      NULL, 'partial', '2026-01-02',
                      '2026-01-02T00:00:00+00:00')
            """
        )
        connection.commit()

        initialize_database(connection)
        initialize_database(connection)

        current = connection.execute(
            "SELECT display_name, market, currency FROM watchlist WHERE identifier_value = 'SAME'"
        ).fetchone()
        as_of_newer_version = watchlist_as_of(connection, as_of="2026-01-02")
        self.assertEqual(tuple(current), ("CURRENT NAME", "JPX", "JPY"))
        self.assertEqual(
            [(row["display_name"], row["market"], row["currency"]) for row in as_of_newer_version],
            [("CURRENT NAME", "JPX", "JPY")],
        )
        self.assertEqual(
            connection.execute(
                "SELECT display_name FROM watchlist_versions "
                "WHERE identifier_value = 'SAME' AND effective_from = '2026-01-02'"
            ).fetchone()[0],
            "PARTIAL NEWER",
        )

    def test_direct_projection_rebuild_creates_safe_override_for_partial_collision(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        connection.execute("ALTER TABLE watchlist ADD COLUMN effective_date TEXT")
        connection.execute("ALTER TABLE watchlist ADD COLUMN observed_at TEXT")
        today = "2026-01-02"
        connection.execute(
            """
            INSERT INTO watchlist(
                identifier_value, identifier_type, display_name, asset_type,
                market, currency, benchmark, notes, effective_date, observed_at
            ) VALUES ('SAME', 'other', 'CURRENT NAME', 'ETF', 'JPX', 'JPY',
                      'NIKKEI', 'legacy', ?, ?)
            """,
            (today, f"{today}T00:00:00+00:00"),
        )
        connection.execute(
            """
            INSERT INTO watchlist_versions(
                identifier_value, identifier_type, display_name, asset_type,
                market, currency, benchmark, notes, effective_from, observed_at
            ) VALUES ('SAME', 'other', 'PARTIAL NEWER', 'ETF', 'NYSE', 'USD',
                      NULL, 'partial', ?, ?)
            """,
            (today, f"{today}T00:00:00+00:00"),
        )
        connection.commit()

        materialize_current(connection, as_of=today)

        current = connection.execute(
            "SELECT display_name, market, currency FROM watchlist WHERE identifier_value = 'SAME'"
        ).fetchone()
        point_in_time = watchlist_as_of(connection, as_of=today)
        self.assertEqual(tuple(current), ("CURRENT NAME", "JPX", "JPY"))
        self.assertEqual(
            [(row["display_name"], row["market"], row["currency"]) for row in point_in_time],
            [("CURRENT NAME", "JPX", "JPY")],
        )
        self.assertEqual(
            connection.execute(
                "SELECT display_name FROM watchlist_projection_overrides "
                "WHERE identifier_value = 'SAME' AND effective_from = ?",
                (today,),
            ).fetchone()[0],
            "CURRENT NAME",
        )

    def test_direct_projection_rebuild_clamps_legacy_observation_for_newer_collision(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        connection.execute("ALTER TABLE watchlist ADD COLUMN effective_date TEXT")
        connection.execute("ALTER TABLE watchlist ADD COLUMN observed_at TEXT")
        connection.execute(
            """
            INSERT INTO watchlist(
                identifier_value, identifier_type, display_name, asset_type,
                market, currency, benchmark, notes, effective_date, observed_at
            ) VALUES ('SAME', 'other', 'CURRENT NAME', 'ETF', 'JPX', 'JPY',
                      'NIKKEI', 'legacy', '2026-01-01', '2026-01-01T00:00:00+00:00')
            """
        )
        connection.execute(
            """
            INSERT INTO watchlist_versions(
                identifier_value, identifier_type, display_name, asset_type,
                market, currency, benchmark, notes, effective_from, observed_at
            ) VALUES ('SAME', 'other', 'PARTIAL NEWER', 'ETF', 'NYSE', 'USD',
                      NULL, 'partial', '2026-01-02', '2026-01-02T00:00:00+00:00')
            """
        )
        connection.commit()

        materialize_current(connection, as_of="2026-01-02")

        self.assertEqual(
            tuple(connection.execute(
                "SELECT display_name, market, currency FROM watchlist "
                "WHERE identifier_value = 'SAME'"
            ).fetchone()),
            ("CURRENT NAME", "JPX", "JPY"),
        )
        self.assertEqual(
            [(row["display_name"], row["market"], row["currency"]) for row in watchlist_as_of(connection, as_of="2026-01-02")],
            [("CURRENT NAME", "JPX", "JPY")],
        )


class FifteenthRepairReportSafetyTests(unittest.TestCase):
    def test_separator_actions_disclosures_and_new_secret_shapes_are_rejected(self) -> None:
        forbidden = (
            "B,U,Y now",
            "H:O:L:D now",
            "HOLD",
            "WATCH earnings closely",
            "REDUCE risk gradually",
            "BUY",
            "SELL",
            "PURCHASE",
            "ACCUMULATE",
            "a.c.c.o.u.n.t ID: ABC123",
            "For context my account ID is ABC",
            "Use customer identifier CLIENTABC for audit",
            "john-portfolio.pdf",
            "gho_abcdefghijklmnopqrstuvwxyz123456",
            "Authorization: Basic YWJj",
        )
        for phrase in forbidden:
            with self.subTest(phrase=phrase):
                with self.assertRaises(ValueError):
                    validate_report(minimal_report(phrase), source_records=[SOURCE_ID])

        validate_report(
            minimal_report("price-history.csv is safe; token is explanatory text"),
            source_records=[SOURCE_ID],
        )

    def test_structured_source_metadata_is_scanned_before_binding(self) -> None:
        for source_name in (
            "For context my account ID is ABC",
            "Authorization: Basic YWJj",
            "Please B,U,Y now",
        ):
            with self.subTest(source_name=source_name):
                with self.assertRaises(ValueError):
                    validate_report(
                        minimal_report("stable evidence"),
                        source_records=[{"id": SOURCE_ID, "source_name": source_name}],
                    )

    def test_canonical_labels_are_allowed_only_in_renderer_owned_positions(self) -> None:
        with self.assertRaises(ValueError):
            validate_report(
                minimal_report("stable evidence")
                .replace("## Data warnings\n- None", "## Data warnings\n- **Injected — HOLD**"),
                source_records=[SOURCE_ID],
            )
        with self.assertRaises(ValueError):
            validate_report(
                minimal_report("stable evidence"),
                source_records=[{"id": SOURCE_ID, "label": "HOLD"}],
            )


class FifteenthRepairRendererTests(unittest.TestCase):
    def test_zero_width_renderer_markers_are_bound_as_renderer_reports(self) -> None:
        canonical = renderer_report()
        mutations = (
            "Report gen\u200b erated from local snapshot as of",
            "Provider/model identi\u200b fier:",
            "Report contract finger\u200b print:",
        )
        originals = (
            "Report generated from local snapshot as of",
            "Provider/model identifier:",
            "Report contract fingerprint:",
        )
        for original, mutation in zip(originals, mutations):
            with self.subTest(mutation=mutation):
                mutated = canonical.replace(original, mutation)
                with self.assertRaises(ValueError):
                    validate_report(mutated, source_records=[SOURCE_ID])

        normalized_mutations = (
            "Report gen\u200berated from local snapshot as of",
            "Provider/model identi\u200bfier:",
            "Report contract finger\u200bprint:",
        )
        for original, mutation in zip(originals, normalized_mutations):
            with self.subTest(normalized_mutation=mutation):
                with self.assertRaises(ValueError):
                    validate_report(
                        canonical.replace(original, mutation),
                        source_records=[SOURCE_ID],
                        provider="local",
                        template_version="v1",
                    )

    def test_renderer_marker_format_is_exact_and_binding_metadata_is_required(self) -> None:
        canonical = renderer_report()
        validate_report(
            canonical,
            source_records=[SOURCE_ID],
            provider="local",
            template_version="v1",
        )
        malformed = canonical.replace(
            "Provider/model identifier:", "Provider / model identifier :",
        )
        with self.assertRaises(ValueError):
            validate_report(
                malformed,
                source_records=[SOURCE_ID],
                provider="local",
                template_version="v1",
            )
        with self.assertRaises(ValueError):
            validate_report(
                canonical.replace("Provider/model identifier: `local`", "Provider/model identifier: `other`"),
                source_records=[SOURCE_ID],
                provider="local",
                template_version="v1",
            )
        with self.assertRaises(ValueError):
            validate_report(
                canonical.replace("Provider/model identifier: `local`; template: `v1`.\n", ""),
                source_records=[SOURCE_ID],
                provider="local",
                template_version="v1",
            )
        malformed_only = minimal_report("stable evidence") + (
            "\nReport contract finger print: `" + "0" * 64 + "`.\n"
        )
        with self.assertRaises(ValueError):
            validate_report(malformed_only, source_records=[SOURCE_ID])

    def test_standalone_whitespace_and_punctuation_marker_mutations_cannot_downgrade_validation(self) -> None:
        mutations = (
            "Reportgenerated from local snapshot as of `2026-01-01`.",
            "Report g e n e r a t e d from local snapshot as of `2026-01-01`.",
            "Providermodel identifier: `local`; template: `v1`.",
            "P r o v i d e r / m o d e l identifier: `local`; template: `v1`.",
            "Reportcontract fingerprint: `" + "0" * 64 + "`.",
            "Report c o n t r a c t f i n g e r p r i n t: `" + "0" * 64 + "`.",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                with self.assertRaises(ValueError):
                    validate_report(
                        minimal_report("stable evidence") + "\n" + mutation + "\n",
                        source_records=[SOURCE_ID],
                    )

    def test_malformed_marker_variant_is_rejected_even_alongside_canonical_renderer_markers(self) -> None:
        with self.assertRaises(ValueError):
            validate_report(
                renderer_report()
                + "Reportgenerated from local snapshot as of `2026-01-01`.\n",
                source_records=[SOURCE_ID],
                provider="local",
                template_version="v1",
            )


if __name__ == "__main__":
    unittest.main()
