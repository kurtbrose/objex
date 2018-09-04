# objex TODO

## ROADMAP

Extensions
- collections.deque, collections.defaultdict
- frame, func, code etc
- old style classes?

Tests
- CI
- "close the loop" on dump -- check that DB was written CORRECTLY not just that it didn't crash

Py3 Support
- briefly worked, atrophied w/out tests
- old-style classes

CLI
- better commands / sub-commands / flags /etc (face interface?)
- help

Library Meta-Data in dump
- detect version mismatch between export and exploration

## Extra export data

- Truncated repr for user classes and strings (dicts, lists, etc. can be reconstructed already)
- boltons.ecoutils fingerprint
- export duration
- sys.getrefcount of every object
- shared libraries loaded?
- slots + type cache + save mro

## Command grammar

first chars in the list below: 'bdfghilmopstuw'  (keep updated)

* back
* forward
* mark - flag an object as interesting
* in - list the inbound references to this object
* out
* up - looking at frame, up in the stack. looking at type, up in the inheritance.
* down - counterpart to up
* go
* list
* where - if a frame, includes stack, if a type, includes mro, always gives some idea of orientation (traversal path history)
* history
* path
* top - high-level statistics (most references, most central, etc.)
* search? or survey?
* python/sql?

Other:

* "in all" -> pager list (with no shortcuts)
* manually traversed path so far (needs normalization, in the case of cycles)
* automated audit checks: e.g., detect leaks via frames (i.e., via tracebacks hanging around)
* aliases: @mod_name, @module.module_global name, @TypeName?
* prompt toolkit

## Metrics

* flood fill paths to give steps to nearest module, or if not accessible via module, frames
  * common prefix of paths interesting?
* centrality, weighted by sizeof
* pagerank of the reverse directed graph (suspected)
* some way of quantifying loosely connected components (hanging threads) (community stuff too slow)

## Other

* convenience function for fork-and-export
* support fork() equivalent for Windows (it borks on sockets, but that's fine for us)


## Traversal

* Iterators are an example of a group of "builtin" (aka implemented in
  C) types that can hold onto a reference that's not accessible from
  Python. There are listerators dictkey iterators, dictvalue
  iterators, set iterators, etc. Luckily, like dictproxy they only
  hold onto one reference and it's the relevant container. Might be
  able to develop a heuristic around these kinds of types.

## CLI

* Subcommands (possibly separate commands once export/explore apps are separated)
  * snapshot/capture
  * explore

* objex capture --path rel_file.db --delay 30 -- python_script.py args
  * path defaults to <local dir>/objex-<script_name_slug>-<hostname>-<isodt>.db
  * --use-gc defaults to false, needs a better name, more of a debug option
  * --delay is the number of seconds to wait before capturing
  * a --count might be easy to implement to do multiple dumps, gonna
    have to parse and rewrite their path though if it's a custom one,
    and the explorer doesn't support it.
  * --min-size: don't capture unless/until the maxrss has crested this size
* objex explore
  * should autoimport (with message to that effect) if the database in
    question doesn't have indices built, maybe a --auto flag to not
    prompt.
  * --with-graph also loads the data into a networkx digraph
