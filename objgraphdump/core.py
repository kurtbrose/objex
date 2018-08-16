import gc
import sys
import sqlite3
import time
import types

from . import checkers


_SCHEMA = '''
CREATE TABLE pytype (
    id INTEGER PRIMARY KEY,
    object INTEGER NOT NULL,
    name TEXT NOT NULL -- typenames are okay
);

CREATE TABLE object (
    id INTEGER PRIMARY KEY,
    pytype INTEGER NOT NULL,
    size INTEGER NOT NULL,
    len INTEGER
);

CREATE TABLE reference (
    src INTEGER NOT NULL, -- object
    dst INTEGER NOT NULL, -- object
    ref TEXT NOT NULL -- keys *might* be okay
);
'''


def _dict_rel(obj, ref):
    '''extract the dict-relations between object obj and dict ref'''
    src_keys = []
    for item in ref.items():
        if item[0] is obj:
            src_keys.append((ref, '<key>'))
        if item[1] is obj:
            src_keys.append((ref, repr(item[0])))
    return src_keys


class _Writer(object):
    '''
    responsible for dumping objects
    '''
    def __init__(self, conn, type_id_map, object_id_map):
        self.conn = conn
        self.type_id_map = type_id_map
        self.object_id_map = object_id_map
        self.times = []

    @classmethod
    def from_path(cls, path):
        '''create a new instance that will dump state to path (which shouldn't exist)'''
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA journal_mode = WAL")
        for ddl_stmt in _SCHEMA.split(';'):
            ddl_stmt = ddl_stmt.strip()
            if ddl_stmt:
                conn.execute(ddl_stmt)
        type_id_map = {type: 0}
        object_id_map = {type: 0}
        conn.execute(
            "INSERT INTO object (id, pytype, size, len) VALUES (0, 0, ?, null)",
            (sys.getsizeof(type),))
        conn.execute(
            "INSERT INTO pytype (id, object, name) VALUES (0, 0, 'type')")
        return cls(conn, type_id_map, object_id_map)

    def execute(self, sql, params):
        start = time.time()
        self.conn.execute(sql, params)
        self.times.append(time.time() - start)

    def executemany(self, sql, params):
        start = time.time()
        self.conn.executemany(sql, params)
        total = time.time() - start
        self.times.extend([total / len(params)] * len(params))

    def _ensure_db_id(self, obj):
        if id(obj) in self.object_id_map:
            return self.object_id_map[id(obj)]
        obj_id = self.object_id_map[id(obj)] = len(self.object_id_map)
        if hasattr(obj, '__class__'):
            type_id = self._ensure_db_id(obj.__class__)
            if obj.__class__ not in self.type_id_map:
                # idiom for assigning items of type_id_map positive integers
                # 0, 1, 2, 3, ...
                type_type_id = self.type_id_map[obj.__class__] = len(self.type_id_map)
                self.execute(
                    "INSERT INTO pytype (id, object, name) VALUES (?, ?, ?)",
                    (type_type_id, type_id, obj.__class__.__name__))
            else:
                type_type_id = self.type_id_map[obj.__class__]
        else:  # old-stype class
            type_type_id = self.type_id_map[type]
        try:  # very hard to forward detect if this will works
            length = len(obj)
        except Exception:
            length = None
        self.execute(
            "INSERT INTO object (id, pytype, size, len) VALUES (?, ?, ?, ?)",
            (obj_id, type_type_id, sys.getsizeof(obj), length))
        # just in case a class doesn't have any instances it will still be populated
        if id(obj) not in self.type_id_map and isinstance(obj, type):
            obj_type_id = self.type_id_map[id(obj)] = len(self.type_id_map)
            self.execute(
                "INSERT INTO pytype (id, object, name) VALUES (?, ?, ?)",
                (obj_type_id, type_id, obj.__name__))
        return obj_id

    def _add_referrers(self, obj):
        src_keys = []
        db_id = self._ensure_db_id(obj)
        for ref in gc.get_referrers(obj):
            if ref is self.object_id_map or ref is self.type_id_map:
                continue
            if isinstance(ref, dict):
                src_keys.extend(_dict_rel(obj, ref))
            elif isinstance(ref, (list, tuple)):
                for idx in checkers.find_in_tuple_or_list(obj, ref):
                    src_keys.append((ref, str(idx)))
            elif type(ref) is types.FrameType:
                frames_ref = dict(ref.f_locals)
                frames_ref.update(ref.f_globals)
                src_keys.extend(_dict_rel(obj, frames_ref))
            elif type(ref) is types.MethodType:
                src_keys.extend(_dict_rel(
                    obj,
                    {
                        "im_class": ref.im_class,
                        "im_func": ref.im_func,
                        "im_self": ref.im_self
                    }
                ))
            elif hasattr(ref, '__dict__'):
                src_keys.extend(_dict_rel(obj, ref.__dict__))
            if type(ref) is obj:
                src_keys.append((ref, '__class__'))
        if not src_keys:
            return
        self.conn.executemany(
            "INSERT INTO reference (src, dst, ref) VALUES (?, ?, ?)",
            [(self._ensure_db_id(src), db_id, key) for src, key in src_keys])

    def add_obj(self, obj):
        '''add an object and references to this graph'''
        self._add_referrers(obj)

    def add_all(self):
        for obj in gc.get_objects():
            self.add_obj(obj)

    def flush(self):
        self.conn.commit()


def dump_graph(path):
    start = time.time()
    grapher = _Writer.from_path(path)
    grapher.add_all()
    grapher.flush()
    duration = time.time() - start
    print "wrote {} rows in {}".format(len(grapher.times), duration)
    print "db perf: {0:3f}ms/row".format(1000 * sum(grapher.times) / len(grapher.times))
    print "duration - db time:", duration - sum(grapher.times)


class Reader(object):
    '''read a graph dumped previously'''
    def __init__(self, path):
        self.conn = sqlite3.connect(path)

    def get_object_count(self):
        return self.conn.execute('SELECT count(*) FROM object').fetchall()[0][0]

    def get_reference_count(self):
        return self.conn.execute('SELECT count(*) FROM reference').fetchall()[0][0]


dump_graph('t23.db')
