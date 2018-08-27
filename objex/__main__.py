
import os
import sys

from . import explorer

if len(sys.argv) >= 3 and sys.argv[-3] == 'make-analysis-db':
    explorer.make_analysis_db(sys.argv[-2], sys.argv[-1])

try:
    explorer.ConsoleV2(explorer.Reader(sys.argv[-1])).run()
except Exception:
    if os.getenv('OBJEX_DEBUG', ''):
        import traceback; traceback.print_exc()
        import pdb; pdb.post_mortem()
