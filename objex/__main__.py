import sys

from . import core


core.CLI(core.Reader(sys.argv[-1])).run()
