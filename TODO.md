# objex TODO

## Extra export data

- Truncated repr for user classes and strings (dicts, lists, etc. can be reconstructed already)
- boltons.ecoutils fingerprint
- export duration
- shared libraries loaded?

## Command grammar

* back
* forward
* mark - flag an object as interesting
* in - list the inbound references to this object
* out
* up - looking at frame, up in the stack. looking at type, up in the inheritance.
* down - counterpart to up
* go

## Other

* convenience function for fork-and-export
* support fork() equivalent for Windows (it borks on sockets, but that's fine for us)
