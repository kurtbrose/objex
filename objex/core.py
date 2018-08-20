import gc
import os
import resource
import sys
import sqlite3
import time
import types


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
    def from_path(cls, path, use_wal=True):
        '''create a new instance that will dump state to path (which shouldn't exist)'''
        conn = sqlite3.connect(path)
        if use_wal:
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

    def _add_referents(self, obj):
        '''
        obj is something that is tracked by gc
        '''
        db_id = self._ensure_db_id(obj)
        key_dst = []
        mode = "object"
        t = type(obj)
        # STEP 1 - FIGURE OUT WHICH MODE TO USE
        if t is dict:
            mode = "dict"
        elif t is list or t is tuple:
            mode = "list"
        elif isinstance(obj, dict):
            mode = "dict"
        elif isinstance(obj, (list, tuple)):
            mode = "list"
        # STEP 2 - GET KEYS
        if mode == "dict":
            keys = obj.keys()
            key_dst += [('<key>', key) for key in keys] + [
                ('<object@' + str(self._ensure_db_id(key)) + '>', 
                 obj[key]) for key in keys]
        if mode == "list":
            key_dst += enumerate(obj)
        if mode == "object":
            if hasattr(obj, "__dict__"):
                key_dst += obj.__dict__.items()
            try:
                slots = obj.__class__.__slots__
            except AttributeError:
                slots = ()
            for key in slots:
                if key == '__dict__':
                    continue
                if key.startswith('__'):  # private slots name mangling
                    key = "_" + obj.__class__.__name__ + key
                key_dst.append((key, getattr(obj, key)))
        self.conn.executemany(
            "INSERT INTO reference (src, dst, ref) VALUES (?, ?, ?)",
            [(db_id, self._ensure_db_id(dst), key) for key, dst in key_dst])

    def add_obj(self, obj):
        '''add an object and references to this graph'''
        #self._add_referrers(obj)  # things that point at obj
        self._add_referents(obj)  # things that obj points at

    def add_all(self):
        for obj in gc.get_objects():
            self.add_obj(obj)

    def finish(self):
        self.conn.commit()
        self.conn.close()


def dump_graph(path):
    start = time.time()
    grapher = _Writer.from_path(path)
    grapher.add_all()
    grapher.finish()
    duration = time.time() - start
    memory = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0  # MiB
    dumpsize = os.stat(path).st_size / 1024.0 / 1024  # MiB
    objects = len(gc.get_objects())
    print "process memory usage: {:0.3f}MiB".format(memory)
    print "total objects:", objects
    print "wrote {} rows in {}".format(
        len(grapher.times), duration)
    print "db perf: {:0.3f}us/row".format(
        1e6 * sum(grapher.times) / len(grapher.times))
    print "overall perf: {:0.3f} s/GiB, {:0.03f} ms/object".format(
        1024 * duration / memory, 1000 * duration / objects)
    print "duration - db time:", duration - sum(grapher.times)
    print "compression - {:0.02f}MiB -> {:0.02f}MiB ({:0.01f}%)".format(
        memory, dumpsize, 100 * (1 - dumpsize / memory))


class Reader(object):
    '''read a graph dumped previously'''
    def __init__(self, path):
        self.conn = sqlite3.connect(path)

    def get_object_count(self):
        return self.conn.execute('SELECT count(*) FROM object').fetchall()[0][0]

    def get_reference_count(self):
        return self.conn.execute('SELECT count(*) FROM reference').fetchall()[0][0]
