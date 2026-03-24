import argparse
import collections
import re
import threading
import time
import types
import weakref

from objex import dump_graph


GO_NESTED = None


class LegacyA:
    pass


class SlotsA:
    __slots__ = ('a', '__weakref__')


class SlotsB(SlotsA):
    __slots__ = ('b',)


class SlotsC(SlotsB):
    pass


class StaticAndClassMethods:
    @staticmethod
    def static_method():
        return 1

    @classmethod
    def class_method(cls):
        return cls.__name__


class NoneModule:
    pass


NoneModule.__module__ = None


class ExplodingGetattr:
    def __getattr__(self, name):
        if name == '__dict__':
            raise NameError("no mapped classes registered under the name '__dict__'")
        raise AttributeError(name)


class MisdirectingGetattribute:
    def __init__(self):
        self.value = 1
        self._pattern = re.compile('x')

    def __getattribute__(self, name):
        if name == '__dict__':
            return getattr(object.__getattribute__(self, '_pattern'), name)
        return object.__getattribute__(self, name)


def closing(a, b=1):
    c = 2

    def has_closure(d):
        return a + b + c + d

    return has_closure


def make_empty_closure():
    value = 'sentinel'

    def inner():
        return value

    del value
    return inner


def make_sample_objects():
    legacy_a = LegacyA()

    slots_c = SlotsC()
    slots_c.a = 'slot-a'
    slots_c.b = 'slot-b'
    slots_c.c = 'normal-attr'

    deque_obj = collections.deque([1, 2, 3])
    defaultdict_obj = collections.defaultdict(int)
    defaultdict_obj[1] = 'cat'

    closure = closing(1)
    empty_closure = make_empty_closure()
    exploding_getattr = ExplodingGetattr()
    misdirecting_getattribute = MisdirectingGetattribute()
    bound_method = StaticAndClassMethods().class_method
    generator = (item for item in range(2))

    weak_set = weakref.WeakSet()
    weak_set.add(slots_c)

    global GO_NESTED
    GO_NESTED = types.SimpleNamespace(
        level1=types.SimpleNamespace(
            level2=types.SimpleNamespace(
                target=deque_obj,
            ),
        ),
    )

    return {
        'legacy_a': legacy_a,
        'slots_c': slots_c,
        'deque_obj': deque_obj,
        'defaultdict_obj': defaultdict_obj,
        'closure': closure,
        'empty_closure': empty_closure,
        'exploding_getattr': exploding_getattr,
        'misdirecting_getattribute': misdirecting_getattribute,
        'bound_method': bound_method,
        'generator': generator,
        'weak_set': weak_set,
        'go_nested': GO_NESTED,
    }


def stacker(stop_event, started, n=25):
    if n:
        return stacker(stop_event, started, n - 1)
    started.set()
    while not stop_event.wait(0.01):
        time.sleep(0.01)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('path')
    parser.add_argument('--use-gc', action='store_true')
    args = parser.parse_args(argv)

    stop_event = threading.Event()
    started = threading.Event()
    sample_objects = make_sample_objects()
    thread = threading.Thread(target=stacker, args=(stop_event, started), daemon=True)
    thread.start()
    assert started.wait(timeout=2)
    try:
        assert sample_objects
        dump_graph(args.path, use_gc=args.use_gc)
    finally:
        stop_event.set()
        thread.join(timeout=2)


if __name__ == '__main__':
    raise SystemExit(main())
