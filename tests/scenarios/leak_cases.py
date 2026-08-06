'''
Leak-scenario fixtures: each scenario constructs a leak whose payload objects
are reachable only through a "tricky" edge (traceback, closure cell, suspended
coroutine frame, C-internal cache, ...), then dumps the process object graph.

Run standalone in a bare subprocess:

    python -m tests.scenarios.leak_cases <scenario> <dump_path>

Each scenario defines a uniquely named payload class (``Payload_<scenario>``)
so tests can locate instances by type name with zero ambiguity, and keeps the
leak alive via module globals or live threads/loops as the scenario dictates.
'''

import argparse
import asyncio
import functools
import threading

from objex import dump_graph


def _payload_class(name):
    cls = type('Payload_' + name, (), {})
    cls.__module__ = __name__
    return cls


def module_global(dump_path):
    '''baseline sanity: payloads held by a module-level list'''
    cls = _payload_class('module_global')
    globals()['LEAK_module_global'] = [cls() for _ in range(12)]
    del cls
    dump_graph(dump_path)


def exception_traceback(dump_path):
    '''
    an exception raised deep in a 10-frame call chain with a payload in the
    locals of every frame, caught and stored on a module global; only the
    exception object is stored -- payloads are reachable solely via
    __traceback__ -> tb_frame -> locals
    '''
    cls = _payload_class('exception_traceback')

    def deep(n):
        payload = cls()  # noqa: F841 -- pinned by the frame in the traceback
        if n <= 1:
            raise RuntimeError('leak anchor')
        deep(n - 1)

    try:
        deep(10)
    except RuntimeError as exc:
        globals()['LEAK_exception_traceback'] = exc
    del cls
    dump_graph(dump_path)


def _make_closure_callback(cls):
    # built in a factory: `del payloads` in the caller would clear the shared
    # closure cell (deleting a cellvar empties the cell), killing the leak
    payloads = [cls() for _ in range(12)]

    def callback():
        return payloads

    return callback


def closure_cell(dump_path):
    '''a module-global callback whose closure cell is the only path to payloads'''
    cls = _payload_class('closure_cell')
    globals()['LEAK_closure_cell'] = _make_closure_callback(cls)
    del cls
    dump_graph(dump_path)


def suspended_coroutine(dump_path):
    '''
    a sibling asyncio.Task suspended at an await with payloads in its frame
    locals; the dump happens while the loop is running, so payloads are
    reachable only via Task -> coroutine -> cr_frame -> locals
    '''
    cls = _payload_class('suspended_coroutine')

    async def holder(gate):
        payloads = [cls() for _ in range(12)]  # noqa: F841 -- pinned by the suspended frame
        await gate.wait()

    async def main():
        gate = asyncio.Event()
        task = asyncio.ensure_future(holder(gate))
        globals()['LEAK_suspended_coroutine'] = task
        await asyncio.sleep(0)  # let holder run up to its await
        dump_graph(dump_path)
        gate.set()
        await task

    asyncio.run(main())


def thread_frame(dump_path):
    '''a daemon thread parked in a deep call with payloads in its locals'''
    cls = _payload_class('thread_frame')
    stop_event = threading.Event()
    started = threading.Event()

    def parked(payloads, n=10):
        if n:
            return parked(payloads, n - 1)
        started.set()
        while not stop_event.wait(0.01):
            pass

    thread = threading.Thread(
        target=parked, args=([cls() for _ in range(12)],), daemon=True)
    thread.start()
    assert started.wait(timeout=5)
    del cls
    dump_graph(dump_path)
    stop_event.set()
    thread.join(timeout=5)


def lru_cache(dump_path):
    '''payload containers held only by a C-internal lru_cache dict'''
    cls = _payload_class('lru_cache')

    @functools.lru_cache(maxsize=None)
    def cached(n):
        return [cls() for _ in range(3)]

    for i in range(4):
        cached(i)
    globals()['LEAK_lru_cache'] = cached
    del cls, cached
    dump_graph(dump_path)


def iterator_pin(dump_path):
    '''a module-global dict_keyiterator is the only strong path to a dict of payloads'''
    cls = _payload_class('iterator_pin')
    payload_dict = {cls(): i for i in range(12)}
    globals()['LEAK_iterator_pin'] = iter(payload_dict.keys())
    del cls, payload_dict
    dump_graph(dump_path)


SCENARIOS = {
    'module_global': module_global,
    'exception_traceback': exception_traceback,
    'closure_cell': closure_cell,
    'suspended_coroutine': suspended_coroutine,
    'thread_frame': thread_frame,
    'lru_cache': lru_cache,
    'iterator_pin': iterator_pin,
}

# minimum payload instances each scenario guarantees at dump time
PAYLOAD_COUNTS = {
    'module_global': 12,
    'exception_traceback': 10,
    'closure_cell': 12,
    'suspended_coroutine': 12,
    'thread_frame': 12,
    'lru_cache': 12,
    'iterator_pin': 12,
}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('scenario', choices=sorted(SCENARIOS))
    parser.add_argument('dump_path')
    args = parser.parse_args(argv)
    SCENARIOS[args.scenario](args.dump_path)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
