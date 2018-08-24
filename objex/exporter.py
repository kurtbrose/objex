import gc
import inspect
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

from boltons.tbutils import Callpoint

from .schema import _SCHEMA
from .dbutils import _run_ddl

# MAINTENANCE NOTE: why are some python types "special" and get broken out as
# their own table type whereas others are not?
# the guiding principle is that objects which tend to need special display
# and/or collection logic get separate tables

# TODO: refactor foo to foo_obj_id when it refers to a row in the object table


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
    _TRACKED_TYPES = (types.ModuleType, types.FrameType, types.FunctionType, types.CodeType)

    def __init__(self, conn):
        self.conn = conn
        self.type_id_map = {}  # map of type ids to pytype rowids
        self.object_id_map = {}  # map of object ids to object rowids
        self.tracked_t_id_map = {t: {} for t in self._TRACKED_TYPES}
        self.type_slots_map = {}  # map of type ids to __slots__
        self.modules_map = dict(sys.modules)  # map of __module__ to fake modules when no entry in sys.modules
        self.started = time.time()
        # ignore ids not just to avoid analysis noise, but because these can
        # get pretty big over time, don't want to waste DB space
        self.ignore_ids = {id(e) for e in self.__dict__.values()}
        self.ignore_ids.add(id(self.ignore_ids))
        self.ignore_ids.add(id(self.__dict__))
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
        memory = _get_memory_mb()
        conn.execute(
            "INSERT INTO meta (id, pid, hostname, memory_mb, gc_info) VALUES (0, ?, ?, ?, ?)",
            (os.getpid(), getfqdn(), memory, '[{},{},{}]'.format(*gc.get_count())))
        return cls(conn)

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
        # this is a quick idiom for assigning integers to objects
        # e.g. first thing in object_id_map gets assigned 0, second -> 1, etc...
        obj_id = self.object_id_map[id(obj)] = len(self.object_id_map)
        type_obj_id = self._ensure_db_id(type(obj), is_type=True)
        try:  # very hard to forward detect if this will works
            length = len(obj)
        except Exception:
            length = None
        self.execute(
            "INSERT INTO object (id, pytype, size, len) VALUES (?, ?, ?, ?)",
            (obj_id, type_obj_id, sys.getsizeof(obj), length))
        # all of these are pretty rare (maybe optimize?)
        if id(obj) not in self.type_id_map and (is_type or isinstance(obj, type)):
            obj_type_id = self.type_id_map[id(obj)] = len(self.type_id_map)
            module_obj_id = self._module_name2obj_id(obj.__module__)
            self.execute(
                "INSERT INTO pytype (id, object, module, name) VALUES (?, ?, ?, ?)",
                (obj_type_id, obj_id, module_obj_id, obj.__name__))
        elif type(obj) in self.tracked_t_id_map:
            # ^ expected to be False > 99% of time
            self._handle_tracked_type(obj, obj_id)
        return obj_id

    def _handle_tracked_type(self, obj, obj_id):
        '''
        after creating the row in the object table, handle creating another
        row in the corresponding special talbe (module, pyframe, pycode, function)
        '''
        t_id_map = self.tracked_t_id_map[type(obj)]
        if id(obj) in t_id_map:
            return
        obj_t_id = t_id_map[id(obj)] = len(t_id_map)
        if type(obj) is types.ModuleType:
            self.execute(
                "INSERT INTO module (id, object, file, name) VALUES (?, ?, ?, ?)",
                (obj_t_id, obj_id, getattr(obj, "__file__", "(none)"), obj.__name__))
        elif type(obj) is types.FrameType:
            if obj.f_back:
                f_back_obj_id = self._ensure_db_id(obj.f_back)
            else:
                f_back_obj_id = None
            self.conn.execute(
                "INSERT INTO pyframe (id, object, f_back_obj_id, f_code_obj_id, f_lasti, f_lineno, trace)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    obj_t_id,
                    obj_id,
                    f_back_obj_id,
                    self._ensure_db_id(obj.f_code),
                    obj.f_lasti,
                    obj.f_lineno,
                    Callpoint.from_frame(obj).tb_frame_str(),
                )
            )
        elif type(obj) is types.CodeType:
            self.conn.execute(
                "INSERT INTO pycode (id, object, co_name) VALUES (?, ?, ?)",
                (obj_t_id, obj_id, obj.co_name))
        elif type(obj) is types.FunctionType:
            self.conn.execute(
                "INSERT INTO function (id, object, func_name, func_code_obj_id, module_obj_id)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    obj_t_id,
                    obj_id,
                    obj.func_name,
                    self._ensure_db_id(obj.func_code),
                    self._module_name2obj_id(obj.__module__)
                )
            )

    def _module_name2obj_id(self, name):
        if name is None:
            return None  # sometimes __module__ is None
        if name not in self.modules_map:
            self.modules_map[name] = types.ModuleType(name)
        return self._ensure_db_id(self.modules_map[name])

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
        elif t is types.FrameType:
            mode = "frame"
        elif t is types.FunctionType:
            mode = 'func'
        elif isinstance(obj, dict):
            mode = "dict"
        elif isinstance(obj, (list, tuple)):
            mode = "list"
        # STEP 2 - GET KEYS
        if mode == "dict":
            keys = obj.keys()
            key_dst += [('@{}'.format(self._ensure_db_id(key)), obj[key]) for key in keys]
        if mode == "list":
            key_dst += enumerate(obj)
        if mode == "object":
            if hasattr(obj, "__dict__"):
                key_dst += [('.' + key, dst) for key, dst in obj.__dict__.items()]
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
                    key_dst.append(('.' + key, getattr(obj, key)))
                except AttributeError:
                    pass  # just because a slot exists doesn't mean it has a value
        if mode == 'frame':  # expensive to handle, but pretty rare
            key_dst += [(".locals[{!r}]".format(key), val) for key, val in obj.f_locals.items()]
            key_dst.append((".f_globals", obj.f_globals))
        if mode == 'func':
            '''
            >>> a = 1
            >>> def b():
            ...    c = 2
            ...    def d():
            ...       e = 3
            ...       return a + c + 3
            ...    return d
            ...
            >>> b().func_code.co_freevars
            ('c',)
            >>> b().func_closure[0].cell_contents
            2
            '''
            if obj.func_closure:  # (maybe) grab function closure
                for varname, cell in zip(obj.func_code.co_freevars, obj.func_closure):
                    key_dst.append((varname, cell.cell_contents))
            args, varargs, keywords, defaults = inspect.getargspec(obj)
            if defaults:  # (maybe) grab function defaults
                for name, default in zip(reversed(args), reversed(defaults)):
                    key_dst.append((".defaults[{!r}]".format(name), default))
        self.conn.executemany(
            "INSERT INTO reference (src, dst, ref) VALUES (?, ?, ?)",
            [(db_id, self._ensure_db_id(dst), key) for key, dst in key_dst])
        return db_id

    def add_frames(self):
        '''
        add all of the current frames
        '''
        cur_frames = sys._current_frames()
        for thread_id, frame in cur_frames.items():
            self.conn.execute(
                "INSERT INTO thread (stack_obj_id, thread_id) VALUES (?, ?)",
                (self._ensure_db_id(frame), thread_id))

    def add_all(self):
        gc.collect()  # try to minimize garbage
        self.add_obj(type)
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

    return


#f_globals INTEGER, -- object (a dict instance)  OR should this be a ref type e.g. locals["foo"], globals["bar"]
#f_locals INTEGER, -- object (a dict instance)
