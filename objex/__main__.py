import sys

from . import core

try:
    core.CLI(core.Reader(sys.argv[-1])).run()
except:
    import traceback; traceback.print_exc()
    import pdb; pdb.post_mortem()
