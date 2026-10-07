import importlib.util
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from migreplay.model import Limits
from tests.test_comparison import fixture


def _stall(*args, **kwargs):
    Path(os.environ['MIGREPLAY_TEST_MARKER']).write_text('entered', encoding='utf-8')
    time.sleep(10)


def blocked_analysis(*args):
    from migreplay.runner import _analysis_coordinator
    with patch('migreplay.compare.analyze_trace', _stall):
        _analysis_coordinator(*args)


def blocked_decode(*args):
    from migreplay import runner
    original = runner._decode
    calls = 0
    def decode(*values):
        nonlocal calls
        calls += 1
        if calls == 2:
            _stall()
        return original(*values)
    with patch('migreplay.runner._decode', decode):
        runner._analysis_coordinator(*args)


def blocked_projection(*args):
    from migreplay.runner import _analysis_coordinator
    with patch('migreplay.status.project_report', _stall):
        _analysis_coordinator(*args)


class AnalysisProcessesTests(unittest.TestCase):
    def api(self):
        from migreplay import runner
        self.assertTrue(hasattr(runner, '_supervise_analysis'), 'real analysis supervisor is missing')
        return runner._supervise_analysis

    def test_real_analysis_uses_shared_deadline_and_separate_ledgers(self):
        supervise = self.api()
        result = supervise(fixture(), Limits())
        self.assertEqual(result['summary'].status, 'unexpected_loss')
        self.assertEqual(result['summary'].exit_code, 1)
        self.assertEqual(result['summary'].unexpected_count, 1)
        self.assertGreater(result['snapshot_bytes_total'], 0)
        self.assertGreater(result['ipc_bytes_total'], 0)
        self.assertTrue(result['reaped'])
        self.assertEqual(result['child_count'], 2)
        self.assertFalse(Path(result['temp_path']).exists())
        failed = supervise(fixture(), Limits(ipc_bytes_total=100))
        self.assertEqual(failed['code'], 'ipc_limit')
        self.assertEqual(failed['summary'].exit_code, 3)
        self.assertLessEqual(failed['ipc_bytes_total'], 100)

    def test_blocking_decode_analysis_and_projection_are_hard_terminated(self):
        supervise = self.api()
        with tempfile.TemporaryDirectory() as name:
            sentinel = Path(name) / 'sentinel'
            sentinel.write_text('external synthetic sentinel', encoding='utf-8')
            for target in (blocked_decode, blocked_analysis, blocked_projection):
                with self.subTest(target=target.__name__):
                    marker = Path(name) / target.__name__
                    with patch.dict(os.environ, {'MIGREPLAY_TEST_MARKER': str(marker)}):
                        result = supervise(fixture(), Limits(run_seconds=2), _coordinator_target=target)
                    self.assertTrue(marker.exists(), 'must reach the actual producer phase before timeout')
                    self.assertEqual(result['code'], 'run_timeout')
                    self.assertEqual(result['summary'].exit_code, 3)
                    self.assertTrue(result['reaped'])
                    self.assertEqual(result['child_count'], 2)
                    self.assertLess(result['elapsed'], 4)
                    self.assertFalse(Path(result['temp_path']).exists())
                    self.assertEqual(sentinel.read_text(encoding='utf-8'), 'external synthetic sentinel')
