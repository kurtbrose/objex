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

    use_gc -- flag whether to call gc.get_referrers and gc.get_referrents
    on every object and record in addition to other information
    (default False because this is ~20x slower)

    NOTE: this object tends to get really huge; it is meant to be disposable
    (ideally, the whole process is disposable when the export process is run)
    '''
    _TRACKED_TYPES = (types.ModuleType, types.FrameType, types.FunctionType, types.CodeType)

    def __init__(self, conn, use_gc=False):
        self.conn = conn
        self.use_gc = use_gc
        gc.collect()  # try to minimize garbage
        # track these to separate out "extra" objects that are generated as part of
        # the object walking process
        self.all_objects = gc.get_objects()
        self.all_object_ids = {id(e) for e in self.all_objects}
        # tracking objects by id gives two benefits:
        # 1- avoids calls to __eq__ which may execute arbitrary code
        # 2- avoids changing refcount on objects
        self.type_id_map = {}  # map of type ids to pytype rowids
        self.object_id_map = {}  # map of object ids to object rowids
        self.tracked_t_id_map = {t: {} for t in self._TRACKED_TYPES}
        self.type_slots_map = {}  # map of type ids to __slots__
        self.modules_map = dict(sys.modules)  # map of __module__ to fake modules when no entry in sys.modules
        self.started = time.time()
        # ignore ids not just to avoid analysis noise, but because these can
        # get pretty big over time, don't want to waste DB space
        self.ignore_ids = {id(e) for e in self.__dict__.values() + self.tracked_t_id_map.values()}
        self.ignore_ids.add(id(self.ignore_ids))
        self.ignore_ids.add(id(self.__dict__))
        self.ignore_ids.add(id(self))
        # commented out tracing code
        # TODO how to put it in w/out hurting perf if off?
        # (maybe optional decorators?)
        # self.times = []

    @classmethod
    def write_to_path(cls, path, use_gc=False, use_wal=True):
        '''create a new instance that will dump state to path (which shouldn't exist)'''
        conn = sqlite3.connect(path)
        if use_wal:
            conn.execute("PRAGMA journal_mode = WAL")
        _run_ddl(conn, _SCHEMA)
        memory = _get_memory_mb()
        conn.execute(
            "INSERT INTO meta (id, pid, hostname, memory_mb, gc_info) VALUES (0, ?, ?, ?, ?)",
            (os.getpid(), getfqdn(), memory, '[{},{},{}]'.format(*gc.get_count())))
        writer = cls(conn, use_gc=use_gc)
        writer.add_all()
        writer.finish()

    def execute(self, sql, params):
        # start = time.time()
        self.conn.execute(sql, params)
        # self.times.append(time.time() - start)

    def executemany(self, sql, params):
        # start = time.time()
        self.conn.executemany(sql, params)
        # total = time.time() - start
        # self.times.extend([total / len(params)] * len(params))

    def _ensure_db_id(self, obj, is_type=False, refs=0):
        '''
        DB id creation is separate from full object creation
        as a kind of "forward declaration" step to avoid loops
        (e.g. an object that refers to itself)

        refs is the number of "extra" references generated by the export process
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
        refcount = sys.getrefcount(obj) - (refs + 1)
        in_gc_objects = id(obj) in self.all_object_ids
        is_gc_tracked = in_gc_objects or gc.is_tracked(obj)
        self.execute(
            """
            INSERT INTO object (id, pytype, size, len, refcount, in_gc_objects, is_gc_tracked)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                obj_id,
                type_obj_id,
                sys.getsizeof(obj),
                length,
                refcount,
                in_gc_objects,
                is_gc_tracked)
            )
        # all of these are pretty rare (maybe optimize?)
        if id(obj) not in self.type_id_map and (is_type or isinstance(obj, type)):
            obj_type_id = self.type_id_map[id(obj)] = len(self.type_id_map)
            module_obj_id = self._module_name2obj_id(obj.__module__)
            self.execute(
                "INSERT INTO pytype (id, object, module, name) VALUES (?, ?, ?, ?)",
                (obj_type_id, obj_id, module_obj_id, obj.__name__))
            bases = getattr(obj, '__bases__', [])
            for base in bases:
                base_obj_id = self._ensure_db_id(base, is_type=True)
                self.execute("INSERT INTO pytype_bases (obj_id, base_obj_id) VALUES (?, ?)",
                             (obj_id, base_obj_id))
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
        if not isinstance(name, str):
            # TODO: sometimes None, sometimes random stuff (property?)
            # is there a better heuristic for this?
            return None
        if name not in self.modules_map:
            self.modules_map[name] = types.ModuleType(name)
        return self._ensure_db_id(self.modules_map[name], refs=2)

    def add_obj(self, obj, refs=0):
        '''
        add an object and references to this graph

        this funcion is idempotent; i.e. calling it 100x
        on the same object is the same as calling it once
        '''
        obj_id = id(obj)
        if obj_id in self.ignore_ids:
            return self.object_id_map.get(obj_id)
        refs = refs + 1  # take into account current frame
        self.ignore_ids.add(obj_id)
        db_id = self._ensure_db_id(obj, refs=refs)
        key_dst = []
        scrape_as_obj = False  # whether to scrape the __dict__ and __slots__
        extra_relationship = None  # which special built-in to scrape as (dict, list, etc)
        t = type(obj)
        # STEP 1 - FIGURE OUT WHICH MODE TO USE
        if t in _SPECIAL_TYPES:
            extra_relationship = t
        else:
            scrape_as_obj = True
            if isinstance(obj, tuple):  # namedtuple assume these will be most common
                extra_relationship = tuple
            elif isinstance(obj, dict):
                extra_relationship = dict
            elif isinstance(obj, list):
                extra_relationship = list
            elif isinstance(obj, set):
                extra_relationship = set
            elif isinstance(obj, frozenset):
                extra_relationship = frozenset
        # STEP 2 - GET KEYS
        if extra_relationship is dict:
            keys = obj.keys()
            key_dst += [('@{}'.format(self._ensure_db_id(key, refs=2)), dict.__getitem__(obj, key))
                        for key in keys]
        elif extra_relationship in (list, tuple):
            key_dst += enumerate(obj)
        elif extra_relationship in (set, frozenset):
            key_dst += zip(['*'] * len(obj), obj)
        elif extra_relationship is types.FrameType:  # expensive to handle, but pretty rare
            key_dst += [(".locals[{!r}]".format(key), val) for key, val in obj.f_locals.items()]
            key_dst += [(".f_globals[{!r}]".format(key), val) for key, val in obj.f_globals.items()]
            key_dst += [
                (".f_globals", obj.f_globals),
                (".f_back", obj.f_back),
                (".f_code", obj.f_code),
            ]
        elif extra_relationship is types.FunctionType:
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
                    key_dst.append((".locals[{!r}]".format(varname), cell.cell_contents))
            args, varargs, keywords, defaults = inspect.getargspec(obj)
            if defaults:  # (maybe) grab function defaults
                for name, default in zip(reversed(args), reversed(defaults)):
                    key_dst.append((".defaults[{!r}]".format(name), default))
            key_dst.append((".func_code", obj.func_code))
            key_dst.append((".func_globals", obj.func_globals))
            # __module__ is a special case b/c unlike other dst values, we don't
            # want to call _ensure_db_id on the module; so it gets its own insert
            module = self._module_name2obj_id(obj.__module__)
            if module:
                self.conn.execute(
                    "INSERT INTO reference (src, dst, ref) VALUES (?, ?, ?)",
                    (db_id, module, ".__module__"))
        elif extra_relationship is types.GeneratorType:
            key_dst.append(('.gi_code', obj.gi_code))
            key_dst.append(('.gi_frame', obj.gi_frame))
        elif extra_relationship in (types.MethodType, types.UnboundMethodType):
            key_dst += [
                ('im_class', obj.im_class),
                ('im_func', obj.im_func),
                ('im_self', obj.im_self)
            ]
        if scrape_as_obj:
            if hasattr(obj, "__dict__"):
                key_dst += [('.' + key, dst) for key, dst in obj.__dict__.items()]
                key_dst.append(('.__dict__', obj.__dict__))
            if id(type(obj)) not in self.type_slots_map:
                slot_names = set()
                try:
                    mro = type(obj).mro()
                except TypeError:
                    pass
                else:
                    for type_ in mro:
                        try:  # object.__getattribute__ to avoid any custom __getattr__ etc
                            slot_names.update(object.__getattribute__(type_, '__slots__'))
                        except AttributeError:
                            pass
                self.type_slots_map[id(type(obj))] = slot_names or ()
                # () is a singleton which creates less object noise than set()
            for key in self.type_slots_map[id(type(obj))]:
                if key in ('__dict__', '__weakref__'):
                    # see https://docs.python.org/3/reference/datamodel.html#slots
                    continue
                if key.startswith('__'):  # private slots name mangling
                    key = "_" + obj.__class__.__name__ + key
                try:
                    key_dst.append(('.' + key, object.__getattribute__(obj, key)))
                except AttributeError:
                    pass  # just because a slot exists doesn't mean it has a value
        self.conn.executemany(
            "INSERT INTO reference (src, dst, ref) VALUES (?, ?, ?)",
            [(db_id, self._ensure_db_id(dst, refs=2), key) for key, dst in key_dst])
        if self.use_gc:
            for referrer in gc.get_referrers(obj):
                self.conn.execute(
                    "INSERT INTO gc_referrer (src, dst) VALUES (?, ?)",
                    (self._ensure_db_id(referrer, refs=1), db_id))
            for referent in gc.get_referents(obj):
                self.conn.execute(
                    "INSERT INTO gc_referent (src, dst) VALUES (?, ?)",
                    (db_id, self._ensure_db_id(referent, refs=1)))
        return db_id

    def add_frames(self):
        '''
        add all of the current frames
        '''
        cur_frames = sys._current_frames()
        ignore_cur = sys._getframe()
        for thread_id, frame in cur_frames.items():
            if frame is ignore_cur:
                continue  # don't log the stack that is taking the snapshot
            self.conn.execute(
                "INSERT INTO thread (stack_obj_id, thread_id) VALUES (?, ?)",
                (self._ensure_db_id(frame, refs=2), thread_id))

    def add_all(self):
        # ignore this frame to avoid a bunch of spurious data
        self.ignore_ids.add(id(sys._getframe()))
        self.add_obj(type)
        self.add_frames()
        for obj in self.all_objects:
            self.add_obj(obj, refs=2)
        self.ignore_ids.remove(id(sys._getframe()))

    def finish(self):
        self.conn.execute(
            "UPDATE meta SET duration_s = ?",
            (time.time() - self.started,))
        self.conn.commit()
        self.conn.close()


# special types that have special-handling code for discovering contents
_SPECIAL_TYPES = set([
    dict, list, tuple, set, frozenset, types.FrameType, types.FunctionType,
    types.GeneratorType, types.MethodType, types.UnboundMethodType])


def dump_graph(path, print_info=False, use_gc=False):
    '''
    dump a collection db to path;
    the collection db is designed to be small
    and write fast, so it needs post-processing
    to e.g. add indices and compute values
    before analysis
    '''
    start = time.time()
    _Writer.write_to_path(path, use_gc=use_gc)
    if print_info:
        duration = time.time() - start
        memory = _get_memory_mb()
        dumpsize = os.stat(path).st_size / 1024.0 / 1024  # MiB
        objects = len(gc.get_objects())
        print "process memory usage: {:0.3f}MiB".format(memory)
        print "total gc objects:", objects
        # print "wrote {} rows in {}".format(
        #     len(grapher.times), duration)
        # print "db perf: {:0.3f}us/row".format(
        #     1e6 * sum(grapher.times) / len(grapher.times))
        # print "overall perf: {:0.3f} s/GiB, {:0.03f} ms/object".format(
        #     1024 * duration / memory, 1000 * duration / objects)
        # print "duration - db time:", duration - sum(grapher.times)
        print "duration: {0:0.1f}".format(duration)
        print "compression - {:0.02f}MiB -> {:0.02f}MiB ({:0.01f}%)".format(
            memory, dumpsize, 100 * (1 - dumpsize / memory))

    return


#f_globals INTEGER, -- object (a dict instance)  OR should this be a ref type e.g. locals["foo"], globals["bar"]
#f_locals INTEGER, -- object (a dict instance)
