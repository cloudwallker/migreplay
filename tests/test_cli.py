import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class CliTests(unittest.TestCase):
    def invoke(self, *args):
        return subprocess.run([sys.executable, '-m', 'migreplay', *map(str, args)], cwd=ROOT,
                              capture_output=True, text=True, encoding='utf-8', timeout=40)

    def run_case(self, manifest, output):
        return self.invoke('run', '--manifest', manifest, '--out-dir', output)

    def test_cli_runs_real_sql(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            for name, code in [('notes-loss', 1), ('notes-preserved', 0)]:
                out = parent / name
                result = self.run_case(ROOT / 'examples' / name / 'case.json', out)
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertTrue((out / 'report.json').is_file(), result.stderr)
                model = json.loads((out / 'report.json').read_text(encoding='utf-8'))
                self.assertIn(model['summary']['status'], result.stdout)
                self.assertIn(model['summary']['status'], (out / 'report.html').read_text(encoding='utf-8'))
                self.assertEqual(model['summary']['exit_code'], code)
                if name == 'notes-loss':
                    table = model['comparisons'][0]['table_results'][0]
                    self.assertEqual(table['changed_count'], 10)
                    self.assertEqual(table['after']['null_counts']['body'], 10)
                    original = table['before']['row_digest']
            copied = parent / 'changed'
            shutil.copytree(ROOT / 'examples' / 'notes-loss', copied)
            seed = copied / 'seed.sql'
            seed.write_text(seed.read_text().replace('note-01', 'different synthetic body'), encoding='utf-8')
            result = self.run_case(copied / 'case.json', parent / 'changed-report')
            self.assertEqual(result.returncode, 1)
            actual = json.loads((parent / 'changed-report/report.json').read_text(encoding='utf-8'))
            self.assertNotEqual(actual['comparisons'][0]['table_results'][0]['before']['row_digest'], original)

    def test_invalid_manifest_three_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            for name in ('missing', 'unknown', 'at'):
                manifest = parent / (name + '.json')
                if name != 'missing':
                    source = ROOT / 'examples/notes-loss'
                    for sql in source.glob('*.sql'):
                        shutil.copyfile(sql, parent / sql.name)
                    doc = json.loads((source / 'case.json').read_text())
                    if name == 'unknown':
                        doc['secret_unknown'] = 'RAW_ERROR_DO_NOT_EXPORT'
                    else:
                        doc['assertions'] = [{'id': 'bad', 'at': ['unknown'], 'sql': 'SELECT 1', 'expect': 1}]
                    manifest.write_text(json.dumps(doc), encoding='utf-8')
                result = self.run_case(manifest, parent / name)
                self.assertEqual(result.returncode, 2, result.stderr)
                model = json.loads((parent / name / 'report.json').read_text())
                self.assertEqual(model['stages'], [])
                self.assertEqual(model['summary']['status'], 'invalid_input')
                self.assertIn('invalid_input', result.stdout)
                self.assertIn('invalid_input', (parent / name / 'report.html').read_text(encoding='utf-8'))

    def test_output_never_overwrites_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            source = parent / 'input'
            shutil.copytree(ROOT / 'examples/notes-loss', source)
            manifest = source / 'case.json'
            sentinel = source / 'sentinel'
            sentinel.write_text('keep', encoding='utf-8')
            before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source.iterdir()}
            hardlink = parent / 'hardlink'
            os.link(manifest, hardlink)
            existing = parent / 'existing'
            existing.mkdir()
            destinations = [manifest, source, parent, hardlink, existing, parent / 'absent/../escape']
            if os.name == 'nt':
                junction = parent / 'junction'
                subprocess.run(['cmd', '/c', 'mklink', '/J', str(junction), str(source)], capture_output=True, check=True)
                destinations.append(junction / 'new')
            for destination in destinations:
                result = self.run_case(manifest, destination)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn('invalid_input', result.stdout)
            self.assertFalse((parent / 'absent').exists())
            self.assertFalse((source / 'new').exists())
            self.assertEqual(before, {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source.iterdir()})

    def test_demo_and_version(self):
        self.assertIn('SQLite', self.invoke('--version').stdout)
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'reports/demo'
            result = self.invoke('demo', '--out-dir', output)
            self.assertEqual(result.returncode, 1, result.stderr)
            for name, status in [('notes-loss', 'unexpected_loss'), ('notes-preserved', 'passed')]:
                self.assertEqual(json.loads((output / name / 'report.json').read_text())['summary']['status'], status)

    def test_expected_and_incomplete_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name, manifest, code, status in [
                ('expected', 'null-default/allowed.json', 0, 'expected_irreversible'),
                ('incomplete', 'foreign-key-rebuild/invalid-reference.json', 3, 'incomplete')]:
                output = Path(tmp) / name
                result = self.run_case(ROOT / 'examples' / manifest, output)
                self.assertEqual(result.returncode, code, result.stderr)
                model = json.loads((output / 'report.json').read_text())
                self.assertEqual(model['summary']['status'], status)
                self.assertIn(status, result.stdout)
                self.assertIn(status, (output / 'report.html').read_text(encoding='utf-8'))
                if name == 'expected':
                    self.assertGreater(model['summary']['expected_count'], 0)

    def test_unknown_input_boundary_creates_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'out'
            result = self.run_case(Path(tmp) / 'missing/case.json', output)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
