
from __future__ import print_function

import os
from cmd import Cmd
import pprint
import random
import shutil
import sqlite3
try:
    import colorama
except ImportError:
    pass
else:
    colorama.init()
try:
    from termcolor import colored
except ImportError:
    colored = lambda s, color: s


from .schema import _INDICES
from .dbutils import _run_ddl


def _add_class_references(conn):
    '''
    ensure there is a __class__ pointing from instance to class
    '''
    conn.execute("""
        INSERT INTO reference (src, dst, ref)
        SELECT id, pytype, '__class__' FROM object
        WHERE NOT EXISTS (
            SELECT 1 FROM REFERENCE WHERE
            src = object.id AND
            dst = object.pytype AND
            ref = '__class__'
        )
    """)


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
    _add_class_references(conn)


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

    def sql_list(self, sql, args=None):
        '''run SELECT and return [a, b, c] instead of [(a,), (b,), (c,)]'''
        return list(sum(self.sql(sql, args), ()))

    def object_count(self):
        return self.sql_val('SELECT count(*) FROM object')

    def reference_count(self):
        return self.sql_val('SELECT count(*) FROM reference')

    def visible_memory_fraction(self):
        '''get the fraction of peak RSS that is accounted for'''
        return self.sql_val(
            'SELECT 1.0 * sum(size) / 1024 / 1024 / (SELECT memory_mb from meta) FROM object, meta')

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
            'SELECT name FROM pytype where object = ('
                'SELECT pytype FROM object WHERE id = ?)',
            (obj_id,))

    def obj_size(self, obj_id):
        return self.sql_val('SELECT size FROM object WHERE id = ?', (obj_id,))

    def obj_refcount(self, obj_id):
        return self.sql_val('SELECT refcount FROM object WHERE id = ?', (obj_id,))

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

    def obj_is_func(self, obj_id):
        return self.sql_val('SELECT EXISTS(SELECT 1 FROM function WHERE object = ?)', (obj_id,))

    def obj_is_module(self, obj_id):
        return self.sql_val('SELECT EXISTS(SELECT 1 FROM module WHERE object = ?)', (obj_id,))

    def obj_is_frame(self, obj_id):
        return self.sql_val('SELECT EXISTS(SELECT 1 FROM pyframe WHERE object = ?)', (obj_id,))

    def typename(self, obj_id):
        '''name of an object that IS a type'''
        return self.sql_val('SELECT name FROM pytype WHERE object = ?', (obj_id,))

    def modulename(self, obj_id):
        return self.sql_val('SELECT name FROM module WHERE object = ?', (obj_id,))

    def funcname(self, obj_id):
        return self.sql_val('SELECT func_name FROM function WHERE object = ?', (obj_id,))

    def obj_instances(self, obj_id, limit=20):
        return self.sql_list(
            'SELECT id FROM object WHERE object.pytype = ('
                'SELECT id FROM pytype WHERE pytype.object = ?) LIMIT ?',
            (obj_id, limit))

    def obj_instance_count(self, obj_id):
        return self.sql_val(
            'SELECT count(*) FROM object WHERE object.pytype = ('
            'SELECT id FROM pytype WHERE pytype.object= ?)',
            (obj_id,))

    def instances_by_typename(self, typename):
        return self.sql(
            'SELECT id FROM object WHERE pytype = (SELECT id FROM pytype WHERE name = ?)',
            (typename,))

    def get_modules(self):
        '''
        return {name: obj-id} for all modules
        '''
        return dict(self.sql('SELECT name, object FROM module'))

    def get_threads(self):
        '''
        reconstructs the active threads, returns {thread_id: [trace]}
        '''
        thread_stack_map = {}
        thread_frames = self.sql(
            'SELECT thread_id, (SELECT id FROM pyframe WHERE object = stack_obj_id) FROM thread')
        for thread_id, frame_id in thread_frames:
            frame_ids = []
            while frame_id:
                frame_ids.append(frame_id)
                frame_id = self.sql_val(
                    'SELECT id FROM pyframe WHERE object = ('
                        'SELECT f_back_obj_id FROM pyframe WHERE id = ?)',
                    (frame_id,))
            frame_ids.reverse()
            thread_stack_map[thread_id] = [
                self.sql_val('SELECT trace FROM pyframe WHERE id = ?', (frame_id,))
                for frame_id in frame_ids]
        return thread_frames

    def _find_paths_from_any(self, src_obj_ids, dst_obj_id, limit=20):
        '''
        find the shortest path between any src and dst a variant of A*
        algorithm over the directed graph of references

        https://en.wikipedia.org/wiki/A*_search_algorithm

        returns several minimal paths in a list:
        [ [src_obj_id, obj_id, .... , dst_obj_id], ... ]

        (this algorithm is guaranteed to return a minimum length path,
        but may not return all minimum length paths)
        '''
        # {obj_id: parent} "towards" the sources
        for src_obj_id in src_obj_ids:
            assert type(src_obj_id) in (int, long), ("not an object id", src_obj_id)
        src_parent = {obj_id: None for obj_id in src_obj_ids}
        src_fringe = set(src_parent)  # "fringe" meaning the "surface", nodes that touch exterior nodes
        # {obj_id: child} "towards" the destination
        dst_child = {dst_obj_id: None}
        dst_fringe = set(dst_child)
        dst_depth = src_depth = 0
        while not src_fringe & dst_fringe:
            if dst_depth + src_depth > limit:
                # print("depth", dst_depth, src_depth)
                return []  # depth limit exceeded
            if not dst_fringe or not src_fringe:
                # print("deadend", not(dst_fringe), dst_depth, not(src_fringe), src_depth, list(dst_child)[:10])
                return []  # dead end without match
            if len(dst_fringe) < len(src_fringe):
                dst_depth += 1
                nxt_dst_fringe = set()
                for obj_id in dst_fringe:
                    parent_ids = self.sql_list(
                        "SELECT src FROM reference WHERE dst = ? OR ref = '@' || ?", (obj_id, str(obj_id)))
                    for parent_id in parent_ids:
                        if parent_id in dst_child:
                            continue  # already found it earlier
                        nxt_dst_fringe.add(parent_id)
                        dst_child[parent_id] = obj_id
                dst_fringe = nxt_dst_fringe
            else:
                src_depth += 1
                nxt_src_fringe = set()
                for obj_id in src_fringe:
                    child_ids = self.sql_list(
                        'SELECT dst FROM reference WHERE src = ?', (obj_id,))
                    for child_id in child_ids:
                        if child_id in src_parent:
                            continue  # already found it
                        nxt_src_fringe.add(child_id)
                        src_parent[child_id] = obj_id
                src_fringe = nxt_src_fringe
        # build out in both directions from each connection
        contact_points = src_fringe & dst_fringe
        paths = []
        for obj_id in contact_points:
            path = []
            cur = obj_id
            while cur is not None:
                path.append(cur)
                cur = src_parent[cur]
            path.reverse()
            cur = path.pop()  # avoid doubling-up
            while cur is not None:
                path.append(cur)
                cur = dst_child[cur]
            paths.append(path)
        return paths

    def _find_obj_ref_paths_from_any(self, src_obj_ids, dst_obj_id, limit=20):
        paths = self._find_paths_from_any(src_obj_ids, dst_obj_id, limit)
        obj_ref_paths = []
        for path in paths:
            obj_ref_path = []
            for i in range(len(path) - 1):
                ref = self.sql_val(
                    'SELECT ref FROM reference WHERE src = ? and dst = ?',
                    (path[i], path[i + 1]))
                obj_ref_path.append((path[i], ref))
            obj_ref_paths.append(obj_ref_path)
        return obj_ref_paths

    def find_path_to_module(self, obj_id):
        '''
        find how (if at all) this object is referenced from a module-global context
        returns [[(obj-id, ref), (obj-id, ref), ...], ...]
        where the first obj-id is a module
        (obj_id itself is not included in the result)
        '''
        return self._find_obj_ref_paths_from_any(self.get_modules().values(), obj_id)

    def find_path_to_frame(self, obj_id):
        '''
        find how (if at all) this object is referenced from a stack-frame
        return [[(obj-id, ref), (obj-id, ref), ...], ..]
        (same as find_path_to_module)
        '''
        return self._find_obj_ref_paths_from_any(self.sql_list('SELECT object FROM pyframe'), obj_id)

    def get_orphan_ids(self):
        '''
        return a list of the object ids that are not dst of any references
        '''
        return self.sql_list(
            "SELECT id FROM object WHERE id NOT IN (SELECT dst FROM reference) AND NOT EXISTS ("
            "SELECT 1 FROM reference WHERE ref = '@' || CAST(object.id AS TEXT) )")

    def get_orphan_count(self):
        return self.sql_val('SELECT count(*) FROM object WHERE id NOT IN (SELECT dst FROM reference)')

    def get_orphan_type_count(self):
        return dict(self.sql(
            """
            SELECT name, count(object.id) FROM object JOIN pytype ON object.pytype = pytype.object
            WHERE object.id NOT IN (SELECT dst FROM reference)
                  AND NOT EXISTS (SELECT 1 FROM reference WHERE ref = '@' || CAST(object.id AS TEXT))
            GROUP BY name
            """))


class Console(Cmd):
    prompt = 'objex> '

    doc_leader = '''\nobjex memory explorer v0.9.0\n'''

    def __init__(self, reader, start=None):
        self.reader = reader
        self.history = [start or 0]
        self.history_idx = 0

        self.cmd_history = []

        Cmd.__init__(self)  # old style class :'(

    @property
    def cur(self):
        return self.history[self.history_idx]

    def precmd(self, line):
        if not self.cmd_history:
            return line
        try:
            shortcut_idx = int(line) - 1
        except (ValueError, TypeError):
            return line

        for cmd_hist in reversed(self.cmd_history):
            options = cmd_hist.get('options', [])
            if options:
                break
        try:
            ret = options[shortcut_idx]
        except (KeyError, IndexError):
            print('expected a valid command or options 1 - %s, not %s, try again.'
                  % (len(options), shortcut_idx + 1))
            ret = None
        return ret

    # Cmd customizations/overrides
    def onecmd(self, line):
        if line is None:
            return
        orig_line = line
        cmd, args, line = self.parseline(line)
        if cmd == 'exit' and not orig_line.strip() == 'exit':
            print('type "exit" to quit the console')
            return

        if os.getenv('OBJEX_DEBUG', '') and cmd not in self.completenames(''):
            command, arg, line = Cmd.parseline(self, line)
            reader_func = getattr(self.reader, command, None)
            if reader_func:
                args = []
                for a in arg.split():
                    try:
                        args.append(int(a))
                    except ValueError:
                        args.append(a)
                try:
                    res = reader_func(*args)
                    pprint.pprint(res)
                except Exception:
                    import traceback; traceback.print_exc()
                print()
                return

        if line and line != 'EOF' and cmd and self.completenames(cmd):
            self.cmd_history.append({'line': line, 'cmd': cmd, 'args': args, 'options': []})
        try:
            return Cmd.onecmd(self, line)
        except Exception:
            # TODO: better exception handling can go here, maybe pdb with OBJEX_DEBUG=True
            self.cmd_history.pop()
            raise

    def parseline(self, line):
        # keep this stateless
        command, arg, line = Cmd.parseline(self, line)
        if arg is not None:
            arg = arg.split()
        # we do this bc self.complete() is stateful
        commands = [c for c in sorted(self.completenames(''), key=len)
                    if command and c.startswith(command)]
        if commands:
            command = commands[0]

        return command, arg, line

    def postcmd(self, stop, line):
        # print()  # TODO: better place to put this?
        return stop

    def cmdloop(self, *a, **kw):
        while 1:
            try:
                return Cmd.cmdloop(self, *a, **kw)
            except KeyboardInterrupt:
                print('^C')
                continue
        return

    def do_EOF(self, line):
        "type Ctrl-D to exit"
        print()
        return True

    def do_exit(self, line):
        "exits the objex console"
        print()
        return True

    def _obj_label(self, obj_id):
        if self.reader.obj_is_type(obj_id):
            return colored("<type {}#{}>".format(
                self.reader.typename(obj_id), obj_id), 'green')
        if self.reader.obj_is_module(obj_id):
            return "<module {}#{}>".format(
                self.reader.modulename(obj_id), obj_id)
        if self.reader.obj_is_func(obj_id):
            return "<function {}#{}>".format(
                self.reader.funcname(obj_id), obj_id)
        return colored("<{}#{}>".format(
            self.reader.obj_typename(obj_id), obj_id), 'red')

    def _ref(self, ref):
        '''translate ref for display'''
        if ref[0].isdigit():
            return "[{}]".format(ref)
        if ref[0] == '@':
            return "[" + self._obj_label(int(ref[1:])) + "]"
        return ref

    def _info_str(self, obj_id):
        if self.reader.obj_is_type(obj_id):
            return "{label} (instances={num_instances:,})".format(
                label=self._obj_label(obj_id),
                obj_id=obj_id,
                num_instances=self.reader.obj_instance_count(obj_id),
            )
        obj_len = self.reader.obj_len(obj_id)
        if obj_len is None:
            return '{label} (size={size}, refcount={refcount})'.format(
                label=self._obj_label(obj_id),
                refcount=self.reader.obj_refcount(obj_id),
                size=self.reader.obj_size(obj_id))

        return '{label} (size={size}, refcount={refcount}, len={len})'.format(
            label=self._obj_label(obj_id),
            refcount=self.reader.obj_refcount(obj_id),
            size=self.reader.obj_size(obj_id),
            len=obj_len)

    def _print_option(self, shortcut, option):
        cur_options = self.cmd_history[-1]['options']

        cur_options.append(shortcut)
        res = ' %s - %s' % (str(len(cur_options)).rjust(2), option)
        print(res)
        return res

    def do_in(self, args):
        "View inbound references of the current object"
        res = []
        in_ref = self.reader.refers_to_obj(self.cur)
        if args:
            return res  # TODO (go to a specific one)

        label = self._obj_label(self.cur)
        print("{:,} objects refer to {}:".format(self.reader.refers_to_obj_count(self.cur),
                                                 label))

        for ref, src in in_ref:
            self._print_option('go %s' % src, ' {}{}'.format(self._obj_label(src), self._ref(ref)))

        print()
        if self.reader.obj_is_type(self.cur):
            print('{:,} instances of {}:'.format(self.reader.obj_instance_count(self.cur),
                                                 label))

            for inst in self.reader.obj_instances(self.cur):
                self._print_option('go %s' % inst, ' {}'.format(self._info_str(inst)))

        if not self.reader.obj_is_module(self.cur):
            print()
            module_ref_paths = self.reader.find_path_to_module(self.cur)
            if module_ref_paths:
                print('%s modules transitively refer to %s:'
                      % (len(module_ref_paths), label))
                for ref_path in module_ref_paths:
                    cur_text = ''.join([self._obj_label(obj_id) + self._ref(ref)
                                        for obj_id, ref in ref_path])
                    self._print_option('go %s' % ref_path[0][0], cur_text)

        if not self.reader.obj_is_frame(self.cur):
            print()
            frame_ref_paths = self.reader.find_path_to_frame(self.cur)
            if frame_ref_paths:
                print('%s frames transitively refer to %s:'
                      % (len(frame_ref_paths), label))
                for ref_path in frame_ref_paths:
                    cur_text = ''.join([self._obj_label(obj_id) + self._ref(ref)
                                        for obj_id, ref in ref_path])
                    self._print_option('go %s' % ref_path[0][0], cur_text)

        print()
        return

    def do_out(self, args):
        "View outbound references of the current object"
        res = []
        out_ref = self.reader.obj_refers_to(self.cur)
        if args:
            return res  # TODO (go to a specific one)

        label = self._obj_label(self.cur)
        print("{:,} references to {}:".format(self.reader.obj_refers_to_count(self.cur),
                                              label))

        for ref, dst in out_ref:
            option_text = ' {}: {}'.format(ref, self._info_str(dst))
            self._print_option('go %s' % dst, option_text)

        print()
        return

    def _to_id(self, obj_id):
        if obj_id == 'random':
            return random.randrange(self.reader.object_count())

        try:
            ret = int(obj_id)
        except ValueError:
            print('expected valid integer or "random" for object id, not: %r' % obj_id)
            ret = None
        return ret

    def do_list(self, args=None):
        if not args:
            target = self.cur
        else:
            target = self._to_id(args[0])
            if target is None:
                return
        if target == self.cur:
            prefix = 'Now at:'
        else:
            prefix = 'Listing:'
        print('')

        try:
            print(prefix, self._info_str(target))
        except IndexError:
            print('no object with id: %r' % target)
            return

        return

    def do_go(self, args):
        if len(args) != 1:
            print('go command expects one argument')
            return
        target = args[0]
        target = self._to_id(target)
        if target is None:
            return
        self.history_idx = len(self.history)
        self.history = self.history[:self.history_idx] + [target]

        self.do_list()

    def do_back(self, args):
        if self.history_idx == 0:
            print('already at earliest point in history')
            return
        self.history_idx -= 1
        self.do_list()

    def do_forward(self, args):
        if self.history_idx == (len(self.history) - 1):
            print('already at latest point in history')
            return
        self.history_idx += 1
        self.do_list()

    def run(self):
        print("WELCOME TO OBJEX EXPLORER")
        print('Now exploring "{}" collected from {} at {}'.format(
            self.reader.path,
            self.reader.sql_val('SELECT hostname FROM meta'),
            self.reader.sql_val('SELECT ts FROM meta'),
        ))
        print("RSS memory was {:.2f}MiB; {:0.01f}MiB ({:0.01f}%) found in {:,} python objects".format(
            self.reader.sql_val('SELECT memory_mb FROM meta'),
            self.reader.sql_val('SELECT SUM(size) FROM object') / 1024 / 1024,
            self.reader.visible_memory_fraction() * 100,
            self.reader.object_count(),
        ))
        print('(Type "help" for options.)')
        print()
        self.do_list()
        return self.cmdloop()


def mro(t, get_bases=None):
    """Compute the class precedence list (mro) according to C3

    lightly modified from https://www.python.org/download/releases/2.3/mro/
    """
    if get_bases is None:
        get_bases = lambda t: t.__bases__

    start = [[t]] + [mro(bt) for bt in t.__bases__] + [list(t.__bases__)]

    def _merge_mro(seqs):
        i=0
        res = []
        while 1:
            nonemptyseqs = [seq for seq in seqs if seq]
            if not nonemptyseqs:
                 return res
            i += 1
            for seq in nonemptyseqs:  # find merge candidates among seq heads
                cand = seq[0]
                nothead=[s for s in nonemptyseqs if cand in s[1:]]
                if nothead:
                    cand = None  # reject candidate
                else:
                    break
            if not cand:
                raise ValueError("Inconsistent hierarchy")
            res.append(cand)
            for seq in nonemptyseqs: # remove cand
                if seq[0] == cand:
                    del seq[0]
        raise ValueError('could not produce valid mro merge')

    return _merge_mro(start)
