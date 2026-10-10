import argparse
import json
import logging
from dataclasses import asdict
from pathlib import Path

from .analysis import aggregate
from .database import open_database
from .importer import import_csv


def main() -> None:
    parser = argparse.ArgumentParser(description="Import, analyze, and review local transactions")
    parser.add_argument("--database", type=Path, default=Path("data/fyntok.sqlite3"))
    commands = parser.add_subparsers(dest="command", required=True)
    importer = commands.add_parser("import")
    importer.add_argument("csv", type=Path)
    analysis = commands.add_parser("analyze")
    analysis.add_argument("--by", choices=["monthly", "trends", "merchant"], default="monthly")
    serve = commands.add_parser("serve", help="Open the localhost manual application")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument("--reload", action="store_true", help="Reload on Python source changes")
    serve.add_argument(
        "--log-level",
        choices=["debug", "info", "warning", "error", "critical"],
        default="info",
        help="Server logging verbosity (default: info)",
    )
    args = parser.parse_args()
    if args.command == "serve":
        logging.basicConfig(
            level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
        )
        logging.getLogger("fyntok").setLevel(args.log_level.upper())
        logging.getLogger(__name__).info("Opening database %s", args.database.resolve())
    engine = open_database(args.database)
    try:
        if args.command == "serve":
            from .app import serve

            serve(engine, port=args.port, log_level=args.log_level, reload=args.reload)
            return
        result = (
            asdict(import_csv(engine, args.csv))
            if args.command == "import"
            else aggregate(engine, by=args.by)
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        engine.dispose()


if __name__ in {"__main__", "__mp_main__"}:
    main()
