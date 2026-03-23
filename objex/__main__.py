import argparse
import os

from . import explorer


def build_parser():
    parser = argparse.ArgumentParser(
        prog='python -m objex',
        description='Export and explore Python object graphs stored in SQLite databases.',
    )
    subparsers = parser.add_subparsers(dest='command')

    explore_parser = subparsers.add_parser(
        'explore',
        help='Open an analysis database in the interactive explorer.',
    )
    explore_parser.add_argument('analysis_db', help='Path to an objex analysis database.')

    make_analysis_parser = subparsers.add_parser(
        'make-analysis-db',
        help='Create an analysis database from a collected objex dump.',
    )
    make_analysis_parser.add_argument('collection_db', help='Path to the collected objex dump database.')
    make_analysis_parser.add_argument('analysis_db', help='Path to write the analysis database.')

    parser.add_argument(
        'analysis_db',
        nargs='?',
        help=argparse.SUPPRESS,
    )
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == 'make-analysis-db':
        explorer.make_analysis_db(args.collection_db, args.analysis_db)
        return 0

    analysis_db = None
    if args.command == 'explore':
        analysis_db = args.analysis_db
    elif args.analysis_db:
        analysis_db = args.analysis_db
    else:
        parser.print_help()
        return 0

    try:
        explorer.Console(explorer.Reader(analysis_db)).run()
    except Exception:
        if os.getenv('OBJEX_DEBUG', ''):
            import pdb
            import traceback

            traceback.print_exc()
            pdb.post_mortem()
        else:
            raise
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
