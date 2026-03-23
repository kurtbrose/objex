import collections
import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import weakref
from pathlib import Path

from objex import Reader, dump_graph, make_analysis_db
from objex.explorer import InvalidDatabaseError
from objex.web import dispatch_request


class LegacyA:
    pass


class SlotsA:
    __slots__ = ('a', '__weakref__')


class SlotsB(SlotsA):
    __slots__ = ('b',)


class SlotsC(SlotsB):
    pass


class StaticAndClassMethods:
    @staticmethod
    def static_method():
        return 1

    @classmethod
    def class_method(cls):
        return cls.__name__


class NoneModule:
    pass


NoneModule.__module__ = None


def closing(a, b=1):
    c = 2

    def has_closure(d):
        return a + b + c + d

    return has_closure


class ObjexTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.stop_event = threading.Event()
        self.started = threading.Event()
        self._sample_objects = self._make_sample_objects()
        self.thread = threading.Thread(target=self._stacker, daemon=True)
        self.thread.start()
        self.assertTrue(self.started.wait(timeout=2))

    def tearDown(self):
        self.stop_event.set()
        self.thread.join(timeout=2)
        self.temp_dir.cleanup()

    def _make_sample_objects(self):
        legacy_a = LegacyA()

        slots_c = SlotsC()
        slots_c.a = 'slot-a'
        slots_c.b = 'slot-b'
        slots_c.c = 'normal-attr'

        deque_obj = collections.deque([1, 2, 3])
        defaultdict_obj = collections.defaultdict(int)
        defaultdict_obj[1] = 'cat'

        closure = closing(1)
        bound_method = StaticAndClassMethods().class_method
        generator = (item for item in range(2))

        weak_set = weakref.WeakSet()
        weak_set.add(slots_c)

        return {
            'legacy_a': legacy_a,
            'slots_c': slots_c,
            'deque_obj': deque_obj,
            'defaultdict_obj': defaultdict_obj,
            'closure': closure,
            'bound_method': bound_method,
            'generator': generator,
            'weak_set': weak_set,
        }

    def _stacker(self, n=25):
        if n:
            return self._stacker(n - 1)
        self.started.set()
        while not self.stop_event.wait(0.01):
            time.sleep(0.01)

    def test_dump_graph_and_make_analysis_db(self):
        base_path = Path(self.temp_dir.name)

        for use_gc in (False, True):
            with self.subTest(use_gc=use_gc):
                dump_path = base_path / ('objex-test-gc.db' if use_gc else 'objex-test.db')
                analysis_path = base_path / ('objex-test-gc-analysis.db' if use_gc else 'objex-test-analysis.db')

                dump_graph(str(dump_path), use_gc=use_gc)
                make_analysis_db(str(dump_path), str(analysis_path))

                with Reader(str(analysis_path)) as reader:
                    self.assertTrue(dump_path.exists())
                    self.assertTrue(analysis_path.exists())
                    self.assertGreater(reader.object_count(), 0)
                    self.assertGreater(reader.reference_count(), 0)
                    self.assertGreater(reader.visible_memory_fraction(), 0)
                    self.assertLessEqual(reader.visible_memory_fraction(), 1.5)

                    modules = reader.get_modules()
                    self.assertIn('builtins', modules)

                    legacy_type_ids = reader.find_type_by_name('LegacyA')
                    self.assertTrue(legacy_type_ids)
                    legacy_type_id = legacy_type_ids[0]
                    self.assertTrue(reader.typequalname(legacy_type_id).endswith('LegacyA'))
                    self.assertGreaterEqual(reader.obj_instance_count(legacy_type_id), 1)

                    instances = reader.random_instances(legacy_type_id, limit=10)
                    self.assertTrue(instances)
                    self.assertTrue(all(reader.obj_typename(obj_id) == 'LegacyA' for obj_id in instances))

                    self.assertGreaterEqual(reader.sql_val("SELECT COUNT(*) FROM pyframe"), 1)
                    self.assertGreaterEqual(reader.sql_val("SELECT COUNT(*) FROM thread"), 1)
                    self.assertGreaterEqual(
                        reader.sql_val("SELECT COUNT(*) FROM function WHERE func_name = ?", ('has_closure',)),
                        1,
                    )

                    default_factory_refs = reader.sql(
                        """
                        SELECT reference.ref, pytype.name
                        FROM reference
                        JOIN object ON reference.dst = object.id
                        JOIN pytype ON object.pytype = pytype.object
                        WHERE reference.ref = '.default_factory'
                        """
                    )
                    self.assertTrue(any(name == 'type' for _, name in default_factory_refs))

                conn = sqlite3.connect(str(analysis_path))
                try:
                    index_names = {
                        row[0] for row in conn.execute(
                            "SELECT name FROM sqlite_master WHERE type = 'index'"
                        )
                    }
                finally:
                    conn.close()
                self.assertIn('reference_src', index_names)

    def test_dump_graph_reconciles_wal_into_main_db(self):
        dump_path = Path(self.temp_dir.name) / 'portable.db'

        dump_graph(str(dump_path), use_gc=False)

        self.assertTrue(dump_path.exists())
        self.assertFalse(dump_path.with_name(dump_path.name + '-wal').exists())

        conn = sqlite3.connect(str(dump_path))
        try:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM meta').fetchone()[0], 1)
            self.assertGreater(conn.execute('SELECT COUNT(*) FROM object').fetchone()[0], 0)
        finally:
            conn.close()

    def test_module_help_does_not_start_console(self):
        result = subprocess.run(
            [sys.executable, '-m', 'objex', '--help'],
            capture_output=True,
            text=True,
            check=True,
            cwd=Path(__file__).resolve().parents[1],
        )

        self.assertIn('usage:', result.stdout)
        self.assertIn('make-analysis-db', result.stdout)
        self.assertIn('explore', result.stdout)
        self.assertNotIn('WELCOME TO OBJEX EXPLORER', result.stdout)

    def test_module_cli_parser_supports_legacy_explore_and_analysis_command(self):
        result = subprocess.run(
            [sys.executable, '-m', 'objex', 'make-analysis-db', '--help'],
            capture_output=True,
            text=True,
            check=True,
            cwd=Path(__file__).resolve().parents[1],
        )
        self.assertIn('collection_db', result.stdout)
        self.assertIn('analysis_db', result.stdout)

        result = subprocess.run(
            [sys.executable, '-m', 'objex', 'explore', '--help'],
            capture_output=True,
            text=True,
            check=True,
            cwd=Path(__file__).resolve().parents[1],
        )
        self.assertIn('analysis_db', result.stdout)

    def test_reader_rejects_invalid_objex_db(self):
        invalid_db = Path(self.temp_dir.name) / 'invalid.db'
        conn = sqlite3.connect(str(invalid_db))
        try:
            conn.execute('CREATE TABLE meta (id INTEGER PRIMARY KEY, hostname TEXT)')
            conn.commit()
        finally:
            conn.close()

        with self.assertRaises(InvalidDatabaseError):
            Reader(str(invalid_db))

    def test_web_api_serves_summary_and_object_views(self):
        base_path = Path(self.temp_dir.name)
        dump_path = base_path / 'web.db'
        analysis_path = base_path / 'web-analysis.db'
        dump_graph(str(dump_path), use_gc=False)
        make_analysis_db(str(dump_path), str(analysis_path))

        status_code, _, body = dispatch_request(str(analysis_path), '/api/summary')
        self.assertEqual(status_code, 200)
        summary = json.loads(body)
        self.assertIn('hostname', summary)
        self.assertGreater(summary['object_count'], 0)

        status_code, _, body = dispatch_request(str(analysis_path), '/api/random')
        self.assertEqual(status_code, 200)
        random_payload = json.loads(body)
        status_code, _, body = dispatch_request(
            str(analysis_path), '/api/object?id={}'.format(random_payload['id'])
        )
        self.assertEqual(status_code, 200)
        object_payload = json.loads(body)
        self.assertEqual(object_payload['id'], random_payload['id'])
        self.assertIn('label', object_payload)

        status_code, _, body = dispatch_request(
            str(analysis_path), '/api/referents?id={}&limit=5'.format(random_payload['id'])
        )
        self.assertEqual(status_code, 200)
        referents_payload = json.loads(body)
        self.assertIn('count', referents_payload)
        self.assertIn('items', referents_payload)
