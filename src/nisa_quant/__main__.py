"""Command-line entry point for local fixture workflows."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from .imports import import_csv
from .journal import evaluate_recommendation, record_recommendation
from .metrics import calculate_snapshot
from .reports import render_report
from .schema import connect_database, initialize_database
from .screens import run_screens
from .sources import import_price_fixture


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

    prices = subparsers.add_parser("import-prices", help="import a documented local price fixture")
    prices.add_argument("--db", default="data/nisa_quant.sqlite")
    prices.add_argument("--csv", required=True)

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
    evaluate.add_argument("--observed-price", type=float, required=True)
    evaluate.add_argument("--benchmark-price", type=float, required=True)
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
            result = import_csv(connection, Path(args.csv), source_name="synthetic-broker")
            print(json.dumps(asdict(result), ensure_ascii=False))
        elif args.command == "import-prices":
            result = import_price_fixture(connection, Path(args.csv), source_name="synthetic-prices")
            print(json.dumps({"accepted_rows": result.accepted_rows, "source_ids": result.source_ids}))
        elif args.command == "snapshot":
            print(json.dumps(calculate_snapshot(connection, as_of=args.as_of), ensure_ascii=False, indent=2))
        elif args.command == "screens":
            snapshot = calculate_snapshot(connection, as_of=args.as_of)
            print(json.dumps(run_screens(connection, snapshot, as_of=args.as_of), ensure_ascii=False, indent=2))
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
            recommendation_id = record_recommendation(connection, candidates[args.index], data_cutoff=args.as_of, provider=args.provider, template_version=args.template_version)
            print(recommendation_id)
        elif args.command == "evaluate-recommendation":
            evaluate_recommendation(connection, args.recommendation_id, evaluation_date=args.date, observed_price=args.observed_price, benchmark_price=args.benchmark_price)
            print(f"evaluated {args.recommendation_id}")
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
