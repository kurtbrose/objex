import os

from objex import dump_graph, make_analysis_db

if os.path.exists('objex-test.db'):
    os.remove('objex-test.db')
dump_graph('objex-test.db', print_info=True)
make_analysis_db('objex-test.db', 'objex-test-analysis.db')

