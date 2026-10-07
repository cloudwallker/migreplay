import hashlib
import json
from pathlib import Path
import tempfile
import subprocess
import os
import unittest

try:
    from migreplay.inputs import load_case
    from migreplay.model import InvalidInput, Limits
    from migreplay.sql import split_sql
except ImportError:
    load_case = split_sql = None


class InputTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name, text in {'schema.sql': 'CREATE TABLE t(x);', 'seed.sql': "INSERT INTO t VALUES('synthetic');", 'up.sql': 'ALTER TABLE t ADD COLUMN y;', 'down.sql': 'ALTER TABLE t DROP COLUMN y;'}.items():
            (self.root / name).write_text(text, encoding='utf-8')
        self.manifest = self.root / 'case.json'
        self.doc = dict(format_version=1, synthetic_seed=True, schema='schema.sql', seed='seed.sql', migrations=[dict(id='one', up='up.sql', down='down.sql')])

    def save(self):
        self.manifest.write_text(json.dumps(self.doc), encoding='utf-8')

    def test_inputs_read_only_and_bounded(self):
        self.assertIsNotNone(load_case, 'bounded input loader is not implemented')
        self.save()
        before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.root.iterdir()}
        case = load_case(self.manifest, Limits())
        self.assertEqual([s.label for s in case.sources], ['case.json', 'schema.sql', 'seed.sql', 'up.sql', 'down.sql'])
        self.assertEqual(len(case._input_paths), 5)
        from migreplay.runner import supervise_counts
        self.assertEqual(supervise_counts(case, Limits())['code'], 'ok')
        self.assertEqual(before, {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.root.iterdir()})
        original = json.loads(json.dumps(self.doc))
        mutations = [dict(synthetic_seed=False), dict(unknown=True), dict(schema='../escape.sql'), dict(schema='https://example.invalid/x'), dict(schema=str(self.root / 'schema.sql')), dict(migrations=[dict(id='one', up='up.sql')]), dict(migrations=self.doc['migrations'] * 2), dict(assertions=[dict(id='a', at=['down:missing'], sql='SELECT 1;', expect=1)])]
        for change in mutations:
            with self.subTest(change=tuple(change)):
                self.doc = original | change
                self.save()
                with self.assertRaises(InvalidInput):
                    load_case(self.manifest, Limits())
        self.doc = original
        self.save()
        for limits in [Limits(manifest_bytes=1), Limits(sql_file_bytes=3), Limits(sql_total_bytes=30), Limits(statements_per_file=1, executed_statements=2)]:
            with self.assertRaises(InvalidInput):
                load_case(self.manifest, limits)
        (self.root / 'schema.sql').write_bytes(b'SQLite format 3\x00binary')
        with self.assertRaises(InvalidInput):
            load_case(self.manifest, Limits())

    def test_statement_locations(self):
        self.assertIsNotNone(split_sql, 'SQL splitter is not implemented')
        statements = split_sql("-- note\r\nCREATE TABLE t(x);\r\nINSERT INTO t VALUES('a;b');", 'schema.sql')
        self.assertEqual([(s.source.statement, s.source.start_line, s.source.end_line) for s in statements], [(1, 2, 2), (2, 3, 3)])
        trigger = split_sql('CREATE TRIGGER tr AFTER INSERT ON t BEGIN\n UPDATE t SET x=1;\n UPDATE t SET x=2;\nEND; -- tail', 'up.sql')
        self.assertEqual(len(trigger), 1)
        self.assertEqual((trigger[0].source.start_line, trigger[0].source.end_line), (1, 4))
        self.assertEqual(len(split_sql('\ufeffSELECT 1; SELECT 2; /* tail */', 'x.sql')), 2)
        self.assertEqual(split_sql('-- comment only', 'x.sql'), ())
        for value in ['SELECT 1', 'SELECT 1; /* unclosed', "SELECT 'unclosed;"]:
            with self.assertRaises(InvalidInput):
                split_sql(value, 'x.sql')

    def test_duplicate_json_keys_and_strict_types(self):
        self.assertIsNotNone(load_case)
        self.manifest.write_text('{"format_version":1,"format_version":1}', encoding='utf-8')
        with self.assertRaises(InvalidInput):
            load_case(self.manifest, Limits())

    def test_malformed_allowance_is_controlled(self):
        self.doc['allow_changes'] = [dict(id='a', migration=[], phase='down', table='t', kind='rows_removed', columns=[], reason='synthetic')]
        self.save()
        with self.assertRaises(InvalidInput):
            load_case(self.manifest, Limits())
        self.doc.pop('allow_changes')
        self.doc['format_version'] = True
        self.save()
        with self.assertRaises(InvalidInput):
            load_case(self.manifest, Limits())

    def test_reused_sql_is_read_once_and_execution_is_still_counted(self):
        (self.root / 'noop.sql').write_text('SELECT 1;', encoding='utf-8')
        self.doc['migrations'] = [dict(id='one', up='noop.sql', down='noop.sql')]
        self.save()
        value = load_case(self.manifest, Limits())
        self.assertEqual([s.label for s in value.sources], ['case.json', 'schema.sql', 'seed.sql', 'noop.sql'])
        with self.assertRaises(InvalidInput):
            load_case(self.manifest, Limits(executed_statements=4))

    @unittest.skipUnless(os.name == 'nt', 'Windows junction test')
    def test_junction_input_is_rejected(self):
        target = self.root / 'outside'
        target.mkdir()
        (target / 'schema.sql').write_text('CREATE TABLE external(x);', encoding='utf-8')
        link = self.root / 'linked'
        subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(target)], check=True, capture_output=True)
        self.addCleanup(lambda: link.rmdir() if link.exists() else None)
        self.doc['schema'] = 'linked/schema.sql'
        self.save()
        with self.assertRaises(InvalidInput):
            load_case(self.manifest, Limits())


if __name__ == '__main__':
    unittest.main()
