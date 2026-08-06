# objex

Objex - Export and explore Python object graphs

## install

Core library only:

```bash
uv add objex
```

CLI extras for the interactive analysis UI:

```bash
uv add '.[cli]'
```

## when to use objex

objex answers retention questions: "this object count keeps growing -- what is
still holding these objects, and from which module/frame root?" It captures via
fork-and-dump (near-zero pause for the target) into a portable single-file
SQLite artifact you analyze offline, with labeled path-to-root attribution.
For allocation-site questions ("what code path is allocating all this
memory?"), use [memray](https://github.com/bloomberg/memray) or stdlib
`tracemalloc` instead; see
[docs/2026-08-06_leak_tool_landscape.md](docs/2026-08-06_leak_tool_landscape.md)
for the full tool landscape.

# workflow

1- collect data from a running process (this comines well with `os.fork`)

```python
pid = objex.spawn_dump('dump.db')
objex.wait_dump(pid)
```

2- create an analysis database (this takes a few minutes as indices are added)

```bash
python -m objex make-analysis-db dump.db analysis.db
```

3- browse the extracted object graph

```bash
python -m objex analysis.db
WELCOME TO OBJEX EXPLORER
Now exploring "dump.db" collected from a0acf3b86f31 at 2018-09-04 18:25:46
RSS memory was 190.43MiB; 151.0MiB (79.8%) found in 622,108 python objects
(Type "help" for options.)
```

# agent/scripting usage

Every explorer query is available as a one-shot, pipeable JSON invocation via
the `q` subcommand -- no REPL needed:

```bash
python -m objex q analysis.db summary               # {"object_count": ..., ...}
python -m objex q analysis.db root-summary sample_size=200
python -m objex q analysis.db go q=SomeType         # resolve a type/path query
python -m objex q analysis.db path-to-module id=4211
```

Endpoints mirror the web API: `summary`, `object`, `random`, `referents`,
`referrers`, `type-search`, `top-types`, `largest-objects`, `root-summary`,
`path-to-module`, `path-to-frame`, `go`, `marks`, `mark`. Parameters are
`key=value` tokens. Exit code is 0 on success, 1 otherwise (error payloads are
JSON too).

There is also a read-only SQL escape hatch (schema documented in
`objex/schema.py`):

```bash
python -m objex q analysis.db sql "SELECT name, count(*) FROM pytype GROUP BY name ORDER BY 2 DESC LIMIT 10"
```

# how to debug a memory leak

Assuming the process has leaked significantly (e.g. doubled or more in memory footprint since it started),
random objects are likely to be leaks.  So, `go` to a random object.

```
WELCOME TO OBJEX EXPLORER
Now exploring "dump.db" collected from a0acf3b86f31 at 2018-09-04 18:25:46
RSS memory was 190.43MiB; 151.0MiB (79.8%) found in 622,108 python objects
(Type "help" for options.)
objex> go random

Now at: <lithoxyl.action.Action#580111> (size=64, refcount=5)
```

Next, use the `in` command to see what global data structure is referring to the leaked object:

```
objex> in
...

1 modules transitively refer to <lithoxyl.action.Action#580111>:
  5 - <module lithoxyl.context#10871>.LITHOXYL_CONTEXT.loggers[0]._all_sinks[0].begin_events[2].action

...
```

# learn about your processes memory space

`go random` and `in` can also be tools to learn new things about your processes memory structure:

```
objex> go random

Now at: <str#4774> (size=81, refcount=4, len=48)
objex> in
1 objects refer to <str#4774>:
  1 -  <function normpath#4394>.__doc__


1 modules transitively refer to <str#4774>:
  2 - <module ntpath#2459>.normpath.__doc__

1 frames transitively refer to <str#4774>:
  3 - <frame <module>#8316>.locals['os'].path.normpath.__doc__

objex> go random

Now at: <str#16576> (size=49, refcount=5, len=16)
objex> in
0 objects refer to <str#16576>:


2 modules transitively refer to <str#16576>:
  1 - <module objex#2441>.Reader.__dict__[<str#16576>]
  2 - <module objex.explorer#2490>.Reader.__dict__[<str#16576>]

1 frames transitively refer to <str#16576>:
  3 - <frame <module>#8316>.locals['make_analysis_db'].__module__.Reader.__dict__[<str#16576>]

objex> go random

Now at: <frame stacker#219> (size=432, refcount=5)
objex> in
1 objects refer to <frame stacker#219>:
  1 -  <frame stacker#218>.f_back



objex> go random

Now at: <function create_string_buffer#14902> (size=112, refcount=6)
objex> in
6 objects refer to <function create_string_buffer#14902>:
  1 -  <module ctypes#2466>.create_string_buffer
  2 -  <dict#14968>[<str#15003>]
  3 -  <module ctypes._endian#2428>.create_string_buffer
  4 -  <dict#15652>[<str#15003>]
  5 -  <module ctypes.wintypes#2447>.create_string_buffer
  6 -  <dict#15723>[<str#15003>]


3 modules transitively refer to <function create_string_buffer#14902>:
  7 - <module ctypes#2466>.create_string_buffer
  8 - <module ctypes._endian#2428>.create_string_buffer
  9 - <module ctypes.wintypes#2447>.create_string_buffer

1 frames transitively refer to <function create_string_buffer#14902>:
 10 - <frame <module>#8316>.locals['collections']._sys.modules[<str#2320>].create_string_buffer
 ```
