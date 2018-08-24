import os

from objex import dump_graph, make_analysis_db


# set up some corner-casey junk to stress test the dumper
class A: pass
a = A()
class B(object): pass
b = B()

def closing(a, b=1):
    c = 2
    def has_closure(d):
        return b + c + d
    return has_closure

f = closing(1)


import threading
import thread
import time


def stacker(n=200):
    if n == 0:
        time.sleep(100)
    else:
        return stacker(n - 1)


t = threading.Thread(target=stacker)
t.daemon = True
t.start()


thread.start_new_thread(stacker, ())


class NoneModule(object): pass  # sometimes __module__ = None

NoneModule.__module__ = None

def none_module(): pass

none_module.__module__ = None

def garbage_module(): pass

garbage_module.__module__ = 3.141

# now that a bunch of balls are in the air, dump them to disk
if os.path.exists('objex-test.db'):
    os.remove('objex-test.db')
dump_graph('objex-test.db', print_info=True)
if os.path.exists('objex-test-analysis.db'):
    os.remove('objex-test-analysis.db')
make_analysis_db('objex-test.db', 'objex-test-analysis.db')

