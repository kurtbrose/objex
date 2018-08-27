import sys

from . import explorer


# TODO: non lazy command line args

if len(sys.argv) >= 3 and sys.argv[-3] == 'make-analysis-db':
    explorer.make_analysis_db(sys.argv[-2], sys.argv[-1])
else:
    explorer.Console(explorer.Reader(sys.argv[-1])).run()
