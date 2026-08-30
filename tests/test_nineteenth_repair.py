import hashlib
import json
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nisa_quant.journal import evaluate_recommendation, record_recommendation
from nisa_quant.metrics import calculate_snapshot
from nisa_quant.reports import render_report, validate_report
from nisa_quant.schema import connect_database, initialize_database
from nisa_quant.screens import run_screens
from nisa_quant.sources import add_source_record
from nisa_quant.watchlist import materialize_current, watchlist_as_of


SOURCE_ID = "SRC-abcdef123456"


def new_connection() -> sqlite3.Connection:
    connection = connect_database(":memory:")
    initialize_database(connection)
    return connection


def canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


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


def recommendation_fixture() -> tuple[sqlite3.Connection, int, str, str]:
    connection = new_connection()
    connection.execute(
        """
        INSERT INTO watchlist_versions(
            identifier_value, identifier_type, display_name, asset_type, market,
            currency, benchmark, benchmark_identifier_type, benchmark_identifier_value,
            notes, effective_from, observed_at
        ) VALUES ('ASSET', 'other', 'Asset', 'ETF', 'JPX', 'JPY', 'BENCH', 'other',
                  'BENCH', 'watch', '2026-01-01', '2026-01-01T00:00:00+00:00')
        """
    )
    connection.commit()
    materialize_current(connection, as_of="2026-01-01", allow_authoritative_update=True)
    add_source_record(
        connection, source_name="repair19", source_identifier="asset-before",
        retrieved_at="2026-01-01T00:00:00+00:00", observation_date="2026-01-01",
        instrument_identifier="ASSET", instrument_identifier_type="other", field="price",
        value="100", unit="price", currency="JPY", freshness_status="current",
        citation_location="repair19:asset-before", parser_version="repair19-v1",
    )
    add_source_record(
        connection, source_name="repair19", source_identifier="bench-before",
        retrieved_at="2026-01-01T00:00:00+00:00", observation_date="2026-01-01",
        instrument_identifier="BENCH", instrument_identifier_type="other", field="benchmark_price",
        value="10", unit="price", currency="JPY", freshness_status="current",
        citation_location="repair19:bench-before", parser_version="repair19-v1",
    )
    snapshot = calculate_snapshot(connection, as_of="2026-01-01")
    candidate = run_screens(connection, snapshot, as_of="2026-01-01")[0]
    recommendation_id = record_recommendation(
        connection, candidate, data_cutoff="2026-01-01", provider="local", template_version="v1",
        snapshot=snapshot,
    )
    observed_id = add_source_record(
        connection, source_name="repair19", source_identifier="asset-after",
        retrieved_at="2026-01-02T00:00:00+00:00", observation_date="2026-01-02",
        instrument_identifier="ASSET", instrument_identifier_type="other", field="price",
        value="110", unit="price", currency="JPY", freshness_status="current",
        citation_location="repair19:asset-after", parser_version="repair19-v1",
    )
    benchmark_id = add_source_record(
        connection, source_name="repair19", source_identifier="bench-after",
        retrieved_at="2026-01-02T00:00:00+00:00", observation_date="2026-01-02",
        instrument_identifier="BENCH", instrument_identifier_type="other", field="benchmark_price",
        value="11", unit="price", currency="JPY", freshness_status="current",
        citation_location="repair19:bench-after", parser_version="repair19-v1",
    )
    return connection, recommendation_id, observed_id, benchmark_id


class NineteenthRepairWatchlistTests(unittest.TestCase):
    def test_late_legacy_metadata_cannot_be_stamped_at_an_earlier_projection_cutoff(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        connection.execute("ALTER TABLE watchlist ADD COLUMN effective_date TEXT")
        connection.execute("ALTER TABLE watchlist ADD COLUMN observed_at TEXT")
        connection.execute(
            """
            INSERT INTO watchlist(
                identifier_value, identifier_type, display_name, asset_type, market,
                currency, benchmark, notes, effective_date, observed_at
            ) VALUES ('SAME', 'other', 'LATE LEGACY', 'ETF', 'NYSE', 'USD', NULL,
                      'late', '2026-06-01', '2026-08-30T00:00:00+00:00')
            """
        )
        connection.execute(
            """
            INSERT INTO watchlist_versions(
                identifier_value, identifier_type, display_name, asset_type, market,
                currency, benchmark, notes, effective_from, observed_at
            ) VALUES ('SAME', 'other', 'EARLY VERSION', 'ETF', 'JPX', 'JPY', NULL,
                      'early', '2026-06-01', '2026-06-01T00:00:00+00:00')
            """
        )
        connection.commit()

        materialize_current(connection, as_of="2026-06-01")

        self.assertEqual(
            tuple(connection.execute(
                "SELECT display_name, market, currency FROM watchlist WHERE identifier_value = 'SAME'"
            ).fetchone()),
            ("EARLY VERSION", "JPX", "JPY"),
        )
        self.assertEqual(
            tuple(watchlist_as_of(connection, as_of="2026-06-01")[0][field] for field in ("display_name", "market", "currency")),
            ("EARLY VERSION", "JPX", "JPY"),
        )


class NineteenthRepairJournalTests(unittest.TestCase):
    def _evaluate_fixture(self) -> tuple[sqlite3.Connection, int, str, str]:
        return recommendation_fixture()

    def test_valid_control_still_persists_an_outcome(self) -> None:
        connection, recommendation_id, observed_id, benchmark_id = self._evaluate_fixture()
        self.addCleanup(connection.close)
        evaluate_recommendation(
            connection, recommendation_id, evaluation_date="2026-01-02",
            observed_price_source_id=observed_id, benchmark_price_source_id=benchmark_id,
        )
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM recommendation_outcomes").fetchone()[0], 1)

    def test_persisted_metrics_require_the_exact_contract(self) -> None:
        mutations = {
            "integer price status": lambda metrics: metrics.__setitem__("price_status", 1),
            "removed quantity": lambda metrics: metrics.pop("quantity"),
            "arbitrary nested metric": lambda metrics: metrics.__setitem__("unexpected", {"bad": True}),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                connection, recommendation_id, observed_id, benchmark_id = self._evaluate_fixture()
                metrics = json.loads(connection.execute(
                    "SELECT metrics_json FROM recommendations WHERE id = ?", (recommendation_id,)
                ).fetchone()[0])
                mutate(metrics)
                connection.execute(
                    "UPDATE recommendations SET metrics_json = ? WHERE id = ?",
                    (json.dumps(metrics, allow_nan=False), recommendation_id),
                )
                connection.commit()
                with self.assertRaises(ValueError):
                    evaluate_recommendation(
                        connection, recommendation_id, evaluation_date="2026-01-02",
                        observed_price_source_id=observed_id, benchmark_price_source_id=benchmark_id,
                    )
                connection.close()

    def test_recomputed_snapshot_hash_cannot_bind_malformed_or_unknown_provenance(self) -> None:
        mutations = {
            "malformed retrieved_at": lambda snapshot: snapshot["sources"][0].__setitem__("retrieved_at", "not-a-time"),
            "unknown provenance entry": lambda snapshot: snapshot["portfolio"]["provenance"].__setitem__(
                "unknown", {"derivation": "attacker", "source_ids": []}
            ),
            "recomputed hash for changed source": lambda snapshot: snapshot["sources"][0].__setitem__("source_name", "forged"),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                connection, recommendation_id, observed_id, benchmark_id = self._evaluate_fixture()
                snapshot = json.loads(connection.execute(
                    "SELECT snapshot_json FROM recommendations WHERE id = ?", (recommendation_id,)
                ).fetchone()[0])
                mutate(snapshot)
                connection.execute(
                    "UPDATE recommendations SET snapshot_json = ?, snapshot_hash = ? WHERE id = ?",
                    (json.dumps(snapshot, allow_nan=False), canonical_hash(snapshot), recommendation_id),
                )
                connection.commit()
                with self.assertRaises(ValueError):
                    evaluate_recommendation(
                        connection, recommendation_id, evaluation_date="2026-01-02",
                        observed_price_source_id=observed_id, benchmark_price_source_id=benchmark_id,
                    )
                connection.close()

    def test_snapshot_hash_is_required_and_nonfinite_json_is_rejected(self) -> None:
        for missing_hash in (True, False):
            connection, recommendation_id, observed_id, benchmark_id = self._evaluate_fixture()
            if missing_hash:
                connection.execute("UPDATE recommendations SET snapshot_hash = '' WHERE id = ?", (recommendation_id,))
            else:
                connection.execute("UPDATE recommendations SET snapshot_hash = ? WHERE id = ?", ("0" * 64, recommendation_id))
            connection.commit()
            with self.assertRaises(ValueError):
                evaluate_recommendation(
                    connection, recommendation_id, evaluation_date="2026-01-02",
                    observed_price_source_id=observed_id, benchmark_price_source_id=benchmark_id,
                )
            connection.close()

        for constant in ("NaN", "Infinity"):
            connection, recommendation_id, observed_id, benchmark_id = self._evaluate_fixture()
            snapshot = json.loads(connection.execute(
                "SELECT snapshot_json FROM recommendations WHERE id = ?", (recommendation_id,)
            ).fetchone()[0])
            snapshot["portfolio"]["market_value"] = float(constant)
            connection.execute(
                "UPDATE recommendations SET snapshot_json = ? WHERE id = ?",
                (json.dumps(snapshot), recommendation_id),
            )
            connection.commit()
            with self.assertRaises(ValueError):
                evaluate_recommendation(
                    connection, recommendation_id, evaluation_date="2026-01-02",
                    observed_price_source_id=observed_id, benchmark_price_source_id=benchmark_id,
                )
            connection.close()


class NineteenthRepairRendererTests(unittest.TestCase):
    def test_structured_renderer_binding_is_byte_exact(self) -> None:
        connection, _, _, _ = recommendation_fixture()
        self.addCleanup(connection.close)
        snapshot = calculate_snapshot(connection, as_of="2026-01-01")
        candidates = run_screens(connection, snapshot, as_of="2026-01-01")
        report = render_report(snapshot, candidates, provider="local", template_version="v1")
        mutations = (
            ("Report generated", "Ｒeport generated"),
            ("Provider/model identifier:", "Provider／model identifier："),
            ("Report generated", "Report  generated"),
            ("Provider/model identifier:", "Provider-model identifier:"),
            ("Report contract fingerprint:", "Report contract finger\u200bprint:"),
            ("`local`", "`other`"),
        )
        for original, mutation in mutations:
            with self.subTest(mutation=mutation):
                altered = report.replace(original, mutation, 1)
                with self.assertRaises(ValueError):
                    validate_report(
                        altered, snapshot=snapshot, candidates=candidates,
                        provider="local", template_version="v1",
                    )
        validate_report(report, snapshot=snapshot, candidates=candidates, provider="local", template_version="v1")


class NineteenthRepairSafetyTests(unittest.TestCase):
    def test_action_identifier_credential_and_filename_variants_fail_closed(self) -> None:
        forbidden = (
            "Start SELLING now",
            "You should be BUYING immediately",
            "E-X-E-C-U-T-E now",
            "S\u200bE\u200bL\u200bL now",
            "account ABCDEF",
            "Bearer " + "a" * 32,
            "api_key " + "a" * 32,
            "Authorization: Basic " + "a" * 24,
            "ghp_abcdefghijklmnopqrstuvwxyz123456",
            "sk-proj-abcdefghijklmnopqrstuvwxyz123456",
            "-----BEGIN PRIVATE KEY-----",
            "山田-portfolio.csv",
            "alice-ledger.csv",
        )
        for phrase in forbidden:
            with self.subTest(phrase=phrase):
                with self.assertRaises(ValueError):
                    validate_report(minimal_report(phrase), source_records=[SOURCE_ID])

    def test_safe_prose_redaction_and_canonical_renderer_remain_accepted(self) -> None:
        for phrase in (
            "account category, broker research, customer records, portfolio snapshot",
            "token is an explanatory field name; [REDACTED] is not a credential",
            "price-history.csv is a generic filename",
        ):
            with self.subTest(phrase=phrase):
                validate_report(minimal_report(phrase), source_records=[SOURCE_ID])

        connection, _, _, _ = recommendation_fixture()
        self.addCleanup(connection.close)
        snapshot = calculate_snapshot(connection, as_of="2026-01-01")
        candidates = run_screens(connection, snapshot, as_of="2026-01-01")
        report = render_report(snapshot, candidates, provider="local", template_version="v1")
        validate_report(report, snapshot=snapshot, candidates=candidates, provider="local", template_version="v1")

    def test_structured_fields_are_scanned_before_renderer_binding(self) -> None:
        for source_name in (
            "Bearer " + "b" * 32,
            "山田-portfolio.csv",
            "Start SELLING now",
        ):
            with self.subTest(source_name=source_name):
                with self.assertRaises(ValueError):
                    validate_report(
                        minimal_report("stable evidence"),
                        source_records=[{"id": SOURCE_ID, "source_name": source_name}],
                    )


if __name__ == "__main__":
    unittest.main()
