import collections
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import weakref
from pathlib import Path
from unittest.mock import patch
from contextlib import redirect_stdout
from io import StringIO
import objex.__main__ as objex_main
from objex import Reader, dump_graph, make_analysis_db, spawn_dump, wait_dump, Console
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


class ExplodingGetattr:
    def __getattr__(self, name):
        if name == '__dict__':
            raise NameError("no mapped classes registered under the name '__dict__'")
        raise AttributeError(name)


class MisdirectingGetattribute:
    def __init__(self):
        self.value = 1
        self._pattern = re.compile('x')

    def __getattribute__(self, name):
        if name == '__dict__':
            return getattr(object.__getattribute__(self, '_pattern'), name)
        return object.__getattribute__(self, name)


def closing(a, b=1):
    c = 2

    def has_closure(d):
        return a + b + c + d

    return has_closure


def make_empty_closure():
    value = 'sentinel'

    def inner():
        return value

    del value
    return inner


class ObjexTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.stop_event = threading.Event()
        self.started = threading.Event()
        self._sample_objects = self._make_sample_objects()
        self.thread = threading.Thread(target=self._stacker, daemon=True)
        self.thread.start()
        assert self.started.wait(timeout=2)

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
        empty_closure = make_empty_closure()
        exploding_getattr = ExplodingGetattr()
        misdirecting_getattribute = MisdirectingGetattribute()
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
            'empty_closure': empty_closure,
            'exploding_getattr': exploding_getattr,
            'misdirecting_getattribute': misdirecting_getattribute,
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
                    assert dump_path.exists()
                    assert analysis_path.exists()
                    assert reader.object_count() > 0
                    assert reader.reference_count() > 0
                    assert reader.visible_memory_fraction() > 0
                    assert reader.visible_memory_fraction() <= 1.5

                    modules = reader.get_modules()
                    assert 'builtins' in modules

                    legacy_type_ids = reader.find_type_by_name('LegacyA')
                    assert legacy_type_ids
                    legacy_type_id = legacy_type_ids[0]
                    assert reader.typequalname(legacy_type_id).endswith('LegacyA')
                    assert reader.obj_instance_count(legacy_type_id) >= 1

                    instances = reader.random_instances(legacy_type_id, limit=10)
                    assert instances
                    assert all(reader.obj_typename(obj_id) == 'LegacyA' for obj_id in instances)

                    assert reader.sql_val("SELECT COUNT(*) FROM pyframe") >= 1
                    assert reader.sql_val("SELECT COUNT(*) FROM thread") >= 1
                    assert reader.sql_val(
                        "SELECT COUNT(*) FROM function WHERE func_name = ?",
                        ('has_closure',),
                    ) >= 1

                    default_factory_refs = reader.sql(
                        """
                        SELECT reference.ref, pytype.name
                        FROM reference
                        JOIN object ON reference.dst = object.id
                        JOIN pytype ON object.pytype = pytype.object
                        WHERE reference.ref = '.default_factory'
                        """
                    )
                    assert any(name == 'type' for _, name in default_factory_refs)

                conn = sqlite3.connect(str(analysis_path))
                try:
                    index_names = {
                        row[0] for row in conn.execute(
                            "SELECT name FROM sqlite_master WHERE type = 'index'"
                        )
                    }
                finally:
                    conn.close()
                assert 'reference_src' in index_names

    def test_dump_graph_reconciles_wal_into_main_db(self):
        dump_path = Path(self.temp_dir.name) / 'portable.db'

        dump_graph(str(dump_path), use_gc=False)

        assert dump_path.exists()
        assert not dump_path.with_name(dump_path.name + '-wal').exists()

        conn = sqlite3.connect(str(dump_path))
        try:
            assert conn.execute('SELECT COUNT(*) FROM meta').fetchone()[0] == 1
            assert conn.execute('SELECT COUNT(*) FROM object').fetchone()[0] > 0
            assert conn.execute('PRAGMA journal_mode').fetchone()[0].lower() == 'delete'
        finally:
            conn.close()

    def test_dump_graph_survives_getfullargspec_failures(self):
        dump_path = Path(self.temp_dir.name) / 'getfullargspec-failure.db'

        def raising_getfullargspec(obj):
            raise TypeError('unsupported callable')

        with patch('objex.exporter.inspect.getfullargspec', side_effect=raising_getfullargspec):
            dump_graph(str(dump_path), use_gc=False)

        with Reader(str(dump_path)) as reader:
            assert reader.object_count() > 0

    def test_dump_graph_survives_empty_closure_cells(self):
        dump_path = Path(self.temp_dir.name) / 'empty-closure.db'

        dump_graph(str(dump_path), use_gc=False)

        with Reader(str(dump_path)) as reader:
            assert reader.sql_val("SELECT COUNT(*) FROM function WHERE func_name = ?", ('inner',)) > 0

    def test_dump_graph_survives_non_attributeerror_dunder_dict_access(self):
        dump_path = Path(self.temp_dir.name) / 'exploding-getattr.db'

        dump_graph(str(dump_path), use_gc=False)

        with Reader(str(dump_path)) as reader:
            assert reader.find_type_by_name('ExplodingGetattr')

    def test_dump_graph_uses_resolved_dunder_dict_without_reaccess(self):
        dump_path = Path(self.temp_dir.name) / 'misdirecting-getattribute.db'

        dump_graph(str(dump_path), use_gc=False)

        with Reader(str(dump_path)) as reader:
            assert reader.find_type_by_name('MisdirectingGetattribute')

    @unittest.skipUnless(hasattr(os, 'fork'), 'requires os.fork')
    def test_spawn_dump_and_wait_dump(self):
        dump_path = Path(self.temp_dir.name) / 'forked.db'
        self.stop_event.set()
        self.thread.join(timeout=2)

        pid = spawn_dump(str(dump_path), use_gc=False)
        exit_code = wait_dump(pid)

        assert exit_code == 0
        assert dump_path.exists()
        assert not dump_path.with_name(dump_path.name + '-wal').exists()

        with Reader(str(dump_path)) as reader:
            assert reader.object_count() > 0

    def test_module_help_does_not_start_console(self):
        result = subprocess.run(
            [sys.executable, '-m', 'objex', '--help'],
            capture_output=True,
            text=True,
            check=True,
            cwd=Path(__file__).resolve().parents[1],
        )

        assert 'usage:' in result.stdout
        assert 'make-analysis-db' in result.stdout
        assert 'explore' in result.stdout
        assert 'WELCOME TO OBJEX EXPLORER' not in result.stdout

    def test_module_cli_parser_supports_legacy_explore_and_analysis_command(self):
        result = subprocess.run(
            [sys.executable, '-m', 'objex', 'make-analysis-db', '--help'],
            capture_output=True,
            text=True,
            check=True,
            cwd=Path(__file__).resolve().parents[1],
        )
        assert 'collection_db' in result.stdout
        assert 'analysis_db' in result.stdout

        result = subprocess.run(
            [sys.executable, '-m', 'objex', 'explore', '--help'],
            capture_output=True,
            text=True,
            check=True,
            cwd=Path(__file__).resolve().parents[1],
        )
        assert 'analysis_db' in result.stdout

    def test_main_supports_existing_path_legacy_explore_form(self):
        base_path = Path(self.temp_dir.name)
        dump_path = base_path / 'legacy.db'
        analysis_path = base_path / 'legacy-analysis.db'
        dump_graph(str(dump_path), use_gc=False)
        make_analysis_db(str(dump_path), str(analysis_path))

        with patch('objex.__main__.explorer.Console.run', return_value=None):
            assert objex_main.main([str(analysis_path)]) == 0

    def test_main_does_not_treat_typos_as_legacy_explore_paths(self):
        try:
            objex_main.main(['expore'])
        except SystemExit as exc:
            assert exc.code == 2
        else:
            assert False, 'expected parser failure for unknown command typo'

    def test_reader_rejects_invalid_objex_db(self):
        invalid_db = Path(self.temp_dir.name) / 'invalid.db'
        conn = sqlite3.connect(str(invalid_db))
        try:
            conn.execute('CREATE TABLE meta (id INTEGER PRIMARY KEY, hostname TEXT)')
            conn.commit()
        finally:
            conn.close()

        try:
            Reader(str(invalid_db))
        except InvalidDatabaseError:
            pass
        else:
            assert False, 'expected InvalidDatabaseError'

    def test_web_api_serves_summary_and_object_views(self):
        base_path = Path(self.temp_dir.name)
        dump_path = base_path / 'web.db'
        analysis_path = base_path / 'web-analysis.db'
        dump_graph(str(dump_path), use_gc=False)
        make_analysis_db(str(dump_path), str(analysis_path))

        status_code, _, body = dispatch_request(str(analysis_path), '/api/summary')
        assert status_code == 200
        summary = json.loads(body)
        assert 'hostname' in summary
        assert summary['object_count'] > 0

        status_code, _, body = dispatch_request(str(analysis_path), '/api/marks')
        assert status_code == 200
        marks_payload = json.loads(body)
        assert marks_payload['items'] == []

        status_code, _, body = dispatch_request(str(analysis_path), '/api/top-types?limit=5')
        assert status_code == 200
        top_types_payload = json.loads(body)
        assert 'items' in top_types_payload
        assert top_types_payload['items']

        status_code, _, body = dispatch_request(str(analysis_path), '/api/largest-objects?limit=5')
        assert status_code == 200
        largest_objects_payload = json.loads(body)
        assert 'items' in largest_objects_payload
        assert largest_objects_payload['items']

        status_code, _, body = dispatch_request(str(analysis_path), '/api/random')
        assert status_code == 200
        random_payload = json.loads(body)
        status_code, _, body = dispatch_request(
            str(analysis_path), '/api/object?id={}'.format(random_payload['id'])
        )
        assert status_code == 200
        object_payload = json.loads(body)
        assert object_payload['id'] == random_payload['id']
        assert 'label' in object_payload

        status_code, _, body = dispatch_request(
            str(analysis_path), '/api/mark?id={}&mark={}'.format(random_payload['id'], 'interesting')
        )
        assert status_code == 200
        assert json.loads(body)['ok']

        status_code, _, body = dispatch_request(str(analysis_path), '/api/marks')
        assert status_code == 200
        marks_payload = json.loads(body)
        assert marks_payload['items'][0]['mark'] == 'interesting'

        status_code, _, body = dispatch_request(
            str(analysis_path), '/api/referents?id={}&limit=5'.format(random_payload['id'])
        )
        assert status_code == 200
        referents_payload = json.loads(body)
        assert 'count' in referents_payload
        assert 'items' in referents_payload

        status_code, _, body = dispatch_request(
            str(analysis_path), '/api/path-to-module?id={}&limit=5'.format(random_payload['id'])
        )
        assert status_code == 200
        module_paths_payload = json.loads(body)
        assert 'items' in module_paths_payload

        status_code, _, body = dispatch_request(
            str(analysis_path), '/api/path-to-frame?id={}&limit=5'.format(random_payload['id'])
        )
        assert status_code == 200
        frame_paths_payload = json.loads(body)
        assert 'items' in frame_paths_payload

    def test_console_commands_validate_missing_args(self):
        base_path = Path(self.temp_dir.name)
        dump_path = base_path / 'console.db'
        analysis_path = base_path / 'console-analysis.db'
        dump_graph(str(dump_path), use_gc=False)
        make_analysis_db(str(dump_path), str(analysis_path))

        with Reader(str(analysis_path)) as reader:
            console = Console(reader)
            output = StringIO()
            with redirect_stdout(output):
                console.do_path_to([])
                console.do_path_from([])
                console.do_mark([])
                console.do_top([])

        text = output.getvalue()
        assert 'path_to command expects one argument' in text
        assert 'path_from command expects one argument' in text
        assert 'mark command expects one argument' in text
        assert 'top command expects one or two arguments' in text
