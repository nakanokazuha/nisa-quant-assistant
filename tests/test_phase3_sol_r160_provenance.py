from __future__ import annotations

import json
import unittest

from nisa_quant.evidence_providers import HttpResponse, SECEdgarProvider


class SolR160SecProvenanceTests(unittest.TestCase):
    def test_fetch_company_facts_uses_exact_zero_padded_cik_url_for_provenance(self) -> None:
        expected_url = "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json"
        calls: list[str] = []

        class Transport:
            def get(self, url: str, *, headers: dict[str, str], timeout: float) -> HttpResponse:
                calls.append(url)
                return HttpResponse(200, json.dumps({
                    "cik": "0000320193",
                    "facts": {"us-gaap": {"Revenue": {"units": {"USD": [{
                        "val": 10,
                        "end": "2025-12-31",
                        "filed": "2026-01-15",
                        "form": "10-K",
                        "accn": "0000320193-26-000001",
                    }]}}}},
                }).encode())

        records = SECEdgarProvider(
            Transport(), user_agent="phase2-test [REDACTED]",
        ).fetch_company_facts(
            "320193", ticker="AAPL", retrieved_at="2026-09-16T00:00:00+00:00",
        )

        self.assertEqual(calls, [expected_url])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].source_url, expected_url)
        self.assertEqual(records[0].citation, expected_url)
        self.assertNotEqual(records[0].source_url, "https://data.sec.gov/api/xbrl/companyfacts/")
        self.assertNotEqual(records[0].citation, "https://data.sec.gov/api/xbrl/companyfacts/")


if __name__ == "__main__":
    unittest.main()
