from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from migreplay.model import Allowance, AssertionSpec, Case, Diagnostic, ExecutionFailed, Limits, Migration
from migreplay.policy import Budget, ConnectionGuard, RunContext
from migreplay.sql import split_sql


def fixture(schema='CREATE TABLE t(id INTEGER PRIMARY KEY, body TEXT);', seed="INSERT INTO t VALUES(1,'a');", up='ALTER TABLE t DROP COLUMN body;', down='ALTER TABLE t ADD COLUMN body TEXT;', **kwargs):
    return Case(split_sql(schema, 'schema.sql'), split_sql(seed, 'seed.sql'), (Migration('001', split_sql(up, '001.up.sql'), split_sql(down, '001.down.sql')),), **kwargs)


class ComparisonTests(unittest.TestCase):
    def execute(self, value, limits=None):
        self.assertIsNotNone(importlib.util.find_spec('migreplay.compare'), 'restoration producer is missing')
        from migreplay.worker import execute_trace
        from migreplay.compare import analyze_trace
        limits = limits or Limits()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        guard = ConnectionGuard(Path(tmp.name) / 'run.sqlite', Budget(RunContext.create(limits)))
        self.addCleanup(guard.close)
        trace = execute_trace(guard, value)
        report = analyze_trace(value, trace, limits)
        return guard, trace, report

    def test_snapshot_keys_and_multisets(self):
        _, trace, report = self.execute(fixture(schema='CREATE TABLE t(body);', seed="INSERT INTO t VALUES('x'),('x'),(NULL);", up="DELETE FROM t WHERE rowid=(SELECT min(rowid) FROM t WHERE body='x');", down='SELECT 1;'))
        table = report.comparisons[0]['table_results'][0]
        self.assertEqual((table['removed_count'], table['added_count'], table['changed_count']), (1, 0, None))
        self.assertEqual(table['identity_mode'], 'multiset')
        self.assertEqual(report.summary['exit_code'], 1)
        _, trace, report = self.execute(fixture(schema='CREATE TABLE t(id TEXT PRIMARY KEY, body);', seed="INSERT INTO t VALUES(NULL,'a'),(NULL,'b');", up='SELECT 1;', down='SELECT 1;'))
        self.assertEqual(trace.stages[0].snapshots[-1].tables['t'].row_count, 2)
        self.assertEqual(report.summary['exit_code'], 0)
        self.assertTrue(any(f.kind == 'identity_unreliable' for f in report.findings))
        _, _, report = self.execute(fixture(schema='CREATE TABLE t(a TEXT,b INTEGER,body,PRIMARY KEY(a,b)) WITHOUT ROWID;', seed="INSERT INTO t VALUES('x',1,'a'),('x',2,'b');", up='SELECT * FROM t ORDER BY b DESC;', down='SELECT 1;'))
        table = report.comparisons[0]['table_results'][0]
        self.assertEqual(table['before']['key_columns'], ['a', 'b'])
        self.assertEqual(report.summary['status'], 'passed')

    def test_generated_blob_index_trigger_sequence(self):
        schema = "CREATE TABLE t(id INTEGER PRIMARY KEY AUTOINCREMENT,b BLOB,g INT GENERATED ALWAYS AS(length(b)) STORED); CREATE INDEX ix ON t(g); CREATE TABLE audit(x); CREATE TRIGGER tr AFTER INSERT ON t BEGIN INSERT INTO audit VALUES('a;b'); END;"
        _, trace, report = self.execute(fixture(schema=schema, seed="INSERT INTO t(b) VALUES(x'00ff');", up='SELECT 1;', down='SELECT 1;'))
        table = trace.stages[0].snapshots[-1].tables['t']
        self.assertEqual(set(table.column_digests), {'id', 'b', 'g'})
        self.assertIsNotNone(table.sequence_digest)
        self.assertEqual(report.summary['exit_code'], 0)
        _, _, changed = self.execute(fixture(schema=schema, seed="INSERT INTO t(b) VALUES(x'00ff');", up='DROP INDEX IF EXISTS ix;', down='SELECT 1;'))
        self.assertEqual(changed.summary['exit_code'], 1)

    def test_schema_and_trigger_changes_are_not_restored(self):
        for sql in ('CREATE TABLE t(body TEXT,id INTEGER PRIMARY KEY);', 'CREATE TABLE t(id INTEGER PRIMARY KEY,body TEXT DEFAULT 0);', 'CREATE TABLE t(id INTEGER PRIMARY KEY,body TEXT CHECK(body IS NOT NULL));'):
            _, _, report = self.execute(fixture(up='ALTER TABLE t RENAME TO old; ' + sql + ' INSERT INTO t(id,body) SELECT id,body FROM old; DROP TABLE old;', down='SELECT 1;'))
            self.assertFalse(report.comparisons[0]['table_results'][0]['schema_restored'])
            self.assertEqual(report.summary['exit_code'], 1)
        _, _, report = self.execute(fixture(schema="CREATE TABLE t(id INTEGER PRIMARY KEY,body TEXT); CREATE TRIGGER tr AFTER UPDATE OF body ON t BEGIN UPDATE t SET body='original' WHERE id=new.id; END;", up="DROP TRIGGER tr; CREATE TRIGGER tr AFTER UPDATE OF body ON t BEGIN UPDATE t SET body='changed' WHERE id=new.id; END;", down='SELECT 1;'))
        self.assertEqual(report.summary['exit_code'], 1)

    def test_final_only_and_first_observed(self):
        value = fixture()
        _, trace, report = self.execute(value)
        comparison = report.comparisons[0]
        self.assertEqual(comparison['first_observed_snapshot_id'], 'up:001/statement/1')
        self.assertEqual(comparison['first_observed_change']['statement'], 1)
        self.assertEqual(comparison['first_failed_restoration']['file'], '001.down.sql')
        self.assertEqual(comparison['after_snapshot_id'], 'down:001/final')
        from migreplay.compare import analyze_trace
        trace.stages[2].final_snapshot_id = None
        report = analyze_trace(value, trace, Limits())
        self.assertEqual(report.comparisons[0]['status'], 'incomparable')
        self.assertIsNone(report.comparisons[0]['first_failed_restoration'])
        self.assertEqual(report.summary['exit_code'], 3)

    def test_row_findings_locate_distinct_key_changes(self):
        value = fixture(seed="INSERT INTO t VALUES(1,'one'),(2,'two');",
            up="UPDATE t SET body='temporary' WHERE id=1; UPDATE t SET body='one' WHERE id=1; "
               "INSERT INTO t VALUES(4,'transient'); DELETE FROM t WHERE id=4; "
               "DELETE FROM t WHERE id=2; INSERT OR IGNORE INTO t VALUES(3,'three');",
            down='SELECT 1;')
        _, _, report = self.execute(value)
        self.assertEqual(report.comparisons[0]['first_observed_snapshot_id'], 'up:001/statement/5')
        locations = {f['kind']: f['source']['statement'] for f in report._public_model['findings']
                     if f['stage_id'] == 'down:001'}
        self.assertEqual(locations, {'rows_removed': 5, 'rows_added': 6, 'keys_changed': 6})

    def test_multiset_findings_locate_changes_in_duplicate_counts(self):
        value = fixture(schema='CREATE TABLE t(body);', seed="INSERT INTO t VALUES('x'),('x');",
            up="INSERT INTO t VALUES('transient'); DELETE FROM t WHERE body='transient'; "
               "DELETE FROM t WHERE rowid=(SELECT min(rowid) FROM t WHERE body='x'); "
               "INSERT INTO t VALUES('y');", down='SELECT 1;')
        _, _, report = self.execute(value)
        self.assertEqual(report.comparisons[0]['first_observed_snapshot_id'], 'up:001/statement/3')
        locations = {f['kind']: f['source']['statement'] for f in report._public_model['findings']
                     if f['stage_id'] == 'down:001'}
        self.assertEqual(locations, {'rows_removed': 3, 'rows_added': 4})

    def test_row_locations_skip_missing_table_and_unreliable_key(self):
        value = fixture(schema='CREATE TABLE "t"(id,body);', keys={'t': ('id',)},
            seed="INSERT INTO t VALUES(1,'one'),(2,'two');",
            up='ALTER TABLE t RENAME TO holding; ALTER TABLE holding RENAME TO t; '
               'UPDATE t SET id=NULL WHERE id=2; UPDATE t SET id=2 WHERE id IS NULL; '
               'DELETE FROM t WHERE id=2;', down='SELECT 1;')
        _, _, report = self.execute(value)
        removed = next(f for f in report.findings if f.kind == 'rows_removed' and f.stage_id == 'down:001')
        self.assertEqual(removed.source.statement, 5)
        self.assertEqual(report.comparisons[0]['first_observed_snapshot_id'], 'up:001/statement/5')

    def test_allowance_exact_scope(self):
        allowance = Allowance('body', '001', 'down', 't', 'value_changed', ('body',), 'Synthetic body reset is intentional.')
        value = fixture(allowances=(allowance,))
        _, _, report = self.execute(value)
        self.assertEqual(report.summary['status'], 'expected_irreversible')
        self.assertEqual(report.summary['expected_count'], 1)
        self.assertEqual(report.summary['exit_code'], 0)
        self.assertFalse(report.allowance_usage[0]['unused'])
        for change in (dict(phase='reup'), dict(migration='other'), dict(columns=('id',))):
            _, _, bad = self.execute(replace(value, allowances=(replace(allowance, **change),)))
            self.assertEqual(bad.summary['exit_code'], 1)
            self.assertTrue(bad.allowance_usage[0]['unused'])
        _, _, deleted = self.execute(replace(value, migrations=(replace(value.migrations[0], down=split_sql('ALTER TABLE t ADD COLUMN body TEXT; DELETE FROM t;', '001.down.sql')),)))
        self.assertEqual(deleted.summary['exit_code'], 1)
        _, _, false = self.execute(replace(value, assertions=(AssertionSpec('false', ('down:001',), 'SELECT 0', 1),)))
        self.assertEqual(false.summary['exit_code'], 1)
        self.assertEqual(false.summary['assertion_failed_count'], 1)
        _, _, failed = self.execute(replace(value, migrations=(replace(value.migrations[0], down=split_sql('SELECT * FROM missing;', '001.down.sql')),)))
        self.assertEqual(failed.summary['exit_code'], 3)

    def test_allowance_structural_scope(self):
        allowance = Allowance('body', '001', 'down', 't', 'column_missing', ('body',), 'Synthetic column removal.')
        value = fixture(up='SELECT 1;', down='ALTER TABLE t DROP COLUMN body;', allowances=(allowance, Allowance('repeat', '001', 'reup', 't', 'column_missing', ('body',), 'Synthetic column removal.')))
        _, _, report = self.execute(value)
        self.assertEqual(report.summary['exit_code'], 0)
        indexed = replace(value, schema=split_sql('CREATE TABLE t(id INTEGER PRIMARY KEY,body TEXT); CREATE INDEX ix ON t(id);', 'schema.sql'), migrations=(replace(value.migrations[0], down=split_sql('ALTER TABLE t DROP COLUMN body; DROP INDEX ix;', '001.down.sql')),))
        _, _, bad = self.execute(indexed)
        self.assertEqual(bad.summary['exit_code'], 1)
        self.assertTrue(any(f.kind == 'schema_not_restored' and f.allowance_id is None for f in bad.findings))
        rows = (Allowance('add', '001', 'down', 't', 'rows_added', (), 'Synthetic addition.'), Allowance('remove', '001', 'down', 't', 'rows_removed', (), 'Synthetic removal.'))
        _, _, changed = self.execute(fixture(up='UPDATE t SET id=2;', down='SELECT 1;', allowances=rows))
        self.assertTrue(any(f.kind == 'keys_changed' and f.allowance_id is None for f in changed.findings))
        self.assertEqual(changed.summary['exit_code'], 1)

    def test_multicolumn_allowance_requires_the_complete_actual_set(self):
        schema = 'CREATE TABLE t(id INTEGER PRIMARY KEY,body TEXT,title TEXT);'
        seed = "INSERT INTO t VALUES(1,'body','title');"
        for kind, up, down in [('value_changed', "UPDATE t SET body='new body',title='new title';", 'SELECT 1;'), ('column_missing', 'SELECT 1;', 'ALTER TABLE t DROP COLUMN body; ALTER TABLE t DROP COLUMN title;')]:
            for columns, wanted in [(('title', 'body'), 0), (('body',), 1), (('body', 'title', 'id'), 1)]:
                with self.subTest(kind=kind, columns=columns):
                    rules = tuple(Allowance(phase, '001', phase, 't', kind, columns, 'Synthetic two-column change.') for phase in ('down', 'reup'))
                    _, _, report = self.execute(fixture(schema=schema, seed=seed, up=up, down=down, allowances=rules))
                    self.assertEqual(report.summary['exit_code'], wanted)
                    finding = next(f for f in report.findings if f.kind == kind)
                    self.assertEqual(finding.columns, ('body', 'title'))
                    self.assertEqual(finding.allowance_id is not None, wanted == 0)
            separate = tuple(Allowance(column, '001', 'down', 't', kind, (column,), 'Synthetic separate declaration.') for column in ('body', 'title'))
            _, _, report = self.execute(fixture(schema=schema, seed=seed, up=up, down=down, allowances=separate))
            self.assertEqual(report.summary['exit_code'], 1)
            self.assertTrue(all(u['unused'] for u in report.allowance_usage))

    def test_two_migrations_use_independent_baselines(self):
        value = fixture(up='ALTER TABLE t ADD COLUMN extra;', down='ALTER TABLE t DROP COLUMN extra;')
        value = replace(value, migrations=value.migrations + (Migration('002', split_sql('ALTER TABLE t ADD COLUMN more;', '002.up.sql'), split_sql('ALTER TABLE t DROP COLUMN more;', '002.down.sql')),))
        _, trace, report = self.execute(value)
        self.assertEqual([(c['before_stage_id'], c['after_stage_id']) for c in report.comparisons], [('up:001', 'down:002'), ('baseline', 'down:001'), ('up:001', 'reup:001'), ('up:002', 'reup:002')])
        self.assertEqual(report.summary['status'], 'passed')

    def test_context_and_public_projection(self):
        value = fixture(seed="INSERT INTO t VALUES(1,'PRIVATE-SYNTHETIC-ROW');")
        _, trace, report = self.execute(value)
        from migreplay.compare import analyze_trace
        with self.assertRaises(ExecutionFailed):
            analyze_trace(value, trace, Limits(run_seconds=10))
        with self.assertRaises(ExecutionFailed):
            analyze_trace(replace(value, keys={'t': ('body',)}), trace, Limits())
        public = report._public_model
        self.assertEqual(set(public), set('schema_version runtime sources limits stages comparisons assertions findings allowance_usage summary diagnostics'.split()))
        text = json.dumps(public)
        self.assertNotIn('PRIVATE-SYNTHETIC-ROW', text)
        self.assertNotIn('CREATE TABLE', text)
        self.assertNotIn('_rows', text)
        self.assertEqual(public['stages'][0]['snapshots'][0]['tables']['t']['row_count'], 0)
        self.assertEqual(set(public['runtime']), {'product', 'python', 'sqlite'})
        snapshot = public['stages'][0]['snapshots'][0]
        self.assertEqual(set(snapshot), set('id stage_id source state committed schema_digest tables'.split()))
        self.assertEqual(set(snapshot['tables']['t']), set('schema_digest row_count identity_mode key_columns key_digest row_digest column_digests null_counts sequence_digest'.split()))
        self.assertEqual(set(public['findings'][0]['evidence']), set('comparison_id before_digest after_digest before_count after_count snapshot_id'.split()))
        self.assertEqual(len({f['id'] for f in public['findings']}), len(public['findings']))
        self.assertTrue(all(s['committed'] == (s['state'] == 'committed') for stage in public['stages'] for s in stage['snapshots']))

    def test_failure_factory(self):
        self.assertIsNotNone(importlib.util.find_spec('migreplay.status'), 'safe failure factory is missing')
        from migreplay.status import failure_report, failure_summary, exit_code
        from migreplay.model import CODES
        for code in CODES:
            report = failure_report(Diagnostic(code), {}, Limits())
            self.assertEqual(report.summary, vars(failure_summary(code)))
            self.assertEqual(exit_code(report), report.summary['exit_code'])
        report = failure_report(Diagnostic('invalid_input'), {}, Limits())
        self.assertEqual(report.summary, dict(status='invalid_input', exit_code=2, unexpected_count=0, expected_count=0, assertion_failed_count=0, complete=False))
        self.assertEqual(report.stages, ())
        self.assertEqual(report.findings[0].kind, 'invalid_input')
        self.assertIsNone(report.findings[0].count)
        _, _, partial = self.execute(fixture())
        failed = failure_report(Diagnostic('worker_failed'), {}, Limits(), partial=partial)
        self.assertEqual(failed.stages, partial.stages)
        self.assertEqual(failed.summary['exit_code'], 3)
        self.assertFalse(failed.summary['complete'])

    def test_snapshot_budget_value_limits_and_observed_types(self):
        _, trace, report = self.execute(fixture(schema='CREATE TABLE t(id INTEGER PRIMARY KEY,body);', seed="INSERT INTO t VALUES(1,1),(2,1.0),(3,'1'),(4,x'00ff');", up='SELECT 1;', down='SELECT 1;'))
        table = trace.stages[0].snapshots[-1].tables['t']
        self.assertEqual(table.row_count, 4)
        self.assertEqual(len({r[1] for r in table._rows}), 4)
        self.assertEqual(report.summary['exit_code'], 0)
        _, trace, report = self.execute(fixture(schema='CREATE TABLE t(a BLOB,b BLOB);', seed='INSERT INTO t VALUES(zeroblob(600000),zeroblob(600000));', up='SELECT 1;', down='SELECT 1;'))
        self.assertTrue(trace.complete)
        self.assertEqual(report.summary['exit_code'], 0)
        for limits, expected in [(Limits(snapshot_bytes_total=200), 'snapshot_limit'), (Limits(rows_per_table=1), 'row_limit'), (Limits(value_bytes=64), 'value_limit')]:
            value = fixture(seed="INSERT INTO t VALUES(1,'a'),(2,printf('%100s','a'));")
            _, trace, report = self.execute(value, limits)
            self.assertIn(expected, [d.code for d in trace.diagnostics])
            self.assertEqual(report.summary['exit_code'], 3)
            self.assertIsNone(trace.stages[0].final_snapshot_id)

    def test_trigger_changed_rows_and_sequence_are_evidence(self):
        value = fixture(schema="CREATE TABLE t(id INTEGER PRIMARY KEY,body TEXT); CREATE TABLE audit(id INTEGER PRIMARY KEY,n INTEGER); CREATE TRIGGER tr AFTER UPDATE ON t BEGIN UPDATE audit SET n=n+1; END;", seed="INSERT INTO t VALUES(1,'a'); INSERT INTO audit VALUES(1,0);", up="UPDATE t SET body='b';", down="UPDATE t SET body='a';")
        _, _, report = self.execute(value)
        tables = {t['table']: t for t in report.comparisons[0]['table_results']}
        self.assertEqual(tables['t']['changed_count'], 0)
        self.assertEqual(tables['audit']['changed_count'], 1)
        self.assertEqual(report.summary['exit_code'], 1)
        value = fixture(schema='CREATE TABLE t(id INTEGER PRIMARY KEY AUTOINCREMENT,body TEXT);', up="INSERT INTO t(body) VALUES('extra'); DELETE FROM t WHERE id=2;", down='SELECT 1;')
        _, _, report = self.execute(value)
        self.assertEqual(report.comparisons[0]['table_results'][0]['changed_count'], 0)
        self.assertEqual(report.summary['exit_code'], 1)

    def test_missing_table_and_common_column_are_independent_findings(self):
        allowed = (Allowance('missing', '001', 'down', 't', 'table_missing', (), 'Synthetic deletion.'), Allowance('again', '001', 'reup', 't', 'table_missing', (), 'Synthetic deletion.'))
        _, _, report = self.execute(fixture(up='SELECT 1;', down='DROP TABLE t;', allowances=allowed))
        self.assertEqual(report.summary['exit_code'], 0)
        missing = report.comparisons[0]['table_results'][0]
        self.assertIsNone(missing['after'])
        self.assertIsNone(missing['removed_count'])
        self.assertFalse(missing['schema_restored'])
        allowed = (Allowance('missing', '001', 'down', 't', 'column_missing', ('extra',), 'Synthetic deletion.'),)
        _, _, report = self.execute(fixture(schema='CREATE TABLE t(id INTEGER PRIMARY KEY,body TEXT,extra TEXT);', seed="INSERT INTO t VALUES(1,'a','x');", up='SELECT 1;', down="ALTER TABLE t DROP COLUMN extra; UPDATE t SET body='changed';", allowances=allowed))
        comparison = report.comparisons[0]['table_results'][0]
        self.assertEqual(comparison['column_changed_counts'], {'body': 1, 'extra': None, 'id': 0})
        self.assertTrue(any(f.kind == 'value_changed' and f.allowance_id is None for f in report.findings))
        self.assertEqual(report.summary['exit_code'], 1)

    def test_known_missing_final_never_uses_a_statement(self):
        value = fixture(up='SELECT 1;', down='ALTER TABLE t DROP COLUMN body; SELECT * FROM missing;')
        _, trace, report = self.execute(value)
        self.assertEqual(trace.stages[2].snapshots[0].state, 'rolled_back')
        self.assertFalse(trace.stages[2].snapshots[0].committed)
        self.assertIsNone(report.comparisons[0]['after_snapshot_id'])
        self.assertIsNone(report.comparisons[0]['first_failed_restoration'])
        self.assertEqual(report.summary['exit_code'], 3)

    def test_unreliable_explicit_key_is_visible_without_losing_rows(self):
        _, trace, report = self.execute(fixture(schema='CREATE TABLE t(id,body);', seed="INSERT INTO t VALUES(1,'a'),(1,'b');", up='SELECT 1;', down='SELECT 1;', keys={'t': ('id',)}))
        self.assertEqual(trace.stages[0].snapshots[-1].tables['t'].row_count, 2)
        self.assertEqual(trace.stages[0].snapshots[-1].tables['t'].identity_mode, 'multiset')
        self.assertTrue(any(f.kind == 'identity_unreliable' for f in report.findings))
        self.assertEqual(report.summary['status'], 'passed')

    def test_reliable_key_definition_change_and_view_change(self):
        _, _, report = self.execute(fixture(schema='CREATE TABLE t(id INTEGER PRIMARY KEY,body TEXT UNIQUE);', up='ALTER TABLE t RENAME TO old; CREATE TABLE t(id INTEGER,body TEXT PRIMARY KEY); INSERT INTO t SELECT * FROM old; DROP TABLE old;', down='SELECT 1;'))
        table = report.comparisons[0]['table_results'][0]
        self.assertIn('key_definition_changed', table['incomparable_reasons'])
        self.assertIsNone(table['changed_count'])
        self.assertEqual(report.summary['exit_code'], 1)
        _, _, report = self.execute(fixture(schema='CREATE TABLE t(id INTEGER PRIMARY KEY,body TEXT); CREATE VIEW v AS SELECT id FROM t;', up='DROP VIEW v; CREATE VIEW v AS SELECT body FROM t;', down='SELECT 1;'))
        self.assertEqual(report.summary['exit_code'], 1)

    def test_view_trigger_add_remove_and_definition_require_exact_restoration(self):
        base = 'CREATE TABLE t(id INTEGER PRIMARY KEY,body TEXT); CREATE VIEW v AS SELECT id,body FROM t;'
        original = 'CREATE TRIGGER vi INSTEAD OF INSERT ON v BEGIN INSERT INTO t VALUES(new.id,new.body); END;'
        changed = "CREATE TRIGGER vi INSTEAD OF INSERT ON v BEGIN INSERT INTO t VALUES(new.id,'synthetic replacement'); END;"
        variants = [
            ('added', base, original.replace('CREATE TRIGGER', 'CREATE TRIGGER IF NOT EXISTS'), 'DROP TRIGGER vi;'),
            ('removed', base + original, 'DROP TRIGGER IF EXISTS vi;', original),
            ('changed', base + original, 'DROP TRIGGER IF EXISTS vi; ' + changed, 'DROP TRIGGER vi; ' + original),
        ]
        for mode, schema, up, restoring_down in variants:
            for restored in (False, True):
                with self.subTest(mode=mode, restored=restored):
                    # Temporary ordinary-table changes must not be mistaken for
                    # the first observation of a view-trigger restoration loss.
                    replay_up = 'CREATE TABLE transient(x); DROP TABLE transient; ' + up
                    unused = Allowance('unrelated', '001', 'down', 't', 'column_missing', ('body',), 'Synthetic unrelated rule.')
                    _, trace, report = self.execute(fixture(schema=schema, up=replay_up, down=restoring_down if restored else 'SELECT 1;', allowances=(unused,)))
                    self.assertTrue(trace.complete)
                    self.assertEqual([s.status for s in trace.stages], ['committed'] * 4)
                    comparison = report.comparisons[0]
                    self.assertEqual(report.summary['exit_code'], 0 if restored else 1)
                    self.assertEqual(comparison['status'], 'restored' if restored else 'unexpected_loss')
                    self.assertEqual(report.comparisons[1]['status'], 'restored')
                    self.assertTrue(report.allowance_usage[0]['unused'])
                    table = comparison['table_results'][0]
                    self.assertTrue(table['schema_restored'])
                    self.assertEqual((table['added_count'], table['removed_count'], table['changed_count']), (0, 0, 0))
                    if restored:
                        self.assertEqual(report.findings, ())
                    else:
                        self.assertEqual(report.summary['unexpected_count'], 1)
                        finding = report.findings[0]
                        self.assertEqual((finding.kind, finding.table, finding.allowance_id), ('schema_not_restored', None, None))
                        self.assertEqual(finding.evidence['before_digest'], trace.stages[0].snapshots[-1].schema_digest)
                        self.assertEqual(finding.evidence['after_digest'], trace.stages[2].snapshots[-1].schema_digest)
                        self.assertNotEqual(finding.evidence['before_digest'], finding.evidence['after_digest'])
                        self.assertEqual(comparison['first_observed_snapshot_id'], 'up:001/statement/3')
                        self.assertEqual(comparison['first_failed_restoration']['file'], '001.down.sql')
