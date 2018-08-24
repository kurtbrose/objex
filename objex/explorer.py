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

try:
    import colorama
except ImportError:
    pass
else:
    colorama.init()

from .schema import _INDICES
from .dbutils import _run_ddl

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
            'SELECT name FROM pytype where object = ('
                'SELECT pytype FROM object WHERE id = ?)',
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

    def obj_is_func(self, obj_id):
        return self.sql_val('SELECT EXISTS(SELECT 1 FROM function WHERE object = ?)', (obj_id,))

    def obj_is_module(self, obj_id):
        return self.sql_val('SELECT EXISTS(SELECT 1 FROM module WHERE object = ?)', (obj_id,))

    def obj_is_type(self, obj_id):
        return self.sql_val('SELECT EXISTS(SELECT 1 FROM pytype WHERE object = ?)', (obj_id,))

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

    def get_threads(self):
        '''
        reconstructs the active threads, returns {thread_id: [trace]}
        '''
        thread_frames = {}
        thread_frames = self.sql(
            'SELECT thread_id, (SELECT id FROM pyframe WHERE object = stack_obj_id) FROM thread')
        for thread_id, frame_id in thread_frames:
            frames_ids = []
            while frame_id:
                frame_ids.append(frame_id)
                frame_id = self.sql_val(
                    'SELECT id FROM pyframe WHERE object = ('
                        'SELECT f_back_obj_id FROM pyframe WHERE id = ?)',
                    (frame_id,))
            frame_ids.reverse()
            thread_frames[thread_id] = [
                self.sql_val('SELECT trace FROM pyframe WHERE id = ?', (frame_id,))
                for frame_id in frame_ids]
        return thread_frames



class Console(object):
    def __init__(self, reader, obj_id=0):
        self.reader = reader
        self.obj_id = obj_id

    def _obj_label(self, obj_id):
        try:
            from termcolor import colored
        except ImportError:
            colored = lambda s, color: s

        if self.reader.obj_is_type(obj_id):
            return colored("<type {}@{}>".format(
                self.reader.typename(obj_id), obj_id), 'green')
        if self.reader.obj_is_module(obj_id):
            return "<module {}@{}>".format(
                self.reader.modulename(obj_id), obj_id)
        if self.reader.obj_is_func(obj_id):
            return "<function {}@{}>".format(
                self.reader.funcname(obj_id), obj_id)
        return colored("<{}@{}>".format(
            self.reader.obj_typename(obj_id), obj_id), 'red')

    def _info_str(self, obj_id):
        if self.reader.obj_is_type(obj_id):
            return "{label} (instances={num_instances:,})".format(
                label=self._obj_label(obj_id),
                obj_id=obj_id,
                num_instances=self.reader.obj_instance_count(obj_id),
            )
        return '''{label} (size={size}, len={len})'''.format(
            label=self._obj_label(obj_id),
            obj_id=obj_id,
            size=self.reader.obj_size(obj_id),
            len=self.reader.obj_len(obj_id))

    def _ref(self, ref):
        '''translate ref for display'''
        if ref[0].isdigit():
            return "[{}]".format(ref)
        if ref[0] == '@':
            return "[" + self._obj_label(int(ref[1:])) + "]"
        return ref

    def _menu(self):
        try:
            from termcolor import colored
        except ImportError:
            colored = lambda s, color: s

        label = self._obj_label(self.obj_id)
        refers_to_obj = []
        i = 0
        for ref, src in self.reader.refers_to_obj(self.obj_id):
            refers_to_obj.append('({}) - {}{}'.format(
                i, self._obj_label(src), self._ref(ref)))
            i += 1
        obj_refers_to = []
        for ref, dst in self.reader.obj_refers_to(self.obj_id):
            obj_refers_to.append('({}) - {}: {}'.format(
                i, ref, self._info_str(dst)))
            i += 1
        lines = [
            "CUR: {}".format(self._info_str(self.obj_id)),
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
                instances.append('({}) - {}'.format(i, self._info_str(inst)))
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
        print("WELCOME TO OBJEX EXPLORER")
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

        return
