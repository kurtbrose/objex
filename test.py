import os

from objex import dump_graph

if os.path.exists('objex-test.db'):
    os.remove('objex-test.db')
dump_graph('objex-test.db', print_info=True)
