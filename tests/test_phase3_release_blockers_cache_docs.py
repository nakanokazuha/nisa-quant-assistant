import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlparse

from nisa_quant.historical_market_data import (
    HttpResponse,
    MarketBar,
    UniverseMember,
    fetch_history_snapshot,
    load_history_snapshot,
)


def _http_get(url: str, **_: object) -> HttpResponse:
    ticker = urlparse(url).path.rsplit("/", 1)[-1]
    payload = {
        "chart": {
            "result": [{
                "meta": {"symbol": ticker.replace("%5E", "^")},
                "timestamp": [1767225600, 1767312000],
                "indicators": {
                    "quote": [{
                        "open": [100, 101], "high": [100, 101],
                        "low": [100, 101], "close": [100, 101],
                        "volume": [1000, 1001],
                    }],
                    "adjclose": [{"adjclose": [100, 101]}],
                },
                "events": {"div": {}, "splits": {}},
            }],
        },
    }
    return HttpResponse(200, json.dumps(payload).encode())


def _fetch(directory: Path):
    return fetch_history_snapshot(
        tickers=["AAA", "BBB"], benchmark_ticker="^GSPC",
        start_date="2026-01-01", end_date="2026-01-02",
        cache_dir=directory, retrieved_at="2026-01-03T00:00:00+00:00",
        http_get=_http_get,
    )


def _cache_path(directory: Path) -> Path:
    paths = list(directory.glob("history-*.json"))
    assert len(paths) == 1
    return paths[0]


class Phase3ReleaseBlockersCacheTests(unittest.TestCase):
    def test_phase3_docs_describe_supported_local_dataset_cli_and_real_metrics(self) -> None:
        docs_root = Path(__file__).parents[1] / "docs"
        text = "\n".join(
            (docs_root / name).read_text(encoding="utf-8")
            for name in ("README.md", "phase3-data.md", "phase3-model.md", "phase3-operations.md")
        )

        self.assertNotIn("phase3-" + "fetch-history", text)
        self.assertIn("phase3-backtest", text)
        self.assertIn("--dataset", text)
        self.assertIn("--output", text)
        self.assertIn("existing local", text)
        self.assertIn("does not fetch", text)
        self.assertNotIn("target_1m_total_return", text)
        self.assertIn("target_1m_return", text)

    def test_market_cache_contract_binds_all_request_dimensions_and_round_trips_types(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = _fetch(root)
            contract = json.loads(snapshot.request_contract)
            loaded = load_history_snapshot(
                _cache_path(root), expected_request_contract=snapshot.request_contract,
            )

        self.assertEqual(contract["schema"], "phase3-market-request")
        self.assertEqual(contract["schema_version"], 1)
        self.assertEqual(contract["asset_tickers"], ["AAA", "BBB"])
        self.assertEqual(contract["benchmark_ticker"], "^GSPC")
        self.assertEqual(contract["start_date"], "2026-01-01")
        self.assertEqual(contract["end_date"], "2026-01-02")
        self.assertEqual(contract["interval"], "1d")
        self.assertEqual(contract["events"], "div,splits")
        self.assertEqual(contract["return_basis"], "price_return")
        self.assertIsInstance(loaded.bars_by_ticker["AAA"][0], MarketBar)
        self.assertIsInstance(loaded.universe[0], UniverseMember)
        self.assertEqual(loaded, snapshot)

    def test_cache_rejects_missing_or_old_contract_fields(self) -> None:
        required_fields = (
            "schema", "schema_version", "asset_tickers", "benchmark_ticker",
            "start_date", "end_date", "period1", "period2", "interval",
            "events", "return_basis", "timeout", "retries", "user_agent",
            "endpoint",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = _fetch(root)
            path = _cache_path(root)
            original = json.loads(path.read_text(encoding="utf-8"))
            for field in required_fields:
                with self.subTest(field=field):
                    payload = dict(original)
                    contract = json.loads(snapshot.request_contract)
                    contract.pop(field, None)
                    payload["request_contract"] = json.dumps(
                        contract, sort_keys=True, separators=(",", ":"),
                    )
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        load_history_snapshot(path)
            payload = dict(original)
            contract = json.loads(snapshot.request_contract)
            contract["schema_version"] = 0
            payload["request_contract"] = json.dumps(
                contract, sort_keys=True, separators=(",", ":"),
            )
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "schema"):
                load_history_snapshot(path)

    def test_cache_rejects_role_swap_and_noncanonical_request_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = _fetch(root)
            path = _cache_path(root)
            original = json.loads(path.read_text(encoding="utf-8"))

            swapped = dict(original)
            contract = json.loads(snapshot.request_contract)
            contract["asset_tickers"] = ["^GSPC", "BBB"]
            contract["benchmark_ticker"] = "AAA"
            swapped["request_contract"] = json.dumps(
                contract, sort_keys=True, separators=(",", ":"),
            )
            path.write_text(json.dumps(swapped), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "role"):
                load_history_snapshot(path)

            unsorted = dict(original)
            contract = json.loads(snapshot.request_contract)
            contract["asset_tickers"] = ["BBB", "AAA"]
            unsorted["request_contract"] = json.dumps(
                contract, sort_keys=True, separators=(",", ":"),
            )
            path.write_text(json.dumps(unsorted), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "canonical"):
                load_history_snapshot(path)

    def test_cache_rejects_tampered_market_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = _fetch(root)
            path = _cache_path(root)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["bars_by_ticker"]["AAA"][0]["close"] = 999.0
            path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "snapshot identity"):
                load_history_snapshot(path, expected_request_contract=snapshot.request_contract)


if __name__ == "__main__":
    unittest.main()
