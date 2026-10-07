import json
from pathlib import Path
import unittest

from migreplay.inputs import load_case
from migreplay.model import Limits
from tests import test_comparison as helpers


class ExampleTests(unittest.TestCase):
    execute = helpers.ComparisonTests.execute

    def test_handwritten_examples(self):
        root = Path(__file__).resolve().parents[1] / 'examples'
        for name in ('notes-loss', 'notes-preserved', 'null-default', 'foreign-key-rebuild'):
            directory = root / name
            expected = json.loads((directory / 'expected.json').read_text(encoding='utf-8'))
            for manifest, want in expected.items():
                with self.subTest(example=name, manifest=manifest):
                    value = load_case(directory / manifest, Limits())
                    _, trace, report = self.execute(value)
                    self.assertEqual(report.summary['exit_code'], want['exit_code'])
                    self.assertEqual(report.summary['status'], want['status'])
                    if 'down' in want:
                        result = report.comparisons[0]['table_results']
                        table = next(t for t in result if t['table'] == want['table'])
                        for key, expected_value in want['down'].items():
                            self.assertEqual(table[key], expected_value, key)
                    if name == 'notes-loss':
                        comparison = report.comparisons[0]
                        table = comparison['table_results'][0]
                        self.assertEqual(table['before']['row_count'], 10)
                        self.assertEqual(table['after']['row_count'], 10)
                        self.assertEqual(table['before']['key_digest'], table['after']['key_digest'])
                        self.assertEqual(table['before']['null_counts']['body'], 0)
                        self.assertEqual(table['after']['null_counts']['body'], 10)
                        self.assertEqual(comparison['first_observed_snapshot_id'], 'up:001/statement/1')
                        self.assertEqual(comparison['first_failed_restoration']['file'], '001.down.sql')
                    if name == 'notes-preserved':
                        self.assertEqual([c['status'] for c in report.comparisons], ['restored', 'restored'])
                        self.assertTrue(all(a['status'] == 'passed' for a in report.assertions))
                    if name == 'null-default':
                        table = report.comparisons[0]['table_results'][0]
                        self.assertEqual((table['before']['null_counts']['value'], table['after']['null_counts']['value']), (1, 0))
                    if manifest == 'invalid-reference.json':
                        self.assertEqual(trace.stages[1].status, 'rolled_back')
                        self.assertIsNone(trace.stages[1].final_snapshot_id)
