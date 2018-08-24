import sys

from . import explorer

try:
    explorer.ConsoleV2(explorer.Reader(sys.argv[-1])).run()
except:
    import traceback; traceback.print_exc()
    import pdb; pdb.post_mortem()
