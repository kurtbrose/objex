import collections
import gc
import inspect
import os
try:
    import resource
except ImportError:  # windows
    resource = None
import reprlib
import shutil
import sys
from socket import getfqdn
import sqlite3
import time
import types
import weakref

from .schema import _SCHEMA
from .dbutils import _run_ddl


_DICT_PROXY_TYPE = type(type.__dict__)

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


def _gc_prep():
    '''
    turn off GC, set flags so they will populate gc.garbage,
    then run the collector
    returns the number of objects collected
    '''
    gc.disable()
    flags = gc.get_debug()
    gc.set_debug(gc.DEBUG_UNCOLLECTABLE)  # make gc.garbage useful
    num_collected = gc.collect()
    gc.set_debug(flags)
    gc.enable()
    return num_collected


def _format_frame_trace(frame):
    frameinfo = inspect.getframeinfo(frame)
    context = ''
    if frameinfo.code_context:
        context = frameinfo.code_context[0].strip()
    return '  File "{}", line {}, in {}\n    {}\n'.format(
        frameinfo.filename,
        frameinfo.lineno,
        frameinfo.function,
        context,
    )


def _finalize_wal(conn):
    # Keep WAL for bulk writes, then fold everything back into the main DB
    # so the final artifact is a portable single-file SQLite database.
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("PRAGMA journal_mode = DELETE")
    conn.commit()


class _Writer:
    '''
    responsible for dumping objects

    use_gc -- flag whether to call gc.get_referrers and gc.get_referrents
    on every object and record in addition to other information
    (default False because this is ~20x slower)

    NOTE: this object tends to get really huge; it is meant to be disposable
    (ideally, the whole process is disposable when the export process is run)
    '''
    _TRACKED_TYPES = (types.ModuleType, types.FrameType, types.FunctionType, types.CodeType)
    _COMMIT_INTERVAL_OBJECTS = 10000

    def __init__(self, conn, use_gc=False, values='none', value_limit=128, redact=True):
        if values not in ('none', 'builtins', 'repr'):
            raise ValueError("values must be one of 'none', 'builtins', 'repr'")
        self.conn = conn
        self.use_gc = use_gc
        self.values = values
        self.value_limit = value_limit
        self.redact = redact
        self._reprlib = reprlib.Repr()
        self._reprlib.maxstring = value_limit
        self._reprlib.maxother = value_limit
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
        self.type_is_type_map = {}  # map of whether a type is a type
        self.modules_map = dict(sys.modules)  # map of __module__ to fake modules when no entry in sys.modules
        # frame objects materialize lazily on modern CPython (3.11+): running
        # stacks only yield frame objects when requested (sys._current_frames(),
        # f_back, cr_frame, ...), so they can be absent from the all_objects
        # snapshot; queue them as they're encountered and traverse at the end
        self._pending_frames = []
        self.started = time.time()
        # ignore ids not just to avoid analysis noise, but because these can
        # get pretty big over time, don't want to waste DB space
        ignored = list(self.__dict__.values()) + list(self.tracked_t_id_map.values())
        self.ignore_ids = {id(e) for e in ignored}
        self.ignore_ids.add(id(self.ignore_ids))
        self.ignore_ids.add(id(self.__dict__))
        self.ignore_ids.add(id(self))
        self._objects_since_commit = 0
        # commented out tracing code
        # TODO how to put it in w/out hurting perf if off?
        # (maybe optional decorators?)
        # self.times = []

    @classmethod
    def write_to_path(cls, path, use_gc=False, use_wal=True, values='none', value_limit=128, redact=True):
        '''create a new instance that will dump state to path (which shouldn't exist)'''
        conn = sqlite3.connect(path)
        conn.text_factory = str
        try:
            if use_wal:
                conn.execute("PRAGMA journal_mode = WAL")
            _run_ddl(conn, _SCHEMA)
            memory = _get_memory_mb()
            num_collected = _gc_prep()
            conn.execute(
                """
                INSERT INTO meta (id, pid, hostname, memory_mb, gc_info, num_gcd_objects, capture_values)
                VALUES (0, ?, ?, ?, ?, ?, ?)
                """,
                (os.getpid(), getfqdn(), memory, '[{},{},{}]'.format(*gc.get_count()), num_collected, values))
            writer = cls(conn, use_gc=use_gc, values=values, value_limit=value_limit, redact=redact)
            writer.add_all()
            if writer.values != 'none' and writer.redact:
                writer.redact_sensitive_values()
            writer.finish()
        except Exception:
            conn.close()
            raise

    def execute(self, sql, params):
        # start = time.time()
        self.conn.execute(sql, params)
        # self.times.append(time.time() - start)

    def executemany(self, sql, params):
        # start = time.time()
        self.conn.executemany(sql, params)
        # total = time.time() - start
        # self.times.extend([total / len(params)] * len(params))

    def maybe_commit(self):
        if self._objects_since_commit >= self._COMMIT_INTERVAL_OBJECTS:
            self.conn.commit()
            self._objects_since_commit = 0

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
        obj_type = type(obj)
        type_obj_id = self._ensure_db_id(obj_type, is_type=True, refs=1)
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
        if self.values != 'none':
            self._maybe_capture_value(obj, obj_id)
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

    def _maybe_capture_value(self, obj, obj_id):
        '''
        capture a bounded repr of obj into object_repr, per self.values tier.

        tier 'builtins' only touches exact builtin scalar types (C-level
        repr, never runs user code); tier 'repr' additionally runs
        reprlib.repr on everything else, which EXECUTES user __repr__
        mid-dump -- accepted risk; fork isolation (spawn_dump) bounds the
        blast radius to dump quality.
        '''
        limit = self.value_limit
        obj_type = type(obj)
        truncated = False
        if obj_type in _VALUE_TYPES:
            if obj_type is str or obj_type is bytes:
                # slice BEFORE repr to bound cost on huge strings; repr adds
                # quotes/escapes, so bound the text itself too
                text = repr(obj[:limit])
                truncated = len(obj) > limit or len(text) > limit
                text = text[:limit]
            elif obj_type is int:
                try:
                    text = repr(obj)
                except ValueError:  # 3.11+ int_max_str_digits
                    text = '<int ~{} bits>'.format(obj.bit_length())
                truncated = len(text) > limit
                text = text[:limit]
            else:  # float, complex, bool
                text = repr(obj)
        elif self.values == 'repr':
            try:
                text = self._reprlib.repr(obj)
            except Exception:  # user __repr__ may raise; skip, never fail the dump
                return
            truncated = len(text) > limit
            text = text[:limit]
        else:
            return
        # lone surrogates break sqlite's UTF-8 encode
        text = text.encode('utf-8', 'backslashreplace').decode('utf-8')
        self.execute(
            "INSERT INTO object_repr (object, repr, truncated, redacted) VALUES (?, ?, ?, 0)",
            (obj_id, text, int(truncated)))

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
            module_file = getattr(obj, "__file__", None) or "(none)"
            self.execute(
                "INSERT INTO module (id, object, file, name) VALUES (?, ?, ?, ?)",
                (obj_t_id, obj_id, module_file, obj.__name__))
        elif type(obj) is types.FrameType:
            self._pending_frames.append(obj)
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
                    _format_frame_trace(obj),
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
                    obj.__name__,
                    self._ensure_db_id(obj.__code__),
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
        self._objects_since_commit += 1
        self.maybe_commit()
        key_dst = []
        check_dict, check_slots = False, False  # whether to scrape the __dict__ and __slots__
        extra_relationship = None  # which special built-in to scrape as (dict, list, etc)
        t = type(obj)
        # STEP 1 - FIGURE OUT WHICH MODE TO USE
        if t in _SPECIAL_TYPES:
            extra_relationship = t
        else:
            check_dict, check_slots = True, True
            # check for possible sub-classes
            # TODO: cache the sub-class info based on type-id, same as slots and type-is-type
            if isinstance(obj, BaseException):
                # C-level exception slots are invisible to __dict__/__slots__ scraping
                key_dst += [
                    ('.args', obj.args),
                    ('.__traceback__', obj.__traceback__),
                    ('.__cause__', obj.__cause__),
                    ('.__context__', obj.__context__),
                ]
                notes = getattr(obj, '__notes__', None)  # 3.11+, only when notes were added
                if notes is not None:
                    key_dst.append(('.__notes__', notes))
            elif isinstance(obj, tuple):  # namedtuple assume these will be most common
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
            # dict.items, NOT keys() + __getitem__: a key mutated after insertion
            # (e.g. openpyxl StyleArray, an array subclass with a content-based
            # hash) iterates fine but raises KeyError on lookup by key
            key_dst += [('@{}'.format(self._ensure_db_id(key, refs=2)), value)
                        for key, value in dict.items(obj)]
        elif extra_relationship in (list, tuple, collections.deque):
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
                (".f_builtins", obj.f_builtins),
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
            closure = getattr(obj, '__closure__', None)
            code = obj.__code__
            if closure:  # (maybe) grab function closure
                for varname, cell in zip(code.co_freevars, closure):
                    try:
                        cell_contents = cell.cell_contents
                    except ValueError:
                        continue
                    key_dst.append((".locals[{!r}]".format(varname), cell_contents))
                key_dst.append(('.__closure__', closure))
            positional_argcount = code.co_posonlyargcount + code.co_argcount
            positional_argnames = code.co_varnames[:positional_argcount]
            defaults = obj.__defaults__
            if defaults:  # (maybe) grab function defaults
                for name, default in zip(reversed(positional_argnames), reversed(defaults)):
                    key_dst.append((".defaults[{!r}]".format(name), default))
                key_dst.append(('.__defaults__', obj.__defaults__))
            if obj.__kwdefaults__ is not None:
                key_dst.append(('.__kwdefaults__', obj.__kwdefaults__))
            key_dst.append((".__code__", code))
            key_dst.append((".__globals__", obj.__globals__))
            key_dst.append((".__doc__", obj.__doc__))
            # __module__ is a special case b/c unlike other dst values, we don't
            # want to call _ensure_db_id on the module; so it gets its own insert
            module = self._module_name2obj_id(obj.__module__)
            if module:
                self.conn.execute(
                    "INSERT INTO reference (src, dst, ref) VALUES (?, ?, ?)",
                    (db_id, module, ".__module__"))
            check_dict = True
        elif extra_relationship is types.GeneratorType:
            key_dst.append(('.gi_code', obj.gi_code))
            key_dst.append(('.gi_frame', obj.gi_frame))
            key_dst.append(('.gi_yieldfrom', obj.gi_yieldfrom))
        elif extra_relationship is types.CoroutineType:
            key_dst.append(('.cr_code', obj.cr_code))
            key_dst.append(('.cr_frame', obj.cr_frame))
            key_dst.append(('.cr_await', obj.cr_await))
        elif extra_relationship is types.AsyncGeneratorType:
            key_dst.append(('.ag_code', obj.ag_code))
            key_dst.append(('.ag_frame', obj.ag_frame))
        elif extra_relationship is types.TracebackType:
            key_dst.append(('.tb_frame', obj.tb_frame))
            key_dst.append(('.tb_next', obj.tb_next))
        elif extra_relationship is types.CellType:
            try:
                key_dst.append(('.cell_contents', obj.cell_contents))
            except ValueError:
                pass  # empty cell
        elif extra_relationship is slice:
            key_dst += [
                ('.start', obj.start),
                ('.stop', obj.stop),
                ('.step', obj.step),
            ]
        elif extra_relationship is super:
            key_dst += [
                ('.__self__', obj.__self__),
                ('.__self_class__', obj.__self_class__),
                ('.__thisclass__', obj.__thisclass__),
            ]
        elif extra_relationship is weakref.ref:
            # only the callback -- the referent is a weak edge (gc tp_traverse agrees)
            key_dst.append(('.__callback__', obj.__callback__))
        elif extra_relationship is types.MethodType:
            key_dst += [
                ('.__func__', obj.__func__),
                ('.__self__', obj.__self__),
                ('.__doc__', obj.__doc__),
            ]
        elif extra_relationship is types.BuiltinMethodType:
            try:
                key_dst.append(('.__self__', obj.__self__))
            except AttributeError:
                pass
            key_dst.append(('.__doc__', obj.__doc__))
        elif extra_relationship in (classmethod, staticmethod):
            key_dst.append(('.__func__', obj.__func__))
            key_dst.append(('.__doc__', obj.__doc__))
        elif extra_relationship is property:
            key_dst += [
                ('.fget', obj.fget),
                ('.fset', obj.fset),
                ('.fdel', obj.fdel),
                ('.__doc__', obj.__doc__),
            ]
        elif extra_relationship is _DICT_PROXY_TYPE:
            key_dst.append(('.<proxied_dict>', gc.get_referents(obj)[0]))
        elif extra_relationship is types.ModuleType:
            key_dst.append(('.__doc__', obj.__doc__))
            check_dict = True
        elif extra_relationship is collections.defaultdict:
            keys = obj.keys()
            key_dst += [('@{}'.format(self._ensure_db_id(key, refs=2)), dict.__getitem__(obj, key))
                        for key in keys]
            key_dst.append(('.default_factory', obj.default_factory))
        if check_dict:
            try:
                __dict__ = object.__getattribute__(obj, "__dict__")
            except AttributeError:
                __dict__ = None
            if __dict__ is not None:
                key_dst += [('.' + key, dst) for key, dst in __dict__.items()]
                if type(__dict__) is _DICT_PROXY_TYPE:
                    key_dst.append(('.__dict__<proxy>', __dict__))
                    key_dst.append(('.__dict__', gc.get_referents(__dict__)[0]))
                else:
                    key_dst.append(('.__dict__', __dict__))
        if check_slots:
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
                self.type_is_type_map[id(type(obj))] = issubclass(type(obj), type)
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
            if self.type_is_type_map[id(type(obj))]:
                key_dst.append(('.__bases__', obj.__bases__))
                try:
                    key_dst.append(('.__slots__', obj.__slots__))
                except AttributeError:
                    pass
                try:
                    key_dst.append(('.__mro__', obj.__mro__))
                except AttributeError:
                    pass
                key_dst.append(('.__doc__', obj.__doc__))
        if t not in _GC_COMPLETE_TYPES:
            # tp_traverse ground truth for anything the named handlers missed
            # (C iterators, asyncio.Task, ContextVar, functools.partial, ...)
            covered = {id(dst) for _, dst in key_dst}
            covered.add(id(t))
            for referent in gc.get_referents(obj):
                if id(referent) not in covered:
                    key_dst.append(('<gc>', referent))
        self.conn.executemany(
            "INSERT INTO reference (src, dst, ref) VALUES (?, ?, ?)",
            [(db_id, self._ensure_db_id(dst, refs=2), key) for key, dst in key_dst])
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
        while self._pending_frames:
            self.add_obj(self._pending_frames.pop(), refs=2)
        if self.use_gc:
            for obj in self.all_objects:
                db_id = self._ensure_db_id(obj)
                for referrer in gc.get_referrers(obj):
                    if id(referrer) in self.ignore_ids:
                        continue
                    self.conn.execute(
                        "INSERT INTO gc_referrer (src, dst) VALUES (?, ?)",
                        (self._ensure_db_id(referrer, refs=1), db_id))
                for referent in gc.get_referents(obj):
                    if id(referent) in self.ignore_ids:
                        continue
                    self.conn.execute(
                        "INSERT INTO gc_referent (src, dst) VALUES (?, ?)",
                        (db_id, self._ensure_db_id(referent, refs=1)))
        self.ignore_ids.remove(id(sys._getframe()))

    def redact_sensitive_values(self):
        '''
        replace object_repr rows for values sitting under sensitive-looking
        edge labels with a length-bucketed placeholder. key objects (the
        string 'password') stay readable -- the key is the clue, the value
        is the secret. an object aliased under both a sensitive and an
        innocent label is redacted: one sensitive inbound edge wins.
        '''
        sensitive_dsts = set()
        key_dsts = []  # (key_obj_id, dst) pairs from dict-key edges
        for dst, ref in self.conn.execute("SELECT dst, ref FROM reference"):
            if not isinstance(ref, str):  # list-index edges from enumerate
                continue
            if ref.startswith('@'):  # dict-key edge, format '@<key_obj_id>'
                key_dsts.append((int(ref[1:]), dst))
                continue
            ref_lower = ref.lower()
            if any(p in ref_lower for p in _SENSITIVE_KEY_PATTERNS):
                sensitive_dsts.add(dst)
        # dict keys: a key whose captured repr text matches the patterns
        # marks its dst sensitive; keys with no captured value can't match
        key_text = {}
        key_ids = list({key_obj_id for key_obj_id, _ in key_dsts})
        for i in range(0, len(key_ids), 800):
            chunk = key_ids[i:i + 800]
            qs = ', '.join(['?'] * len(chunk))
            for obj, text in self.conn.execute(
                    "SELECT object, repr FROM object_repr WHERE object IN ({})".format(qs), chunk):
                key_text[obj] = text.lower()
        sensitive_key_obj_ids = set()
        for key_obj_id, dst in key_dsts:
            text = key_text.get(key_obj_id)
            if text is not None and any(p in text for p in _SENSITIVE_KEY_PATTERNS):
                sensitive_dsts.add(dst)
                sensitive_key_obj_ids.add(key_obj_id)
        # keys themselves stay readable (even when aliased as a value
        # elsewhere, e.g. CPython 3.14 interned-string self-mapping dicts)
        sensitive_dsts -= sensitive_key_obj_ids
        # replace captured rows; length basis is object.len when present
        # (true pre-truncation length for str/bytes), else the repr length
        updates = []
        dst_ids = list(sensitive_dsts)
        for i in range(0, len(dst_ids), 800):
            chunk = dst_ids[i:i + 800]
            qs = ', '.join(['?'] * len(chunk))
            for obj, text, length in self.conn.execute(
                    """
                    SELECT object_repr.object, object_repr.repr, object.len
                    FROM object_repr JOIN object ON object_repr.object = object.id
                    WHERE object_repr.object IN ({})
                    """.format(qs), chunk):
                n = length if length is not None else len(text)
                updates.append(('<redacted len<={}>'.format(_len_bucket(n)), obj))
        if updates:
            self.executemany(
                "UPDATE object_repr SET repr = ?, truncated = 0, redacted = 1 WHERE object = ?",
                updates)
        self.conn.commit()

    def finish(self):
        self.conn.execute(
            "UPDATE meta SET duration_s = ?",
            (time.time() - self.started,))
        _finalize_wal(self.conn)
        self.conn.close()


# special types that have special-handling code for discovering contents
_SPECIAL_TYPES = set([
    dict, list, tuple, set, frozenset, types.FrameType, types.FunctionType,
    types.GeneratorType, types.CoroutineType, types.AsyncGeneratorType,
    types.TracebackType, types.CellType, slice, super, weakref.ref,
    types.MethodType,
    _DICT_PROXY_TYPE, classmethod, staticmethod, property,
    types.BuiltinFunctionType, types.BuiltinMethodType,
    types.ModuleType, collections.deque, collections.defaultdict])

# exact builtin containers whose named traversal is definitionally complete;
# exempting them keeps the hot path from building a second id-set
_GC_COMPLETE_TYPES = frozenset([
    dict, list, tuple, set, frozenset, collections.deque, collections.defaultdict])

# exact types only: C-level repr, never runs user code (subclasses excluded)
_VALUE_TYPES = (str, bytes, int, float, complex, bool)

_SENSITIVE_KEY_PATTERNS = (
    # merged from ffcore.redact_sensitive_data defaults and Sentry
    # server-side scrubbing defaults
    'key', 'secret', 'passphrase', 'password', 'passwd', 'pwd',
    'token', 'auth', 'credential', 'session', 'private',
)


def _len_bucket(n):
    '''smallest bucket >= n from the series 100, 500, 1000, 5000, 10000, ...'''
    bucket, step_five = 100, True
    while bucket < n:
        bucket *= 5 if step_five else 2
        step_five = not step_five
    return bucket


def dump_graph(path, print_info=False, use_gc=False, values='none', value_limit=128, redact=True):
    '''
    dump a collection db to path;
    the collection db is designed to be small
    and write fast, so it needs post-processing
    to e.g. add indices and compute values
    before analysis

    values -- value-capture tier: 'none' (default; no object values stored),
    'builtins' (bounded reprs of exact builtin scalars: str, bytes, int,
    float, complex, bool), or 'repr' (builtins plus reprlib.repr of
    everything else; runs user __repr__ mid-dump -- prefer spawn_dump for
    fork isolation)
    value_limit -- max stored repr length per object (default 128)
    redact -- when capturing values, replace values under sensitive-looking
    keys ('secret', 'token', ...) with a length-bucketed placeholder
    (default True)
    '''
    start = time.time()
    _Writer.write_to_path(path, use_gc=use_gc, values=values,
                          value_limit=value_limit, redact=redact)
    if print_info:
        duration = time.time() - start
        memory = _get_memory_mb()
        dumpsize = os.stat(path).st_size / 1024.0 / 1024  # MiB
        objects = len(gc.get_objects())
        print("process memory usage: {:0.3f}MiB".format(memory))
        print("total gc objects:", objects)
        # print "wrote {} rows in {}".format(
        #     len(grapher.times), duration)
        # print "db perf: {:0.3f}us/row".format(
        #     1e6 * sum(grapher.times) / len(grapher.times))
        # print "overall perf: {:0.3f} s/GiB, {:0.03f} ms/object".format(
        #     1024 * duration / memory, 1000 * duration / objects)
        # print "duration - db time:", duration - sum(grapher.times)
        print("duration: {0:0.1f}".format(duration))
        print("compression - {:0.02f}MiB -> {:0.02f}MiB ({:0.01f}%)".format(
            memory, dumpsize, 100 * (1 - dumpsize / memory)))

    return


def spawn_dump(path, print_info=False, use_gc=False, values='none', value_limit=128, redact=True):
    if not hasattr(os, 'fork'):
        raise NotImplementedError('spawn_dump() requires os.fork() support')

    pid = os.fork()
    if pid:
        return pid

    try:
        dump_graph(path, print_info=print_info, use_gc=use_gc,
                   values=values, value_limit=value_limit, redact=redact)
    except BaseException:
        os._exit(1)
    os._exit(0)


def wait_dump(pid):
    waited_pid, status = os.waitpid(pid, 0)
    if waited_pid != pid:
        raise RuntimeError('waited on unexpected pid: {}'.format(waited_pid))
    if os.WIFEXITED(status):
        exit_code = os.WEXITSTATUS(status)
        if exit_code:
            raise RuntimeError('objex dump process {} exited with status {}'.format(pid, exit_code))
        return exit_code
    if os.WIFSIGNALED(status):
        raise RuntimeError('objex dump process {} terminated by signal {}'.format(pid, os.WTERMSIG(status)))
    raise RuntimeError('objex dump process {} ended unexpectedly'.format(pid))


#f_globals INTEGER, -- object (a dict instance)  OR should this be a ref type e.g. locals["foo"], globals["bar"]
#f_locals INTEGER, -- object (a dict instance)
