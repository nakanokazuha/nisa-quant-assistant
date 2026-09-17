from __future__ import annotations

import json
import importlib.util
import os
import select
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


_SERVER_PATH = Path(__file__).resolve().parents[1] / "tools" / "nisa_quant_mcp_server.py"
_SERVER_SPEC = importlib.util.spec_from_file_location("nisa_quant_mcp_server", _SERVER_PATH)
assert _SERVER_SPEC is not None and _SERVER_SPEC.loader is not None
server = importlib.util.module_from_spec(_SERVER_SPEC)
_SERVER_SPEC.loader.exec_module(server)


class NisaQuantMcpCallableTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.cache_dir = self.root / "cache"
        self.report_dir = self.root / "reports"

    def _patch_paths(self):
        return patch.multiple(
            server,
            PHASE3_CACHE_DIR=self.cache_dir,
            REPORT_DIR=self.report_dir,
            LATEST_REPORT_PATHS=(self.report_dir / "hermes-report.json", self.report_dir / "hermes-report.markdown"),
        )

    def _write_result_files(self, output: Path, status: str = "available") -> None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(
                {
                    "status": status,
                    "current_predictions": [
                        {"ticker": "AAA", "freshness_evidence": {"status": "fresh"}},
                        {"ticker": "BBB", "freshness_evidence": {"status": "fresh"}},
                    ],
                    "performance_claims_suppressed": True,
                }
            ),
            encoding="utf-8",
        )
        output.with_suffix(output.suffix + ".manifest.json").write_text(
            json.dumps(
                {
                    "report_status": status,
                    "sec_status": "not_requested",
                    "current_prediction_freshness": [
                        {"ticker": "AAA", "status": "fresh"},
                        {"ticker": "BBB", "status": "fresh"},
                    ],
                    "gaps": {"AAA": {"status": "covered"}},
                    "failures": [],
                    "failures_by_ticker": {},
                }
            ),
            encoding="utf-8",
        )

    def test_callable_surface_is_closed_and_defaults_to_replay(self) -> None:
        self.assertEqual(server.TOOL_NAMES, ("nisa_quant_refresh", "nisa_quant_latest"))
        self.assertEqual(
            set(__import__("inspect").signature(server.nisa_quant_refresh).parameters),
            {"as_of", "start", "end", "mode", "limit", "sec_contact", "format"},
        )

        calls: list[dict[str, object]] = []

        def fake_refresh(**kwargs: object) -> int:
            calls.append(kwargs)
            self.assertFalse(kwargs["live"])
            self.assertTrue(kwargs["replay_only"])
            self.assertIsNone(kwargs["limit"])
            self._write_result_files(Path(str(kwargs["output"])))
            return 0

        with self._patch_paths(), patch.object(server, "refresh_phase3", side_effect=fake_refresh):
            result = server.nisa_quant_refresh(
                as_of="2026-09-16", start="2024-01-01", end="2026-09-16"
            )

        self.assertEqual(result["status"], "available")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(calls[0]["output"], self.report_dir / "hermes-report.markdown")

    def test_live_is_explicit_and_limit_is_forwarded(self) -> None:
        calls: list[dict[str, object]] = []

        def fake_refresh(**kwargs: object) -> int:
            calls.append(kwargs)
            self.assertTrue(kwargs["live"])
            self.assertFalse(kwargs["replay_only"])
            self._write_result_files(Path(str(kwargs["output"])))
            return 0

        with self._patch_paths(), patch.object(server, "refresh_phase3", side_effect=fake_refresh):
            result = server.nisa_quant_refresh(
                as_of="2026-09-16",
                start="2024-01-01",
                end="2026-09-16",
                mode="live",
                limit=3,
                sec_contact="researcher@example.com",
                format="json",
            )

        self.assertEqual(result["status"], "available")
        self.assertEqual(calls[0]["limit"], 3)
        self.assertEqual(calls[0]["sec_contact"], "researcher@example.com")
        self.assertEqual(calls[0]["output"], self.report_dir / "hermes-report.json")

    def test_default_replay_executes_real_producer_without_network(self) -> None:
        with self._patch_paths(), patch("nisa_quant.evidence_providers.UrllibReadOnlyTransport.get", side_effect=AssertionError("replay attempted network")) as network_get:
            result = server.nisa_quant_refresh(
                as_of="2026-09-16", start="2024-01-01", end="2026-09-16", format="json"
            )

        self.assertTrue(result["completed"])
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual(result["status"], "unavailable")
        network_get.assert_not_called()

    def test_invalid_arguments_are_structured_and_producer_is_not_called(self) -> None:
        with patch.object(server, "refresh_phase3") as producer:
            result = server.nisa_quant_refresh(
                as_of="2026/09/16", start="2024-01-01", end="2026-09-16", mode="live", limit=0
            )

        self.assertEqual(result["status"], "validation_error")
        self.assertFalse(result["completed"])
        self.assertTrue(result["validation_errors"])
        producer.assert_not_called()

        with patch.object(server, "refresh_phase3") as producer:
            result = server.nisa_quant_refresh(
                as_of="2026-09-16",
                start="2024-01-01",
                end="2026-09-16",
                mode="replay",
                sec_contact="secret-token-value",
            )
        self.assertEqual(result["status"], "validation_error")
        producer.assert_not_called()

    def test_exit_two_is_completed_structured_unavailable_result(self) -> None:
        def fake_refresh(**kwargs: object) -> int:
            output = Path(str(kwargs["output"]))
            self._write_result_files(output, status="unavailable_insufficient_data")
            return 2

        with self._patch_paths(), patch.object(server, "refresh_phase3", side_effect=fake_refresh):
            result = server.nisa_quant_refresh(
                as_of="2026-09-16", start="2024-01-01", end="2026-09-16", format="json"
            )

        self.assertTrue(result["completed"])
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual(result["status"], "unavailable_insufficient_data")
        self.assertEqual(result["current_prediction_count"], 2)
        self.assertEqual(result["current_prediction_tickers"], ["AAA", "BBB"])
        self.assertTrue(result["performance_claims_suppressed"])
        self.assertIn("unavailable_insufficient_data", result["summary"])

    def test_latest_not_found_is_structured(self) -> None:
        with self._patch_paths():
            result = server.nisa_quant_latest()

        self.assertEqual(result["status"], "not_yet_run")
        self.assertTrue(result["completed"])
        self.assertIsNone(result["report_path"])

    def test_latest_uses_newest_fixed_report_manifest_pair(self) -> None:
        json_path = self.report_dir / "hermes-report.json"
        markdown_path = self.report_dir / "hermes-report.markdown"
        self._write_result_files(json_path)
        self._write_result_files(markdown_path)
        for path, retrieved_at in (
            (json_path, "2026-09-15T00:00:00+00:00"),
            (markdown_path, "2026-09-16T00:00:00+00:00"),
        ):
            manifest_path = path.with_suffix(path.suffix + ".manifest.json")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["retrieved_at"] = retrieved_at
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with self._patch_paths():
            result = server.nisa_quant_latest()

        self.assertEqual(result["report_path"], "reports/phase3/hermes-report.markdown")
        self.assertEqual(
            result["manifest_path"],
            "reports/phase3/hermes-report.markdown.manifest.json",
        )

    def test_relative_path_hides_repo_local_temp_directories(self) -> None:
        report_path = server.REPO_ROOT / ".mcp-test" / "reports" / "phase3" / "hermes-report.json"
        manifest_path = report_path.with_suffix(report_path.suffix + ".manifest.json")

        self.assertEqual(server._relative_path(report_path), "reports/phase3/hermes-report.json")
        self.assertEqual(
            server._relative_path(manifest_path),
            "reports/phase3/hermes-report.json.manifest.json",
        )

    def test_summary_extracts_json_report_and_manifest(self) -> None:
        report = {
            "status": "unavailable_insufficient_data",
            "current_predictions": [{"ticker": "MSFT", "freshness_evidence": {"status": "fresh"}}],
            "performance_claims_suppressed": True,
        }
        manifest = {
            "report_status": "unavailable_insufficient_data",
            "sec_status": "bound_no_usable_facts",
            "current_prediction_freshness": [{"ticker": "MSFT", "status": "fresh"}],
            "gaps": {"MSFT": {"status": "gapped", "gap_count": 1}},
            "failures": ["MSFT: missing bar"],
            "failures_by_ticker": {"MSFT": "missing bar"},
        }

        result = server._summary_from_artifacts(
            report=report,
            manifest=manifest,
            exit_code=2,
            report_path=self.report_dir / "hermes-report.json",
            manifest_path=self.report_dir / "hermes-report.json.manifest.json",
        )

        self.assertEqual(result["current_prediction_tickers"], ["MSFT"])
        self.assertEqual(result["sec_status"], "bound_no_usable_facts")
        self.assertEqual(result["freshness_summary"]["fresh"], 1)
        self.assertEqual(result["gaps"]["MSFT"]["status"], "gapped")
        self.assertEqual(result["failures_by_ticker"]["MSFT"], "missing bar")

    def test_refresh_sanitizes_nested_manifest_paths_and_values(self) -> None:
        def fake_refresh(**kwargs: object) -> int:
            output = Path(str(kwargs["output"]))
            self._write_result_files(output, status="unavailable_insufficient_data")
            manifest_path = output.with_suffix(output.suffix + ".manifest.json")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["failures"] = [
                {
                    "message": "failed to read /Users/private/replay.json",
                    "details": {"value": "x" * (server.MAX_OUTPUT_STRING * 3)},
                }
            ]
            manifest["gaps"] = {
                "AAA": {"evidence": [{"path": "/private/replay.json"}]}
            }
            manifest["failures_by_ticker"] = {
                "AAA": {"error": "missing /var/tmp/replay.json"}
            }
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            return 2

        with self._patch_paths(), patch.object(server, "refresh_phase3", side_effect=fake_refresh):
            result = server.nisa_quant_refresh(
                as_of="2026-09-16", start="2024-01-01", end="2026-09-16", format="json"
            )

        failure = result["failures"][0]
        self.assertEqual(failure["message"], "failed to read [LOCAL_PATH_REDACTED]")
        self.assertLessEqual(len(failure["details"]["value"]), server.MAX_OUTPUT_STRING)
        self.assertEqual(result["gaps"]["AAA"]["evidence"][0]["path"], "[LOCAL_PATH_REDACTED]")
        self.assertEqual(
            result["failures_by_ticker"]["AAA"]["error"],
            "missing [LOCAL_PATH_REDACTED]",
        )
        self.assertNotIn("/Users/private/replay.json", json.dumps(result))
        self.assertNotIn("/private/replay.json", json.dumps(result))
        self.assertNotIn("/var/tmp/replay.json", json.dumps(result))

    def test_latest_sanitizes_report_content_without_mutating_report_files(self) -> None:
        report_path = self.report_dir / "hermes-report.json"
        self._write_result_files(report_path)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["nested"] = {
            "message": "report source /tmp/replay.json",
            "value": "y" * (server.MAX_OUTPUT_STRING * 3),
        }
        report_path.write_text(json.dumps(report), encoding="utf-8")
        manifest_path = report_path.with_suffix(report_path.suffix + ".manifest.json")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["gaps"] = {"AAA": {"path": "/var/folders/replay.json"}}
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        report_before = report_path.read_bytes()
        manifest_before = manifest_path.read_bytes()

        with self._patch_paths():
            result = server.nisa_quant_latest()

        self.assertIn("report source [LOCAL_PATH_REDACTED]", result["report_content"])
        self.assertLessEqual(len(result["report_content"]), server.MAX_REPORT_CONTENT)
        self.assertEqual(result["gaps"]["AAA"]["path"], "[LOCAL_PATH_REDACTED]")
        self.assertNotIn("/tmp/replay.json", result["report_content"])
        self.assertNotIn("/var/folders/replay.json", json.dumps(result))
        self.assertEqual(report_path.read_bytes(), report_before)
        self.assertEqual(manifest_path.read_bytes(), manifest_before)

    def test_refresh_redacts_generic_paths_and_json_secret_assignments(self) -> None:
        sensitive_values = [
            "failed to read /etc/passwd",
            "failed to read /opt/local/private.json",
            "failed to read /foo/My Documents/replay.json",
            "failed to read file:///etc/passwd",
            '{"api_key":"top-secret-value"}',
            'token: ["TOPSECRET_ARRAY_VALUE"]',
            "private_key: |-\n  TOPSECRET_BLOCK_VALUE",
            "public https://example.com/opt/local remains a URL",
            "public https://example.com?path=/etc/passwd remains a URL",
        ]

        def fake_refresh(**kwargs: object) -> int:
            output = Path(str(kwargs["output"]))
            self._write_result_files(output, status="unavailable_insufficient_data")
            manifest_path = output.with_suffix(output.suffix + ".manifest.json")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["failures"] = sensitive_values
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            return 2

        with self._patch_paths(), patch.object(server, "refresh_phase3", side_effect=fake_refresh):
            result = server.nisa_quant_refresh(
                as_of="2026-09-16", start="2024-01-01", end="2026-09-16", format="json"
            )

        failures = result["failures"]
        self.assertEqual(failures[0], "failed to read [LOCAL_PATH_REDACTED]")
        self.assertEqual(failures[1], "failed to read [LOCAL_PATH_REDACTED]")
        self.assertEqual(failures[2], "failed to read [LOCAL_PATH_REDACTED]")
        self.assertEqual(failures[3], "failed to read [LOCAL_PATH_REDACTED]")
        self.assertEqual(failures[4], '{"api_key":"[SECRET_REDACTED]"}')
        self.assertEqual(failures[5], "token: [SECRET_REDACTED]")
        self.assertEqual(failures[6], "private_key: [SECRET_REDACTED]")
        self.assertEqual(failures[7], "public https://example.com/opt/local remains a URL")
        self.assertEqual(failures[8], "public https://example.com?path=/etc/passwd remains a URL")
        self.assertNotIn("failed to read /etc/passwd", json.dumps(result))
        self.assertNotIn("failed to read /opt/local/private.json", json.dumps(result))
        self.assertNotIn("My Documents/replay.json", json.dumps(result))
        self.assertNotIn("top-secret-value", json.dumps(result))
        self.assertNotIn("TOPSECRET_ARRAY_VALUE", json.dumps(result))
        self.assertNotIn("TOPSECRET_BLOCK_VALUE", json.dumps(result))

    def test_latest_redacts_generic_paths_and_json_secret_assignments(self) -> None:
        report_path = self.report_dir / "hermes-report.json"
        self._write_result_files(report_path)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["sensitive_evidence"] = (
            "sources: /etc/passwd and /opt/local/private.json; "
            "/Users/private/My Documents/replay.json and file:///etc/passwd; "
            '{"api_key":"top-secret-value"}; '
            "normal report prose remains readable; "
            "public https://example.com/opt/local and "
            "https://example.com?path=/etc/passwd remain URLs"
        )
        report_path.write_text(json.dumps(report), encoding="utf-8")
        manifest_path = report_path.with_suffix(report_path.suffix + ".manifest.json")
        report_before = report_path.read_bytes()
        manifest_before = manifest_path.read_bytes()

        with self._patch_paths():
            result = server.nisa_quant_latest()

        content = result["report_content"]
        self.assertIn(
            "sources: [LOCAL_PATH_REDACTED] and [LOCAL_PATH_REDACTED]; "
            "[LOCAL_PATH_REDACTED] and [LOCAL_PATH_REDACTED]",
            content,
        )
        self.assertIn('{"api_key":"[SECRET_REDACTED]"}', content)
        self.assertIn("normal report prose remains readable", content)
        self.assertIn(
            "public https://example.com/opt/local and "
            "https://example.com?path=/etc/passwd remain URLs",
            content,
        )
        self.assertNotIn("sources: /etc/passwd", content)
        self.assertNotIn("and /opt/local/private.json", content)
        self.assertNotIn("My Documents/replay.json", content)
        self.assertNotIn("top-secret-value", content)
        self.assertEqual(report_path.read_bytes(), report_before)
        self.assertEqual(manifest_path.read_bytes(), manifest_before)

    def test_latest_sanitizes_secret_values_that_cross_content_limit(self) -> None:
        report_path = self.report_dir / "hermes-report.json"
        self._write_result_files(report_path)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        secret_suffix = ' token="TOPSECRET_VALUE_THAT_CROSSES_THE_RAW_LIMIT'
        report["sensitive_evidence"] = secret_suffix
        token_offset = json.dumps(report).index("token=")
        padding_length = server.MAX_REPORT_CONTENT - token_offset - 10
        report["sensitive_evidence"] = "x" * padding_length + secret_suffix
        report_path.write_text(json.dumps(report), encoding="utf-8")

        with self._patch_paths():
            result = server.nisa_quant_latest()

        self.assertNotIn("TOPSECRET", result["report_content"])
        self.assertLessEqual(len(result["report_content"]), server.MAX_REPORT_CONTENT)

    def test_secret_assignment_keys_redact_without_changing_bearer_prose(self) -> None:
        cases = {
            '"authorization": "Bearer top-secret-value"': '"authorization": "[SECRET_REDACTED]"',
            "bearer: top-secret-value": "bearer: [SECRET_REDACTED]",
            "private_key=top-secret-value": "private_key=[SECRET_REDACTED]",
            "password: top-secret-value": "password: [SECRET_REDACTED]",
            "credential=top-secret-value": "credential=[SECRET_REDACTED]",
            "AWS_SECRET_ACCESS_KEY=hunter2": "AWS_SECRET_ACCESS_KEY=[SECRET_REDACTED]",
            "TOKEN=TOPSECRET,SECONDSECRET": "TOKEN=[SECRET_REDACTED]",
            "TOKEN=TOPSECRET; echo done": "TOKEN=[SECRET_REDACTED]; echo done",
            '{"token": ["TOP]SECRET"]}': '{"token": [SECRET_REDACTED]}',
            '{"token": "TOP\\"SECRET"}': '{"token": "[SECRET_REDACTED]"}',
            "token: TOP SECRET": "token: [SECRET_REDACTED]",
            "private_key: |-\n  TOP\n  SECRET": "private_key: [SECRET_REDACTED]",
            "/foo/My Documents/replay.json": "[LOCAL_PATH_REDACTED]",
            "file:///Users/alice/My Documents/private.json": "[LOCAL_PATH_REDACTED]",
            "file://localhost/etc/passwd": "[LOCAL_PATH_REDACTED]",
            "FILE:///etc/passwd": "[LOCAL_PATH_REDACTED]",
            "bearer of good news": "bearer of good news",
            "https://example.com?path=/etc/passwd": "https://example.com?path=/etc/passwd",
        }

        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(
                    server._sanitize_output_text(source, limit=server.MAX_OUTPUT_STRING),
                    expected,
                )

    def test_fixed_paths_are_repo_local_and_arguments_have_no_path_escape_surface(self) -> None:
        self.assertTrue(server.PHASE3_CACHE_DIR.is_relative_to(server.REPO_ROOT))
        self.assertTrue(server.REPORT_DIR.is_relative_to(server.REPO_ROOT))
        self.assertNotIn("path", {name.casefold() for name in __import__("inspect").signature(server.nisa_quant_refresh).parameters})


class NisaQuantMcpProtocolSmokeTests(unittest.TestCase):
    def test_hermes_venv_stdio_discovery_and_call(self) -> None:
        python = Path("/Users/user/.hermes/hermes-agent/venv/bin/python")
        if not python.exists():
            self.skipTest("Hermes venv Python is not installed")

        process = subprocess.Popen(
            [str(python), "tools/nisa_quant_mcp_server.py"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )

        def stop_process() -> None:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            if process.stdin is not None:
                process.stdin.close()
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()

        self.addCleanup(stop_process)

        def request(message: dict[str, object]) -> dict[str, object]:
            assert process.stdin is not None
            assert process.stdout is not None
            process.stdin.write(json.dumps(message) + "\n")
            process.stdin.flush()
            ready, _, _ = select.select([process.stdout], [], [], 5)
            self.assertTrue(ready, "timed out waiting for MCP response")
            line = process.stdout.readline()
            if not line and process.poll() is not None and process.stderr is not None:
                self.fail(f"server exited: {process.stderr.read()}")
            self.assertTrue(line, "server returned an empty MCP response")
            return json.loads(line)

        initialized = request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "nisa-test", "version": "1"},
                },
            }
        )
        self.assertIn("result", initialized)
        assert process.stdin is not None
        process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        process.stdin.flush()
        listed = request({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        names = {item["name"] for item in listed["result"]["tools"]}
        self.assertEqual(names, {"nisa_quant_refresh", "nisa_quant_latest"})
        refresh_schema = next(item["inputSchema"] for item in listed["result"]["tools"] if item["name"] == "nisa_quant_refresh")
        self.assertEqual(refresh_schema["properties"]["mode"]["enum"], ["live", "replay"])
        self.assertEqual(refresh_schema["properties"]["format"]["enum"], ["markdown", "json"])
        self.assertEqual(refresh_schema["properties"]["limit"]["anyOf"][0]["minimum"], 1)
        self.assertEqual(refresh_schema["properties"]["limit"]["anyOf"][0]["maximum"], server.MAX_LIVE_LIMIT)
        called = request(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "nisa_quant_refresh",
                    "arguments": {"as_of": "2026-02-30", "start": "2024-01-01", "end": "2026-09-16"},
                },
            }
        )
        self.assertIn("result", called)
        self.assertFalse(called["result"].get("isError", False))
        self.assertIn("validation_error", called["result"]["structuredContent"]["status"])


if __name__ == "__main__":
    unittest.main()
