from dataclasses import replace
import importlib.util
import unittest
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

from migreplay.model import AssertionSpec, ExecutionFailed, Limits
from migreplay.policy import Budget, ConnectionGuard, RunContext
from tests import test_comparison as helpers

fixture = helpers.fixture


class AssertionTests(unittest.TestCase):
    execute = helpers.ComparisonTests.execute
    def test_assertions_read_only_shape_and_stage(self):
        cases = [('SELECT 1', 'passed', None), ('SELECT 0', 'failed', 'assertion_false'), ('SELECT 2', 'failed', 'assertion_shape'), ('SELECT NULL', 'failed', 'assertion_shape'), ("SELECT '1'", 'failed', 'assertion_shape'), ('SELECT 1 WHERE 0', 'failed', 'assertion_shape'), ('SELECT 1 UNION ALL SELECT 1', 'failed', 'assertion_shape'), ('SELECT 1,1', 'failed', 'assertion_shape'), ('SELECT * FROM missing', 'error', 'assertion_error'), ('DELETE FROM t', 'error', 'policy_denied'), ("SELECT load_extension('synthetic')", 'error', 'policy_denied'), ('SELECT 1e999', 'error', 'unsupported_value')]
        for sql, status, code in cases:
            with self.subTest(sql=sql):
                value = fixture(up='SELECT 1;', down='SELECT 1;', assertions=(AssertionSpec('invariant', ('baseline', 'down:001'), sql, 1),))
                guard, trace, report = self.execute(value)
                result = trace.stages[0].assertions[0]
                self.assertEqual((result.status, result.code), (status, code))
                self.assertEqual(trace.complete, status != 'error')
                self.assertEqual(trace.stages[0].status, 'committed')
                self.assertEqual(report.assertions[-1]['status'], 'not_started' if status == 'error' else status)
                self.assertEqual(guard.execute('SELECT count(*) FROM t').fetchone(), (1,))

    def test_two_foreign_key_constraints_have_safe_grouped_details(self):
        value = fixture(schema='CREATE TABLE p(id PRIMARY KEY); CREATE TABLE t(id PRIMARY KEY,a REFERENCES p(id) DEFERRABLE INITIALLY DEFERRED,b REFERENCES p(id) DEFERRABLE INITIALLY DEFERRED);', seed='INSERT INTO p VALUES(1);', up='INSERT INTO t VALUES(10,8,9),(20,8,9);', down='SELECT 1;')
        guard, trace, report = self.execute(value)
        self.assertEqual(trace.stages[1].status, 'rolled_back')
        self.assertEqual(guard.execute('SELECT count(*) FROM t').fetchone(), (0,))
        checks = trace.stages[1].checks
        self.assertEqual([(c.kind, c.violations) for c in checks], [('foreign_key', 4), ('integrity', 0)])
        self.assertEqual([(d.table, d.constraint_index, d.count) for d in checks[0].details], [('t', 0, 2), ('t', 1, 2)])
        details = report._public_model['stages'][1]['checks'][0]['details']
        self.assertEqual(set(details[0]), {'table', 'constraint_index', 'count'})
        self.assertEqual(report.summary['exit_code'], 3)

    def test_controlled_connection_identity_lifetime_and_modes(self):
        from migreplay import policy
        self.assertTrue(hasattr(policy, '_controlled_guard'), 'controlled connection adapter is missing')
        budget = Budget(RunContext.create(Limits()))
        with sqlite3.connect(':memory:') as conn:
            with self.assertRaises(ExecutionFailed):
                policy._controlled_guard(conn, budget)
        with tempfile.TemporaryDirectory() as name:
            guard = ConnectionGuard(Path(name) / 'run.sqlite', budget)
            try:
                self.assertIs(policy._controlled_guard(guard.conn, budget), guard)
                with self.assertRaises(ExecutionFailed):
                    policy._controlled_guard(guard.conn, Budget(budget.context))
                guard.execute('CREATE TABLE t(x);')
                from migreplay.assertions import evaluate_assertions
                result = evaluate_assertions(guard.conn, (AssertionSpec('denied', ('baseline',), 'DELETE FROM t', 1),), 'baseline', budget)
                self.assertEqual(result[0].code, 'policy_denied')
                guard.execute('INSERT INTO t VALUES(1);')
                self.assertEqual(guard.execute('SELECT count(*) FROM t').fetchone(), (1,))
            finally:
                guard.close()
            with self.assertRaises(ExecutionFailed):
                policy._controlled_guard(guard.conn, budget)
            before = len(policy._CONNECTIONS)
            with patch.object(ConnectionGuard, 'execute', side_effect=ExecutionFailed('execution_failed')):
                with self.assertRaises(ExecutionFailed):
                    ConnectionGuard(Path(name) / 'failed.sqlite', budget)
            self.assertEqual(len(policy._CONNECTIONS), before)
