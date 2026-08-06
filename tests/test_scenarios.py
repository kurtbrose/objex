'''
Leak-scenario integration tests: each scenario builds a leak in a bare
subprocess whose payloads are reachable only through a "tricky" edge, dumps
the graph, and this side proves the leak is *attributable* -- the payloads
are found by type name and a path to a module/frame root exists that passes
through the scenario's anchor edge.
'''

import os
import subprocess
import sys
from pathlib import Path

import pytest

from objex import make_analysis_db
from objex.explorer import Reader

from tests.scenarios.leak_cases import SCENARIOS, PAYLOAD_COUNTS


REPO_ROOT = Path(__file__).resolve().parents[1]

# scenarios whose leak anchor is a live frame rather than a module global
FRAME_SCENARIOS = frozenset({'suspended_coroutine', 'thread_frame'})

# every marker must appear in some ref of the returned path
PATH_MARKERS = {
    'module_global': ('LEAK_module_global',),
    'exception_traceback': ('.__traceback__', '.tb_frame'),
    'closure_cell': (".locals['payloads']",),
    'suspended_coroutine': (".locals['payloads']",),
    'thread_frame': (".locals['payloads']",),
    'lru_cache': ('<gc>',),
    'iterator_pin': ('<gc>',),
}


@pytest.mark.scenario
@pytest.mark.parametrize('name', sorted(SCENARIOS))
def test_scenario_leak_is_attributable(name, tmp_path):
    dump_path = tmp_path / (name + '.db')
    analysis_path = tmp_path / (name + '-analysis.db')
    env = dict(os.environ)
    env['PYTHONPATH'] = str(REPO_ROOT)
    subprocess.run(
        [sys.executable, '-m', 'tests.scenarios.leak_cases', name, str(dump_path)],
        check=True,
        cwd=REPO_ROOT,
        env=env,
    )
    make_analysis_db(str(dump_path), str(analysis_path))

    with Reader(str(analysis_path)) as reader:
        type_ids = reader.find_type_by_name('Payload_' + name)
        assert len(type_ids) == 1
        type_id = type_ids[0]
        assert reader.obj_instance_count(type_id) >= PAYLOAD_COUNTS[name]

        instance_id = reader.random_instances(type_id, limit=1)[0]
        if name in FRAME_SCENARIOS:
            paths = reader.find_path_to_frame(instance_id)
        else:
            paths = reader.find_path_to_module(instance_id)
        assert paths and paths[0], 'no root path found for scenario %r' % name

        refs = [str(ref) for _, ref in paths[0]]
        for marker in PATH_MARKERS[name]:
            assert any(marker in ref for ref in refs), (
                'marker %r not in path refs %r' % (marker, refs))

        if name == 'suspended_coroutine':
            # the path roots at the suspended frame; prove the frame itself is
            # pinned through the coroutine chain (Task -> coroutine -> cr_frame)
            root_frame_id = paths[0][0][0]
            assert reader.sql_val(
                "SELECT COUNT(*) FROM reference WHERE ref = '.cr_frame' AND dst = ?",
                (root_frame_id,),
            ) >= 1
