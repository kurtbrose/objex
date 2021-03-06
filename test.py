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
import collections
import weakref


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


# do some tricky __slots__ + inheritance stuff

class A(object):
  __slots__ = ('a',)
class B(A):
  __slots__ = ('b',)
class C(B):
  pass
c = C()
c.c = 'lmao'
class D(dict): pass
d = D()
class L(list): pass
l = L()
class S(set): pass
s = S()
def gen(): yield 1; yield 2
g = gen()
class E(object):
    @staticmethod
    def s(): pass
    @classmethod
    def c(cls): pass
e = E()

dq = collections.deque([1, 2, 3])
dd = collections.defaultdict(int)
dd[1] = 'cat'

r1 = weakref.ref(c)
r2 = weakref.WeakKeyDictionary()
r2[c] = 1
r3 = weakref.WeakValueDictionary()
r3[1] = c
r4 = weakref.WeakSet()
r4.add(c)

# now that a bunch of balls are in the air, dump them to disk
if os.path.exists('objex-test.db'):
    os.remove('objex-test.db')
# first one for perf stats
dump_graph('objex-test.db', print_info=True, use_gc=False)
os.remove('objex-test.db')
dump_graph('objex-test.db', print_info=True, use_gc=True)
if os.path.exists('objex-test-analysis.db'):
    os.remove('objex-test-analysis.db')
make_analysis_db('objex-test.db', 'objex-test-analysis.db')
