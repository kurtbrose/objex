# Python memory-leak tooling landscape and objex's niche

Two tool families exist for hunting Python memory leaks; objex is in the
second, thinner one.

## Allocation-site profilers

These answer "*where* is memory being allocated": they find growth sites but
cannot answer "who is still holding this object" -- the retention question.

- [tracemalloc](https://docs.python.org/3/library/tracemalloc.html) -- stdlib
  since 3.4, maintained with CPython. Line-level allocation attribution;
  Python allocations only, blind to C extensions.
- [memray](https://github.com/bloomberg/memray) -- Bloomberg; actively
  maintained. Traces native + Python allocations, flamegraphs, <5% overhead,
  Linux/macOS. The current default recommendation for allocation questions.
- [memory_profiler](https://github.com/pythonprofilers/memory_profiler) --
  line-by-line RSS deltas; effectively unmaintained (last release 2022),
  mostly superseded by memray.

## Retention/object-graph tools

These answer "*why* is this object still alive":

- [objgraph](https://github.com/mgedmin/objgraph) -- maintained (slow
  cadence). In-process, `gc.get_referrers`-based backref chains, graphviz
  output. Requires running code inside the target process; no snapshot
  artifact.
- [guppy3 / heapy](https://github.com/zhuyifei1999/guppy3) -- actively
  maintained, supports 3.10-3.14. In-process heap analysis with its own C
  traversal; powerful, but analysis competes with the target process for
  memory.
- [pympler](https://github.com/pympler/pympler) -- sporadically maintained.
  In-process summaries plus class lifetime tracking.
- [meliae](https://pypi.org/project/meliae/) -- dump-to-JSON then offline
  analysis; the closest architectural cousin to objex. Effectively
  unmaintained.
- [PyHeap](https://github.com/ivanyu/pyheap) -- GDB-injected dumper plus
  offline analyzer with a browser UI. CPython 3.8-3.12, Linux only.
- [dump-heap](https://github.com/lhchavez/dump-heap) -- ptrace-injected
  whole-heap dumper, snapshot-diff oriented. x86_64 Linux only.

## objex's niche

objex's distinctive combination, shared by no maintained tool:
**fork-and-dump (near-zero pause for the target), a portable single-file
SQLite artifact, offline SQL-queryable analysis, and path-to-root attribution
against module/frame/thread roots with human-legible labels** (`.attr`,
`[i]`, `@key`, `.locals['x']`).

Notably, the gc-based competitors (objgraph, heapy) traverse via
`tp_traverse` and therefore see coroutines/iterators/tracebacks natively;
objex's labeled-edge scraping is its UX advantage and was historically its
completeness liability. The hybrid traversal (named labels where known,
`gc.get_referents`/`tp_traverse` fallback labeled `<gc>` everywhere else)
closes the completeness gap while keeping the label advantage, and the
gc-coverage audit in `tests/test_gc_coverage.py` keeps it closed across
CPython upgrades.

## Positioning

| Tool | Retention paths | Offline artifact | Prod-safe capture | Native allocs | Maintained |
|---|---|---|---|---|---|
| tracemalloc | no | snapshots (in-process) | overhead while tracing | no | stdlib |
| memray | no | yes (capture file) | yes (<5% overhead) | yes | yes |
| memory_profiler | no | no | no | no | no |
| objgraph | yes (in-process) | no | no (runs in target) | no | yes |
| guppy3/heapy | yes (in-process) | partial | no (runs in target) | no | yes |
| pympler | partial | no | no (runs in target) | no | sporadic |
| meliae | partial | yes (JSON dump) | dump pause | no | no |
| PyHeap | yes (offline) | yes | GDB stop-the-world | no | yes |
| dump-heap | diff-oriented | yes | ptrace pause | no | yes |
| **objex** | **yes (offline, labeled)** | **yes (single SQLite file)** | **yes (fork-and-dump)** | no | yes |

Rule of thumb: reach for memray/tracemalloc when the question is "what code
path is allocating all this memory"; reach for objex when the question is
"this object count/RSS keeps growing -- what is still holding these objects,
and from which module/frame root".
