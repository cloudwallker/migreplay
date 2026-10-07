from dataclasses import replace
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import _thread
import time
import unittest
from unittest.mock import patch

from migreplay.model import Diagnostic, Limits, OutputRequest
from migreplay.status import failure_report
from tests import test_comparison as helpers

fixture = helpers.fixture


def blocked_render(*args):
    from migreplay.runner import _analysis_coordinator
    def stall(*a):
        Path(os.environ['MIGREPLAY_TEST_MARKER']).write_text('entered')
        time.sleep(10)
    with patch('migreplay.report.render_html', stall):
        _analysis_coordinator(*args)


def blocked_encode(*args):
    from migreplay.runner import _analysis_coordinator
    def stall(*a, **kw):
        Path(os.environ['MIGREPLAY_TEST_MARKER']).write_text('entered')
        time.sleep(10)
    with patch('migreplay.report._encoded_chunks', stall):
        _analysis_coordinator(*args)


def blocked_write(*args):
    from migreplay.runner import _analysis_coordinator
    def stall(*a, **kw):
        Path(os.environ['MIGREPLAY_TEST_MARKER']).write_text('entered')
        time.sleep(10)
    with patch('migreplay.report._write_chunk', stall):
        _analysis_coordinator(*args)


class ReportTests(unittest.TestCase):
    execute = helpers.ComparisonTests.execute

    def api(self):
        self.assertIsNotNone(importlib.util.find_spec('migreplay.report'), 'report consumer missing')
        from migreplay import report
        return report

    def test_report_minimizes_data_and_escapes_markup(self):
        api = self.api()
        _, _, report = self.execute(fixture())
        model = api.report_to_dict(report)
        html = api.render_html(report)
        self.assertIn('结构恢复，数据未恢复', html)
        serialized = json.dumps(model) + html
        for private in ('INSERT INTO', 'CREATE TABLE', '_rows', '_schema', '_key_index', 'note-01'):
            self.assertNotIn(private, serialized)
        self.assertIn('Content-Security-Policy', html)
        self.assertNotIn('<script', html)
        self.assertNotIn('fetch(', html)
        model['allowance_usage'].append(dict(id='a', migration='one', phase='down', table='t', kind='value_changed', columns=['body'], reason='<script>"中文</script>', matched_finding_ids=[], unused=True))
        model['comparisons'][0]['table_results'][0]['table'] = '</script><img src=x onerror=alert(1)>'
        escaped = api.render_html(report)
        self.assertIn('&lt;script&gt;', escaped)
        self.assertNotIn('<img', escaped)
        self.assertIn('&quot;中文', escaped)

    def test_status_and_evidence_agree(self):
        api = self.api()
        for code, status in [('invalid_input', 'invalid_input'), ('execution_failed', 'incomplete')]:
            report = failure_report(Diagnostic(code), {}, Limits())
            self.assertEqual(api.report_to_dict(report)['summary']['status'], status)
            self.assertIn(status, api.render_html(report))
        _, _, report = self.execute(fixture())
        html = api.render_html(report)
        for label in ('schema_digest', 'row_count', 'key_digest', 'column_digests', 'null_counts', 'up.sql', 'final_snapshot_id'):
            self.assertIn(label, html)

    def test_utf8_caps_cleanup(self):
        api = self.api()
        from migreplay.runner import coordinate_run
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            sentinel = parent / 'sentinel'
            sentinel.write_text('keep')
            for limits in [Limits(json_bytes=100), Limits(html_bytes=100)]:
                destination = parent / 'new-parent/result'
                summary = coordinate_run(fixture(), limits, sink=OutputRequest(destination, ()))
                self.assertEqual(summary.exit_code, 3)
                self.assertFalse(destination.exists())
                self.assertFalse(destination.parent.exists())
            self.assertEqual(sentinel.read_text(), 'keep')

    def test_encoding_render_and_write_hard_timeout(self):
        self.api()
        from migreplay.runner import supervise_counts
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            for target in (blocked_render, blocked_encode, blocked_write):
                destination = parent / target.__name__ / 'result'
                marker = parent / (target.__name__ + '-entered')
                with patch.dict(os.environ, {'MIGREPLAY_TEST_MARKER': str(marker)}):
                    result = supervise_counts(fixture(), Limits(run_seconds=2), _analysis=True,
                        _coordinator_target=target, _sink=OutputRequest(destination, ()))
                self.assertTrue(marker.exists())
                self.assertEqual(result['code'], 'run_timeout')
                self.assertTrue(result['reaped'])
                self.assertEqual(result['child_count'], 2)
                self.assertFalse(destination.exists())
                self.assertFalse(Path(result['temp_path']).exists())

    def test_write_failure_and_replaced_output_preserve_external_files(self):
        api = self.api()
        from migreplay.model import SafeError
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            destination = parent / 'result'
            external = parent / 'sentinel'
            external.write_text('external')
            report = failure_report(Diagnostic('invalid_input'), {}, Limits())
            with patch('migreplay.report._write_chunk', side_effect=OSError('PRIVATE_ERROR')):
                with self.assertRaises(SafeError) as result:
                    api.write_reports(report, destination, ())
            self.assertEqual(result.exception.code, 'output_failed')
            self.assertFalse(destination.exists())
            original = os.link
            def replace_before_publish(source, target):
                source.unlink()
                original(external, source)
                return original(source, target)
            with patch('migreplay.report.os.link', replace_before_publish):
                with self.assertRaises(SafeError):
                    api.write_reports(report, destination, ())
            self.assertEqual(external.read_text(), 'external')

    def test_utf8_bytes_not_character_count(self):
        api = self.api()
        from migreplay.model import SafeError
        with self.assertRaises(SafeError):
            list(api._encoded_chunks(('中文' * 20,), 50, None))

    def test_cancel_during_output_reaps_and_cleans(self):
        self.api()
        from migreplay.runner import supervise_counts
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / 'result'
            marker = Path(tmp) / 'entered'
            stop = threading.Event()
            def interrupt():
                while not stop.wait(0.02):
                    if marker.exists():
                        _thread.interrupt_main()
                        return
            thread = threading.Thread(target=interrupt)
            thread.start()
            try:
                with patch.dict(os.environ, {'MIGREPLAY_TEST_MARKER': str(marker)}):
                    result = supervise_counts(fixture(), Limits(), _analysis=True,
                        _coordinator_target=blocked_write, _sink=OutputRequest(destination, ()))
            finally:
                stop.set()
                thread.join()
            self.assertTrue(marker.exists())
            self.assertEqual(result['code'], 'cancelled')
            self.assertTrue(result['reaped'])
            self.assertFalse(destination.exists())

    def test_no_case_uses_factory_without_sql_worker(self):
        self.api()
        from migreplay.runner import supervise_counts, _analysis_coordinator
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / 'result'
            result = supervise_counts(None, Limits(), _analysis=True, _coordinator_target=_analysis_coordinator,
                _sink=OutputRequest(destination, ()), _initial_failure=Diagnostic('unknown_stage'))
            self.assertEqual(result['child_count'], 1)
            self.assertTrue(result['reaped'])
            model = json.loads((destination / 'report.json').read_text())
            self.assertEqual(model['summary'], vars(result['summary']))
            self.assertEqual(model['stages'], [])
            self.assertEqual(model['diagnostics'][0]['code'], 'unknown_stage')


if __name__ == '__main__':
    unittest.main()
