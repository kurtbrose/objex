import gc
import os
try:
    import resource
except ImportError:  # windows
    resource = None
import shutil
import sys
from socket import getfqdn
import sqlite3
import time
import types


# MAINTENANCE NOTE: why are some python types "special" and get broken out as
# their own table type whereas others are not?
# the guiding principle is that objects which tend to need special display
# and/or collection logic get separate tables

_SCHEMA = '''
CREATE TABLE meta (
    id INTEGER PRIMARY KEY,
    ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    pid INTEGER NOT NULL,
    hostname TEXT NOT NULL,
    memory_mb INTEGER NOT NULL,
    gc_info TEXT NOT NULL,
    duration_s REAL
);

CREATE TABLE object (
    id INTEGER PRIMARY KEY,
    pytype INTEGER NOT NULL,
    size INTEGER NOT NULL,
    len INTEGER
);

CREATE TABLE pytype (
    id INTEGER PRIMARY KEY,
    object INTEGER NOT NULL,
    module INTEGER NOT NULL, -- object-id of module
    name TEXT NOT NULL -- typenames are okay
);

CREATE TABLE pytype_inherits (
    id INTEGER PRIMARY KEY,
    parent INTEGER NOT NULL, -- pytype
    child INTEGER NOT NULL -- pytype
);

CREATE TABLE module (
    id INTEGER PRIMARY KEY,
    object INTEGER NOT NULL,
    file TEXT NOT NULL,
    name TEXT NOT NULL
);

CREATE TABLE pyframe (
    id INTEGER PRIMARY KEY,
    object INTEGER NOT NULL,
    f_back INTEGER, -- object (pyframe or None)
    f_code INTEGER NOT NULL, -- object (code)
    f_globals INTEGER, -- object (a dict instance)  OR should this be a ref type e.g. locals["foo"], globals["bar"]
    f_locals INTEGER, -- object (a dict instance)
    f_lasti INTEGER NOT NULL, -- last instruction executed in code
    f_lineno INTEGER NOT NULL, -- line number in code
    trace TEXT NOT NULL  -- segment of a traceback kind of format (used for display to user)
);

CREATE TABLE pycode (  -- needed to assocaite pyframes back to function objects
    id INTEGER PRIMARY KEY,
    object INTEGER NOT NULL,
    co_name TEXT NOT NULL
);

CREATE TABLE function (
    id INTEGER PRIMARY KEY,
    object INTEGER NOT NULL,
    func_name TEXT NOT NULL,
    func_code INTEGER NOT NULL, -- object-id of pycode
    module INTEGER NOT NULL -- object-id of module
);

CREATE TABLE thread (
    id INTEGER PRIMARY KEY,
    stack INTEGER NOT NULL, -- object-id of pyframe
    thread_id INTEGER NOT NULL -- os-layer thread-id (key of sys._current_frames)
);

CREATE TABLE reference (
    src INTEGER NOT NULL, -- object
    dst INTEGER NOT NULL, -- object
    ref TEXT NOT NULL -- keys *might* be okay
);
'''


# these indices are applied when switching from
# "data-collection" mode to "analysis mode"
_INDICES = '''
CREATE INDEX pytype_object ON pytype(object);
CREATE INDEX pytype_name ON pytype(name);
CREATE INDEX object_pytype ON object(pytype);
CREATE INDEX object_size ON object(size);
CREATE INDEX object_len ON object(len);
CREATE INDEX object_all ON object(pytype, size, len);
CREATE INDEX reference_src ON reference(src);
CREATE INDEX reference_dst ON reference(dst);
CREATE INDEX reference_ref ON reference(ref);
CREATE INDEX reference_all ON reference(src, dst, ref);
'''


def _run_ddl(conn, ddl_block):
    """
    break a ; delimited list of DDL statements into
    a list of individual statements and execute them
    in conn
    """
    for ddl_stmt in ddl_block.split(';'):
        ddl_stmt = ddl_stmt.strip()
        if ddl_stmt:
            conn.execute(ddl_stmt)


def _get_memory_mb():
    if resource:
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    # windows, fall back to psutil
    import psutil
    return psutil.Process().memory_info()[0] / 1024.0 / 1024


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
        self.type_slots_map = {}
        self.started = time.time()
        # ignore ids not just to avoid analysis noise, but because these can
        # get pretty big over time, don't want to waste DB space
        self.ignore_ids = {id(e) for e in self.__dict__.values()}
        self.ignore_ids.add(id(self.ignore_ids))
        self.ignore_ids.add(self.__dict__)
        # commented out tracing code
        # TODO how to put it in w/out hurting perf if off?
        # (maybe optional decorators?)
        # self.times = []

    @classmethod
    def from_path(cls, path, use_wal=True):
        '''create a new instance that will dump state to path (which shouldn't exist)'''
        conn = sqlite3.connect(path)
        if use_wal:
            conn.execute("PRAGMA journal_mode = WAL")
        _run_ddl(conn, _SCHEMA)
        type_id_map = {id(type): 0}
        object_id_map = {id(type): 0}
        conn.execute(
            "INSERT INTO object (id, pytype, size, len) VALUES (0, 0, ?, null)",
            (sys.getsizeof(type),))
        conn.execute(
            "INSERT INTO pytype (id, object, name) VALUES (0, 0, 'type')")
        memory = _get_memory_mb()
        conn.execute(
            "INSERT INTO meta (id, pid, hostname, memory_mb, gc_info) VALUES (0, ?, ?, ?, ?)",
            (os.getpid(), getfqdn(), memory, ';'.join(gc.get_count())))
        return cls(conn, type_id_map, object_id_map)

    def execute(self, sql, params):
        # start = time.time()
        self.conn.execute(sql, params)
        # self.times.append(time.time() - start)

    def executemany(self, sql, params):
        # start = time.time()
        self.conn.executemany(sql, params)
        # total = time.time() - start
        # self.times.extend([total / len(params)] * len(params))

    def _ensure_db_id(self, obj, is_type=False):
        '''
        DB id creation is separate from full object creation
        as a kind of "forward declaration" step to avoid loops
        (e.g. an object that refers to itself)
        '''
        if id(obj) in self.object_id_map:
            return self.object_id_map[id(obj)]
        obj_id = self.object_id_map[id(obj)] = len(self.object_id_map)
        if hasattr(obj, '__class__'):
            type_type_id = self._ensure_db_id(obj.__class__, is_type=True)
        else:  # old-stype class
            type_type_id = self.type_id_map[id(type)]
        try:  # very hard to forward detect if this will works
            length = len(obj)
        except Exception:
            length = None
        self.execute(
            "INSERT INTO object (id, pytype, size, len) VALUES (?, ?, ?, ?)",
            (obj_id, type_type_id, sys.getsizeof(obj), length))
        if id(obj) not in self.type_id_map and (is_type or isinstance(obj, type)):
            obj_type_id = self.type_id_map[id(obj)] = len(self.type_id_map)
            self._ensure_db_id()
            self.execute(
                "INSERT INTO pytype (id, object, module, name) VALUES (?, ?, ?, ?)",
                (obj_type_id, obj_id, obj.__name__))
        return obj_id

    def add_obj(self, obj):
        '''
        add an object and references to this graph

        this funcion is idempotent; i.e. calling it 100x
        on the same object is the same as calling it once
        '''
        obj_id = id(obj)
        if obj_id in self.ignore_ids:
            return self.object_id_map.get(obj_id)
        self.ignore_ids.add(obj_id)
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
                ('@{}'.format(self.add_obj(key)), obj[key])
                for key in keys]
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
                if key in ('__dict__', '__weakref__'):
                    # see https://docs.python.org/3/reference/datamodel.html#slots
                    continue
                if key.startswith('__'):  # private slots name mangling
                    key = "_" + obj.__class__.__name__ + key
                try:
                    key_dst.append((key, getattr(obj, key)))
                except AttributeError:
                    pass  # just because a slot exists doesn't mean it has a value
        self.conn.executemany(
            "INSERT INTO reference (src, dst, ref) VALUES (?, ?, ?)",
            [(db_id, self.add_obj(dst), key) for key, dst in key_dst])
        return db_id

    def add_all(self):
        gc.collect()  # try to minimize garbage
        self.add_frames()
        for obj in gc.get_objects():
            self.add_obj(obj)

    def finish(self):
        self.conn.execute(
            "UPDATE meta SET duration_s = ?",
            (time.time() - self.started,))
        self.conn.commit()
        self.conn.close()


def dump_graph(path, print_info=False):
    '''
    dump a collection db to path;
    the collection db is designed to be small
    and write fast, so it needs post-processing
    to e.g. add indices and compute values
    before analysis
    '''
    start = time.time()
    grapher = _Writer.from_path(path)
    grapher.add_all()
    grapher.finish()
    if print_info:
        duration = time.time() - start
        memory = _get_memory_mb() 
        dumpsize = os.stat(path).st_size / 1024.0 / 1024  # MiB
        objects = len(gc.get_objects())
        print "process memory usage: {:0.3f}MiB".format(memory)
        print "total objects:", objects
        # print "wrote {} rows in {}".format(
        #     len(grapher.times), duration)
        # print "db perf: {:0.3f}us/row".format(
        #     1e6 * sum(grapher.times) / len(grapher.times))
        # print "overall perf: {:0.3f} s/GiB, {:0.03f} ms/object".format(
        #     1024 * duration / memory, 1000 * duration / objects)
        # print "duration - db time:", duration - sum(grapher.times)
        print "duration: {0:0.1f}".format(time.time() - grapher.started)
        print "compression - {:0.02f}MiB -> {:0.02f}MiB ({:0.01f}%)".format(
            memory, dumpsize, 100 * (1 - dumpsize / memory))


def make_analysis_db(collection_db_path, analysis_db_path):
    '''
    make an analysis SQLite DB from a collection SQLite DB
    by making a copy and adding indices to make analysis
    queries faster
    '''
    if not os.path.exists(collection_db_path):
        raise EnvironmentError(
            "collection DB doesn't exist at {}".format(collection_db_path))
    if os.path.exists(analysis_db_path):
        raise EnvironmentError(
            "analysis DB already exists at {}".format(analysis_db_path))
    shutil.copyfile(collection_db_path, analysis_db_path)
    conn = sqlite3.connect(analysis_db_path)
    _run_ddl(conn, _INDICES)


class Reader(object):
    '''read a graph dumped previously'''
    def __init__(self, path):
        self.path = path
        self.conn = sqlite3.connect(path)

    def sql(self, sql, args=None):
        '''run SELECT sql against underling DB'''
        if args is None:
            return self.conn.execute(sql).fetchall()
        return self.conn.execute(sql, args).fetchall()

    def sql_val(self, sql, args=None):
        '''run SELECT sql that returns single value'''
        return self.sql(sql, args)[0][0]

    def object_count(self):
        return self.sql_val('SELECT count(*) FROM object')

    def reference_count(self):
        return self.sql_val('SELECT count(*) FROM reference')

    def visible_memory_fraction(self):
        '''get the fraction of peak RSS that is accounted for'''
        return self.sql_val(
            'SELECT 1.0 * sum(size) / 1000000 / (SELECT memory_mb from meta) FROM object, meta')

    def cost_by_type(self):
        '''get (typename, percent memory, number of instances) ordered by percent memory'''
        return self.sql(
            'SELECT name, count(*), 100 * sum(size) / (1.0 * (SELECT sum(size) FROM object))'
            'FROM object JOIN pytype ON object.pytype = pytype.id GROUP BY name ORDER BY sum(size) DESC')

    def as_digraph(self):
        '''return an obj-id -> obj-id networkx DiGraph'''
        from networkx import DiGraph
        return DiGraph(self.conn.execute('SELECT src, dst FROM reference').fetchall())

    def obj_typename(self, obj_id):
        '''given an object id, return typename'''
        return self.sql_val(
            'SELECT name FROM object JOIN pytype ON object.pytype = pytype.id WHERE object.id = ?',
            (obj_id,))

    def obj_size(self, obj_id):
        return self.sql_val('SELECT size FROM object WHERE id = ?', (obj_id,))

    def obj_len(self, obj_id):
        return self.sql_val('SELECT len FROM object WHERE id = ?', (obj_id,))

    def obj_refers_to(self, obj_id, limit=20):
        '''given obj-id, return [(ref, obj-id), ...] for all of the objects this obj refers to'''
        return self.sql('SELECT ref, dst FROM reference WHERE src = ? LIMIT ?', (obj_id, limit))

    def obj_refers_to_count(self, obj_id):
        return self.sql_val('SELECT count(*) FROM reference WHERE src = ?', (obj_id,))

    def refers_to_obj(self, obj_id, limit=20):
        '''given obj-id, return [(ref, obj-id), ...] for all of the objects that refer to this obj'''
        return self.sql('SELECT ref, src FROM reference WHERE dst = ? LIMIT ?', (obj_id, limit))

    def refers_to_obj_count(self, obj_id):
        return self.sql_val('SELECT count(*) FROM reference WHERE dst = ?', (obj_id,))

    def obj_is_type(self, obj_id):
        return self.sql_val('SELECT EXISTS(SELECT 1 FROM pytype WHERE pytype.object = ?)', (obj_id,))

    def typename(self, obj_id):
        '''name of an object that IS a type'''
        return self.sql_val('SELECT name FROM pytype WHERE object = ?', (obj_id,))

    def obj_instances(self, obj_id, limit=20):
        return sum(
            self.sql(
                'SELECT id FROM object WHERE object.pytype = ('
                'SELECT id FROM pytype WHERE pytype.object = ?) LIMIT ?',
                (obj_id, limit)),
            ())

    def obj_instance_count(self, obj_id):
        return self.sql_val(
            'SELECT count(*) FROM object WHERE object.pytype = ('
            'SELECT id FROM pytype WHERE pytype.object= ?)',
            (obj_id,))

    def instances_by_typename(self, typename):
        return self.sql(
            'SELECT id FROM object WHERE pytype = (SELECT id FROM pytype WHERE name = ?)',
            (typename,))


def _info_str(reader, obj_id):
    if reader.obj_is_type(obj_id):
        return "{label} (instances={num_instances:,})".format(
            label=_obj_label(reader, obj_id),
            obj_id=obj_id,
            num_instances=reader.obj_instance_count(obj_id),
        )
    return '''{label} (size={size}, len={len})'''.format(
        label=_obj_label(reader, obj_id),
        obj_id=obj_id,
        size=reader.obj_size(obj_id),
        len=reader.obj_len(obj_id))


try:
    import colorama
except ImportError:
    pass
else:
    colorama.init()


def _obj_label(reader, obj_id):
    try:
        from termcolor import colored
    except ImportError:
        colored = lambda s, color: s

    if reader.obj_is_type(obj_id):
        return colored("<type {}@{}>".format(reader.typename(obj_id), obj_id), 'green')
    return colored("<{}@{}>".format(reader.obj_typename(obj_id), obj_id), 'red')


class CLI(object):
    def __init__(self, reader, obj_id=0):
        self.reader = reader
        self.obj_id = obj_id

    def _menu(self):
        try:
            from termcolor import colored
        except ImportError:
            colored = lambda s, color: s

        label = _obj_label(self.reader, self.obj_id)
        refers_to_obj = []
        i = 0
        for ref, src in self.reader.refers_to_obj(self.obj_id):
            refers_to_obj.append('({}) - {}: {}'.format(
                i, ref, _info_str(self.reader, src)))
            i += 1
        obj_refers_to = []
        for ref, dst in self.reader.obj_refers_to(self.obj_id):
            obj_refers_to.append('({}) - {}: {}'.format(
                i, ref, _info_str(self.reader, dst)))
            i += 1
        lines = [
            "CUR: {}".format(_info_str(self.reader, self.obj_id)),
            "{:,} objects refer to {}...".format(
                self.reader.refers_to_obj_count(self.obj_id),
                label)
        ] + refers_to_obj + [
            "{} refers to {:,} objects...".format(
                label,
                self.reader.obj_refers_to_count(self.obj_id))
        ] + obj_refers_to
        if self.reader.obj_is_type(self.obj_id):
            instances = []
            for inst in self.reader.obj_instances(self.obj_id):
                instances.append('({}) - {}'.format(i, _info_str(self.reader, inst)))
                i += 1
            lines += [
                '{} has {:,} instances...'.format(
                    label,
                    self.reader.obj_instance_count(self.obj_id))
            ] + instances
        return '\n'.join(lines)

    def _choices(self):
        '''return choices to go to as a dict'''
        choices = {}
        i = 0
        for ref, src in self.reader.refers_to_obj(self.obj_id):
            choices[str(i)] = src
            i += 1
        for ref, dst in self.reader.obj_refers_to(self.obj_id):
            choices[str(i)] = dst
            i += 1
        if self.reader.obj_is_type(self.obj_id):
            for inst in self.reader.obj_instances(self.obj_id):
                choices[str(i)] = inst
                i += 1
        return choices

    def run(self):
        print("WELCOME TO OBJECT BROWSER")
        print("you are browsing {} collected from {} at {}".format(
            self.reader.path,
            self.reader.sql_val('SELECT hostname FROM meta'),
            self.reader.sql_val('SELECT ts FROM meta'),
        ))
        print("total RSS memory was {}MiB; {:0.01f}MiB ({:0.01f}%) found in {:,} python objects".format(
            self.reader.sql_val('SELECT memory_mb FROM meta'),
            self.reader.sql_val('SELECT SUM(size) FROM object') / 1024 / 1024,
            self.reader.visible_memory_fraction() * 100,
            self.reader.object_count(),
        ))
        while 1:
            print(self._menu())
            self.obj_id = self._choices()[raw_input("GO TO:")]
