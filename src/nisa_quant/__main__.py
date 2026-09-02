"""Command-line entry point for local fixture workflows."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from .imports import import_csv
from .journal import evaluate_recommendation, record_recommendation
from .metrics import calculate_snapshot
from .phase2 import phase2_evidence_report, refresh_phase2_configured, refresh_phase2_fixtures
from .reports import render_report
from .schema import connect_database, initialize_database
from .screens import run_screens
from .sources import import_distribution_fixture, import_price_fixture
from .watchlist import add_watchlist_item


def _database(path: str):
    database_path = Path(path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = connect_database(database_path)
    initialize_database(connection)
    return connection


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local-first NISA quant research assistant")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init-db", help="initialize a local SQLite ledger")
    init.add_argument("--db", default="data/nisa_quant.sqlite")

    importer = subparsers.add_parser("import-csv", help="import the documented synthetic broker CSV")
    importer.add_argument("--db", default="data/nisa_quant.sqlite")
    importer.add_argument("--csv", required=True)
    importer.add_argument(
        "--retrieved-at",
        help="explicit UTC retrieval timestamp for deterministic fixture imports",
    )

    prices = subparsers.add_parser("import-prices", help="import a documented local price fixture")
    prices.add_argument("--db", default="data/nisa_quant.sqlite")
    prices.add_argument("--csv", required=True)

    distributions = subparsers.add_parser("import-distributions", help="import a documented local ETF distribution fixture")
    distributions.add_argument("--db", default="data/nisa_quant.sqlite")
    distributions.add_argument("--csv", required=True)

    watchlist = subparsers.add_parser("watchlist-add", help="append a typed local watchlist version")
    watchlist.add_argument("--db", default="data/nisa_quant.sqlite")
    watchlist.add_argument("--identifier-value", required=True)
    watchlist.add_argument("--identifier-type", required=True)
    watchlist.add_argument("--display-name", required=True)
    watchlist.add_argument("--asset-type", required=True)
    watchlist.add_argument("--market", required=True)
    watchlist.add_argument("--currency", required=True)
    watchlist.add_argument("--benchmark")
    watchlist.add_argument("--benchmark-identifier-type")
    watchlist.add_argument("--benchmark-identifier-value")
    watchlist.add_argument("--notes", default="")
    watchlist.add_argument("--effective-date")
    watchlist.add_argument("--observed-at")


    snapshot = subparsers.add_parser("snapshot", help="calculate a deterministic JSON snapshot")
    snapshot.add_argument("--db", default="data/nisa_quant.sqlite")
    snapshot.add_argument("--as-of", required=True)

    screens = subparsers.add_parser("screens", help="run deterministic candidate screens")
    screens.add_argument("--db", default="data/nisa_quant.sqlite")
    screens.add_argument("--as-of", required=True)

    report = subparsers.add_parser("report", help="render a cited Markdown report")
    report.add_argument("--db", default="data/nisa_quant.sqlite")
    report.add_argument("--as-of", required=True)
    report.add_argument("--output", required=True)
    report.add_argument("--provider", default="local-deterministic")
    report.add_argument("--template-version", default="report-v1")

    record = subparsers.add_parser("record-recommendation", help="record one ranked candidate")
    record.add_argument("--db", default="data/nisa_quant.sqlite")
    record.add_argument("--as-of", required=True)
    record.add_argument("--index", type=int, default=0)
    record.add_argument("--provider", default="local-deterministic")
    record.add_argument("--template-version", default="report-v1")

    evaluate = subparsers.add_parser("evaluate-recommendation", help="append a later outcome snapshot")
    evaluate.add_argument("--db", default="data/nisa_quant.sqlite")
    evaluate.add_argument("--recommendation-id", type=int, required=True)
    evaluate.add_argument("--date", required=True)
    evaluate.add_argument("--observed-price", type=float)
    evaluate.add_argument("--benchmark-price", type=float)
    evaluate.add_argument("--observed-source-id", required=True)
    evaluate.add_argument("--benchmark-source-id", required=True)

    phase2_refresh = subparsers.add_parser(
        "phase2-refresh-fixtures",
        help="refresh the read-only Phase 2 evidence layer from local fixtures",
    )
    phase2_refresh.add_argument("--db", default="data/nisa_quant.sqlite")
    phase2_refresh.add_argument("--universe-csv", required=True)
    phase2_refresh.add_argument("--market-csv", required=True)
    phase2_refresh.add_argument("--as-of", required=True)
    phase2_refresh.add_argument("--request-id", required=True)
    phase2_refresh.add_argument("--sec-json")
    phase2_refresh.add_argument("--sec-ticker")
    phase2_refresh.add_argument("--sec-cik")
    phase2_refresh.add_argument("--news-xml")

    phase2_configured = subparsers.add_parser(
        "phase2-refresh-config", help="refresh configured read-only Phase 2 providers",
    )
    phase2_configured.add_argument("--db", default="data/nisa_quant.sqlite")
    phase2_configured.add_argument("--config", required=True)
    phase2_configured.add_argument("--universe-csv", required=True)
    phase2_configured.add_argument("--as-of", required=True)
    phase2_configured.add_argument("--request-id", required=True)

    phase2_evidence = subparsers.add_parser(
        "phase2-evidence", help="inspect a read-only Phase 2 evidence snapshot",
    )
    phase2_evidence.add_argument("--db", default="data/nisa_quant.sqlite")
    phase2_evidence.add_argument("--as-of", required=True)
    phase2_evidence.add_argument("--output")
    phase2_evidence.add_argument("--scope-id")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "init-db":
        connection = _database(args.db)
        connection.close()
        print(f"initialized {args.db}")
        return 0
    connection = _database(args.db)
    try:
        if args.command == "import-csv":
            result = import_csv(
                connection, Path(args.csv), source_name="synthetic-broker",
                retrieved_at=args.retrieved_at,
            )
            print(json.dumps(asdict(result), ensure_ascii=False, allow_nan=False))
        elif args.command == "import-prices":
            result = import_price_fixture(connection, Path(args.csv), source_name="synthetic-prices")
            print(json.dumps({"accepted_rows": result.accepted_rows, "source_ids": result.source_ids}, allow_nan=False))
        elif args.command == "import-distributions":
            result = import_distribution_fixture(connection, Path(args.csv), source_name="synthetic-distributions")
            print(json.dumps({"accepted_rows": result.accepted_rows, "source_ids": result.source_ids}, allow_nan=False))
        elif args.command == "watchlist-add":
            if (args.benchmark_identifier_type is None) != (args.benchmark_identifier_value is None):
                raise ValueError("benchmark identifier type and value must be supplied together")
            version_id = add_watchlist_item(
                connection,
                identifier_value=args.identifier_value,
                identifier_type=args.identifier_type,
                display_name=args.display_name,
                asset_type=args.asset_type,
                market=args.market,
                currency=args.currency,
                benchmark=args.benchmark,
                notes=args.notes,
                effective_date=args.effective_date,
                observed_at=args.observed_at,
                benchmark_identifier_type=args.benchmark_identifier_type,
                benchmark_identifier_value=args.benchmark_identifier_value,
            )
            print(f"watchlist version {version_id}")
        elif args.command == "snapshot":
            print(json.dumps(calculate_snapshot(connection, as_of=args.as_of), ensure_ascii=False, indent=2, allow_nan=False))
        elif args.command == "screens":
            snapshot = calculate_snapshot(connection, as_of=args.as_of)
            print(json.dumps(run_screens(connection, snapshot, as_of=args.as_of), ensure_ascii=False, indent=2, allow_nan=False))
        elif args.command == "report":
            snapshot = calculate_snapshot(connection, as_of=args.as_of)
            candidates = run_screens(connection, snapshot, as_of=args.as_of)
            output = Path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(render_report(snapshot, candidates, provider=args.provider, template_version=args.template_version), encoding="utf-8")
            print(f"wrote {output}")
        elif args.command == "record-recommendation":
            snapshot = calculate_snapshot(connection, as_of=args.as_of)
            candidates = run_screens(connection, snapshot, as_of=args.as_of)
            if not candidates:
                raise ValueError("no candidates are available to record")
            if not 0 <= args.index < len(candidates):
                raise ValueError(f"candidate index {args.index} is out of range")
            recommendation_id = record_recommendation(connection, candidates[args.index], data_cutoff=args.as_of, provider=args.provider, template_version=args.template_version, snapshot=snapshot)
            print(recommendation_id)
        elif args.command == "evaluate-recommendation":
            evaluate_recommendation(
                connection, args.recommendation_id, evaluation_date=args.date,
                observed_price=args.observed_price, benchmark_price=args.benchmark_price,
                observed_price_source_id=args.observed_source_id,
                benchmark_price_source_id=args.benchmark_source_id,
            )
            print(f"evaluated {args.recommendation_id}")
        elif args.command == "phase2-refresh-fixtures":
            result = refresh_phase2_fixtures(
                connection,
                universe_path=Path(args.universe_csv),
                market_path=Path(args.market_csv),
                as_of=args.as_of,
                request_id=args.request_id,
                sec_path=Path(args.sec_json) if args.sec_json else None,
                sec_ticker=args.sec_ticker,
                sec_cik=args.sec_cik,
                news_path=Path(args.news_xml) if args.news_xml else None,
            )
            print(json.dumps(asdict(result), ensure_ascii=False, allow_nan=False))
            if result.status == "failed":
                return 2
        elif args.command == "phase2-refresh-config":
            result = refresh_phase2_configured(
                connection, config_path=Path(args.config), universe_path=Path(args.universe_csv),
                as_of=args.as_of, request_id=args.request_id,
            )
            print(json.dumps(asdict(result), ensure_ascii=False, allow_nan=False))
            if result.status == "failed":
                return 2
        elif args.command == "phase2-evidence":
            report = phase2_evidence_report(connection, as_of=args.as_of, scope_id=args.scope_id)
            serialized = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
            if args.output:
                output = Path(args.output)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(serialized + "\n", encoding="utf-8")
                print(f"wrote {output}")
            else:
                print(serialized)
    except (OSError, TypeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
