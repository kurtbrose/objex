'''
Ground-truth traversal coverage audit: for every gc-tracked object the
exporter saw, every `gc.get_referents` (tp_traverse) edge to another seen
object must have a corresponding row in the reference table.

This is the Python-upgrade regression guard: a new CPython version that
introduces an object type the exporter silently fails to traverse shows up
here as a readable list of missing type names.
'''

import gc
import sqlite3

import pytest

from objex.dbutils import _run_ddl
from objex.exporter import _Writer
from objex.schema import _SCHEMA


# types with a known, justified traversal gap; every entry needs a one-line
# reason. Expected ~empty thanks to the <gc> fallback in _Writer.add_obj --
# the residue is objects mutated AFTER traversal (this audit re-runs
# gc.get_referents once the dump is complete, so anything the audit itself
# touches can grow referents the exporter never saw).
ALLOWED_GAPS = frozenset({
    'frame',  # live stacks (this test's own) mutate between traversal and audit
    'dict',  # module/instance dicts gain entries after traversal (e.g. __main__)
    'Connection',  # this audit's sqlite3 statement cache grows as it queries
    '_lru_cache_wrapper',  # C-internal caches gain entries after traversal
})


@pytest.mark.scenario
def test_gc_referents_are_traversed(tmp_path):
    db_path = tmp_path / 'coverage.db'
    conn = sqlite3.connect(str(db_path))
    conn.text_factory = str
    try:
        _run_ddl(conn, _SCHEMA)
        writer = _Writer(conn)
        writer.add_all()
        conn.commit()

        edge_set = set(conn.execute('SELECT src, dst FROM reference'))
        # dict keys are encoded in the ref string ('@<key_db_id>'), not as dst
        edge_set.update(conn.execute(
            "SELECT src, CAST(substr(ref, 2) AS INTEGER) FROM reference"
            " WHERE ref LIKE '@%'"))
    finally:
        conn.close()

    object_id_map = writer.object_id_map
    misses = {}  # type name -> [(src_db_id, dst_db_id, dst_type_name), ...]
    for obj in writer.all_objects:
        src_db_id = object_id_map.get(id(obj))
        if src_db_id is None:
            continue
        for referent in gc.get_referents(obj):
            if referent is type(obj):
                continue  # instance -> type edge lives in object.pytype, not reference
            dst_db_id = object_id_map.get(id(referent))
            if dst_db_id is None:
                continue
            if (src_db_id, dst_db_id) not in edge_set:
                misses.setdefault(type(obj).__name__, []).append(
                    (src_db_id, dst_db_id, type(referent).__name__))

    unexpected = {name: len(examples) for name, examples in misses.items()
                  if name not in ALLOWED_GAPS}
    assert not unexpected, (
        'exporter missed tp_traverse edges for types (type -> miss count): '
        '%r; examples: %r' % (
            unexpected,
            {name: misses[name][:3] for name in unexpected},
        ))
