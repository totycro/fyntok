import argparse
from dataclasses import asdict
import json
from pathlib import Path

from .analysis import aggregate
from .database import open_database
from .importer import import_csv


def main() -> None:
    parser = argparse.ArgumentParser(description="Import and analyze local transactions")
    parser.add_argument('--database', type=Path, default=Path('data/fyntok.sqlite3'))
    commands = parser.add_subparsers(dest='command', required=True)
    importer = commands.add_parser('import')
    importer.add_argument('csv', type=Path)
    analysis = commands.add_parser('analyze')
    analysis.add_argument('--by', choices=['monthly', 'trends', 'merchant'], default='monthly')
    args = parser.parse_args()
    engine = open_database(args.database)
    try:
        result = asdict(import_csv(engine, args.csv)) if args.command == 'import' else aggregate(engine, by=args.by)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        engine.dispose()


if __name__ == '__main__':
    main()
