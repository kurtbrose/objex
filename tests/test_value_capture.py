'''
end-to-end coverage for optional value capture (values= tiers) and the
default-on sensitive-key redaction pass. dumps are in-process (fast);
assertions go straight at the collection db via sqlite, except the reader
integration test which goes through make_analysis_db + Reader.
'''
import sqlite3
import tempfile
from pathlib import Path

import pytest

from objex import Reader, dump_graph, make_analysis_db
from objex.exporter import _len_bucket


# module-level fixture globals so the dump can see them
SAFE_TEXT = 'objex-visible-marker-' + 'x' * 20
LEAK = {
    'password': 'hunter2-' * 80,          # len 640 -> bucket 1000
    'safe_note': SAFE_TEXT,               # innocent key; value must stay readable
    'api_token': 'tok-' + 'y' * 600,      # len 604 -> bucket 1000
    'count': 424242424242,                # unique (non-interned) int
    'audit_trail': [],                    # tracked value: keeps the dict in the
                                          # gc snapshot so its keys get traversed
                                          # (CPython untracks atomic-only dicts)
}


class ReprMarker:
    def __repr__(self):
        return 'objex-repr-marker-instance'


class ExplodingRepr:
    def __repr__(self):
        raise RuntimeError('never dump me')


REPR_INSTANCE = ReprMarker()
EXPLODING_INSTANCE = ExplodingRepr()


@pytest.fixture
def tmp_db_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


def _dump(tmp_db_dir, name, **kwargs):
    path = tmp_db_dir / name
    dump_graph(str(path), **kwargs)
    conn = sqlite3.connect(str(path))
    conn.text_factory = str
    return path, conn


def _key_value_rows(conn, key_repr):
    '''object_repr rows of the values referenced under dict key key_repr'''
    key_ids = [row[0] for row in conn.execute(
        'SELECT object FROM object_repr WHERE repr = ?', (key_repr,))]
    assert key_ids, 'key {} not captured'.format(key_repr)
    rows = []
    for key_id in key_ids:
        rows.extend(conn.execute(
            'SELECT object_repr.repr, object_repr.redacted, object_repr.truncated'
            ' FROM reference JOIN object_repr ON reference.dst = object_repr.object'
            ' WHERE reference.ref = ?', ('@{}'.format(key_id),)))
    return rows


def test_builtins_tier_redacts_sensitive_values(tmp_db_dir):
    _, conn = _dump(tmp_db_dir, 'builtins.db', values='builtins')
    try:
        # innocent value stays readable
        safe_rows = conn.execute(
            "SELECT redacted FROM object_repr WHERE repr LIKE '%objex-visible-marker%'").fetchall()
        assert safe_rows
        assert all(redacted == 0 for (redacted,) in safe_rows)

        # key strings themselves stay readable, un-redacted
        for key_repr in ("'password'", "'api_token'"):
            key_rows = conn.execute(
                'SELECT redacted FROM object_repr WHERE repr = ?', (key_repr,)).fetchall()
            assert key_rows, '{} missing'.format(key_repr)
            assert all(redacted == 0 for (redacted,) in key_rows)

        # values under sensitive keys are bucketed placeholders
        # (filter to redacted rows: CPython >=3.14 interned-string dicts map
        # key strings to themselves, producing a non-redacted value_row where
        # the "value" is the key label itself -- those are correctly kept readable)
        for key_repr in ("'password'", "'api_token'"):
            value_rows = _key_value_rows(conn, key_repr)
            assert value_rows
            redacted_rows = [(t, r, tr) for t, r, tr in value_rows if r == 1]
            assert redacted_rows, 'no redacted values for {}'.format(key_repr)
            for text, redacted, truncated in redacted_rows:
                assert text == '<redacted len<=1000>'
                assert truncated == 0

        # the secret text appears nowhere in the db
        assert conn.execute(
            "SELECT count(*) FROM object_repr WHERE repr LIKE '%hunter2%'").fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM object_repr WHERE repr LIKE '%tok-yy%'").fetchone()[0] == 0

        # innocent int captured verbatim
        count_rows = _key_value_rows(conn, "'count'")
        assert ('424242424242', 0, 0) in count_rows

        # dump is self-describing
        assert conn.execute('SELECT capture_values FROM meta').fetchone()[0] == 'builtins'
    finally:
        conn.close()


def test_default_none_tier_captures_nothing(tmp_db_dir):
    _, conn = _dump(tmp_db_dir, 'none.db')
    try:
        assert conn.execute('SELECT count(*) FROM object_repr').fetchone()[0] == 0
        assert conn.execute('SELECT capture_values FROM meta').fetchone()[0] == 'none'
    finally:
        conn.close()


def test_redact_false_keeps_sensitive_values(tmp_db_dir):
    _, conn = _dump(tmp_db_dir, 'noredact.db', values='builtins', redact=False)
    try:
        assert conn.execute(
            "SELECT count(*) FROM object_repr WHERE repr LIKE '%hunter2%'").fetchone()[0] > 0
        assert conn.execute(
            'SELECT count(*) FROM object_repr WHERE redacted = 1').fetchone()[0] == 0
    finally:
        conn.close()


def test_repr_tier_captures_instances_and_survives_raising_repr(tmp_db_dir):
    _, conn = _dump(tmp_db_dir, 'repr.db', values='repr')
    try:
        assert conn.execute(
            "SELECT count(*) FROM object_repr WHERE repr LIKE '%objex-repr-marker-instance%'"
        ).fetchone()[0] > 0
        # the ExplodingRepr instance has an object row but no repr row
        assert conn.execute(
            "SELECT count(*) FROM pytype WHERE name = 'ExplodingRepr'").fetchone()[0] == 1
    finally:
        conn.close()


def test_value_limit_truncates(tmp_db_dir):
    _, conn = _dump(tmp_db_dir, 'trunc.db', values='builtins', value_limit=16)
    try:
        rows = conn.execute(
            "SELECT repr, truncated FROM object_repr WHERE repr LIKE '%objex-visible%'").fetchall()
        assert rows
        for text, truncated in rows:
            assert truncated == 1
            assert len(text) <= 16
    finally:
        conn.close()


def test_len_bucket():
    assert _len_bucket(0) == 100
    assert _len_bucket(100) == 100
    assert _len_bucket(101) == 500
    assert _len_bucket(640) == 1000
    assert _len_bucket(4999) == 5000
    assert _len_bucket(10001) == 50000


def test_invalid_tier_raises(tmp_db_dir):
    with pytest.raises(ValueError):
        dump_graph(str(tmp_db_dir / 'bad.db'), values='everything')


def test_reader_integration(tmp_db_dir):
    dump_path, conn = _dump(tmp_db_dir, 'reader.db', values='builtins')
    try:
        safe_id = conn.execute(
            "SELECT object FROM object_repr WHERE repr LIKE '%objex-visible-marker%'"
        ).fetchone()[0]
    finally:
        conn.close()
    analysis_path = tmp_db_dir / 'reader-analysis.db'
    make_analysis_db(str(dump_path), str(analysis_path))
    with Reader(str(analysis_path)) as reader:
        value = reader.obj_repr(safe_id)
        assert 'objex-visible-marker' in value
        assert reader.object_summary(safe_id)['repr'] == value
        label = reader.object_label(safe_id)
        assert 'objex-visible' in label
        # display cap: full 43-char repr never lands in the label
        assert value not in label
