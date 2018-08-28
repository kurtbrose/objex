# objex TODO

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
* ranks - high-level statistics (most references, most central, etc.)
* search?
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
