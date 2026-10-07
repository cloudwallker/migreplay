import sqlite3
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

try:
    from migreplay.policy import Budget, ConnectionGuard, RunContext
except ImportError:
    Budget = ConnectionGuard = RunContext = None
from migreplay.model import Limits, LimitExceeded, PolicyDenied


class PolicyTests(unittest.TestCase):
    def guard(self, limits=None):
        self.assertIsNotNone(ConnectionGuard, 'restricted SQLite connection is not implemented')
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        limits = limits or Limits()
        guard = ConnectionGuard(Path(tmp.name) / 'run.sqlite', Budget(RunContext.create(limits)))
        self.addCleanup(guard.close)
        return guard

    def test_authorizer_denies_escape(self):
        guard = self.guard()
        guard.execute('CREATE TABLE t(x);')
        dangerous = ["ATTACH ':memory:' AS other;", "VACUUM INTO 'escaped.sqlite';", "SELECT load_extension('x');", "SELECT readfile('x');", "SELECT writefile('x','y');", 'PRAGMA user_version;', 'SELECT * FROM pragma_table_info(\'t\');', "UPDATE sqlite_schema SET sql='x';", 'BEGIN;', 'SAVEPOINT x;', 'CREATE VIRTUAL TABLE v USING fts5(x);', 'CREATE TEMP TABLE tmp(x);', "CREATE TRIGGER evil AFTER INSERT ON t BEGIN SELECT writefile('x','y'); END;"]
        for sql in dangerous:
            with self.subTest(sql=sql.split()[0]), self.assertRaises(PolicyDenied):
                guard.execute(sql)
        with guard.mode('readonly'):
            with self.assertRaises(PolicyDenied):
                guard.execute('INSERT INTO t VALUES(1);')
        self.assertFalse((guard.path.parent / 'escaped.sqlite').exists())

    def test_supported_ddl_and_trigger(self):
        guard = self.guard()
        for sql in ['CREATE TABLE t(x);', 'CREATE TABLE audit(x);', 'CREATE INDEX ix ON t(x);', 'CREATE VIEW v AS SELECT x FROM t;', 'CREATE TRIGGER tr AFTER INSERT ON t BEGIN INSERT INTO audit VALUES(new.x); UPDATE t SET x=x+1; END;', 'INSERT INTO t VALUES(4);', 'ALTER TABLE t ADD COLUMN y;', 'DROP VIEW v;', 'DROP INDEX ix;', 'DROP TRIGGER tr;']:
            guard.execute(sql)
        self.assertEqual(guard.execute('SELECT x FROM t;').fetchall(), [(5,)])
        self.assertEqual(guard.execute('SELECT x FROM audit;').fetchall(), [(4,)])
        guard.execute('ALTER TABLE t DROP COLUMN y;')
        guard.execute('DROP TABLE audit;')
        self.assertIn(sqlite3.SQLITE_CREATE_TABLE, guard.actions)

    def test_large_row_preserves_each_value_limit(self):
        guard = self.guard()
        guard.execute('CREATE TABLE t(a BLOB, b BLOB);')
        guard.execute('INSERT INTO t VALUES(zeroblob(600000), zeroblob(600000));')
        rows = guard.execute('SELECT a,b FROM t;').fetchall()
        self.assertEqual([(len(a), len(b)) for a, b in rows], [(600000, 600000)])
        self.assertEqual(len(guard.execute('SELECT zeroblob(1048576);').fetchone()[0]), 1048576)
        for sql in ['SELECT zeroblob(1048577);', "SELECT printf('%.*c', 1048577, 'x');"]:
            with self.subTest(kind=sql.split()[1]), self.assertRaises(LimitExceeded) as error:
                guard.execute(sql).fetchall()
            self.assertEqual(error.exception.code, 'value_limit')
        self.assertEqual(len(guard.execute('SELECT ?;', ('é' * 524288,)).fetchone()[0]), 524288)
        with self.assertRaises(LimitExceeded):
            guard.execute('SELECT ?;', ('é' * 524289,)).fetchone()
        # SQLite must still bound allocations before Python sees the value.
        with self.assertRaises(LimitExceeded):
            guard.execute('SELECT zeroblob(67108865);').fetchall()

    def test_dangerous_names_are_valid_plain_identifiers(self):
        guard = self.guard()
        for name in ('readfile', 'pragma_demo', 'writefile', 'load_extension'):
            for quoted in (False, True):
                table = '"' + name + '"' if quoted else name
                column = '"' + name + '"' if quoted else name
                with self.subTest(name=name, quoted=quoted):
                    guard.execute(f'CREATE TABLE {table}({column} TEXT);')
                    guard.execute(f"INSERT INTO {table}({column}) VALUES('synthetic');")
                    guard.execute(f"UPDATE {table} SET {column}='updated';")
                    self.assertEqual(guard.execute(f'SELECT {column} FROM {table};').fetchall(), [('updated',)])
                    guard.execute(f'DROP TABLE {table};')
        guard.execute('CREATE TABLE IF NOT EXISTS main.readfile(x);')
        guard.execute('INSERT INTO main.readfile(x) VALUES(1);')
        guard.execute('CREATE INDEX ix ON readfile(x);')
        self.assertEqual(guard.execute('WITH pragma_demo(x) AS (SELECT 7) SELECT x FROM pragma_demo;').fetchall(), [(7,)])

    def test_dangerous_function_calls_still_denied(self):
        guard = self.guard()
        guard.execute('CREATE TABLE t(x);')
        for sql in ["SELECT readfile /* comment */ ('synthetic');", "SELECT \"writefile\"('synthetic', 'x');", "SELECT `load_extension`('synthetic');", "SELECT * FROM \"pragma_table_info\"('t');", "CREATE VIEW v AS SELECT readfile('synthetic');", "CREATE TRIGGER bad AFTER INSERT ON t BEGIN SELECT readfile('synthetic'); END;"]:
            with self.subTest(sql=sql.split()[0]), self.assertRaises(PolicyDenied):
                guard.execute(sql)
        # Disable only early diagnostics to prove real SQLite callbacks retain
        # enforcement for capabilities that the engine can resolve.
        with patch('migreplay.policy._dangerous_calls', return_value=False):
            for sql in ["SELECT load_extension('synthetic');", "SELECT * FROM pragma_table_info('t');"]:
                with self.assertRaises(PolicyDenied):
                    guard.execute(sql)

    def test_view_column_lists_are_declarations(self):
        guard = self.guard()
        for name in ('readfile', 'pragma_demo'):
            for quoted in (False, True):
                for qualified in (False, True):
                    for if_not_exists in (False, True):
                        identifier = '"' + name + '"' if quoted else name
                        reference = 'main.' + identifier if qualified else identifier
                        modifier = 'IF NOT EXISTS ' if if_not_exists else ''
                        with self.subTest(name=name, quoted=quoted, qualified=qualified, if_not_exists=if_not_exists):
                            guard.execute(f'CREATE VIEW {modifier}{reference}(x) AS SELECT 1;')
                            self.assertEqual(guard.execute(f'SELECT x FROM {reference};').fetchall(), [(1,)])
                            guard.execute(f'DROP VIEW {reference};')

    def test_view_declaration_does_not_exempt_body_calls(self):
        guard = self.guard()
        guard.execute('CREATE TABLE t(x);')
        for sql in [
            'CREATE VIEW "readfile"(x) AS SELECT readfile(\'synthetic\');',
            'CREATE VIEW IF NOT EXISTS main."pragma_demo"(x) AS SELECT name FROM pragma_table_info(\'t\');',
            'CREATE TRIGGER "pragma_demo" AFTER INSERT ON t BEGIN SELECT writefile(\'synthetic\',\'x\'); END;',
        ]:
            with self.subTest(statement=sql.split()[1]), self.assertRaises(PolicyDenied):
                guard.execute(sql)

    def test_budget_fail_closed(self):
        guard = self.guard(Limits(statement_seconds=0.025, value_bytes=256))
        with self.assertRaises(LimitExceeded):
            guard.execute('SELECT randomblob(4096);').fetchall()
        with self.assertRaises(LimitExceeded):
            guard.execute('WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n) SELECT sum(x) FROM n;').fetchall()

    def test_separate_snapshot_and_ipc_accounts(self):
        self.assertIsNotNone(Budget, 'shared budget is not implemented')
        context = RunContext.create(Limits(snapshot_bytes=10, snapshot_bytes_total=16, ipc_bytes_total=16))
        budget = Budget(context)
        budget.begin_snapshot()
        budget.charge_snapshot(10, 0)
        budget.end_snapshot()
        budget.reserve_frame(1, 10)
        budget.verify_frame(1, 10)
        self.assertEqual((context.snapshot_bytes_total, context.ipc_bytes_total), (10, 10))
        with self.assertRaises(LimitExceeded):
            budget.reserve_frame(2, 7)
        # Sending the same bytes on another connection is a new frame charge.
        with self.assertRaises(LimitExceeded):
            budget.reserve_frame(2, 10)
        budget.begin_snapshot()
        with self.assertRaises(LimitExceeded):
            budget.charge_snapshot(7, 0)
        with self.assertRaises(Exception):
            budget.verify_frame(1, 10)

    def test_deadline_is_not_reset(self):
        self.assertIsNotNone(Budget)
        context = RunContext.create(Limits(run_seconds=0.02))
        deadline = context.deadline
        budget = Budget(context)
        self.assertIs(budget.context, context)
        self.assertEqual(budget.context.deadline, deadline)
        # Windows Python 3.12 may use a 15.625ms GetTickCount64 clock:
        # a precise 30ms sleep can advance this clock by only one tick.
        # Wait for the same original deadline, without granting a new budget.
        wait_limit = time.perf_counter() + 1
        while time.monotonic() < deadline:
            self.assertLess(time.perf_counter(), wait_limit, 'monotonic clock did not reach its original deadline')
            time.sleep(0.001)
        with self.assertRaises(LimitExceeded):
            budget.check_deadline()


if __name__ == '__main__':
    unittest.main()
