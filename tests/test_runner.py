from dataclasses import replace
from pathlib import Path
import tempfile
import time
import threading
import _thread
import unittest
import os
import socket
from unittest.mock import patch

from migreplay.model import Case, ExecutionFailed, Limits, Migration
from migreplay.policy import Budget, RunContext, ConnectionGuard
from migreplay.sql import split_sql
try:
    from migreplay.worker import execute_counts
    from migreplay.runner import supervise_counts, run_case, coordinate_run
except ImportError:
    execute_counts = supervise_counts = None


def case(schema='CREATE TABLE t(x);', seed='INSERT INTO t VALUES(1);', up='ALTER TABLE t ADD COLUMN y;', down='ALTER TABLE t DROP COLUMN y;'):
    return Case(split_sql(schema, 'schema.sql'), split_sql(seed, 'seed.sql'), (Migration('one', split_sql(up, 'up.sql'), split_sql(down, 'down.sql')),))


def stalled_child(*args):
    time.sleep(10)


def forged_child(control, *args):
    from migreplay.runner import CONTROL, RECEIVED
    control.sendall(CONTROL.pack(RECEIVED, 999, 123))
    time.sleep(10)


class RunnerTests(unittest.TestCase):
    def execute(self, value, limits=None):
        self.assertIsNotNone(execute_counts, 'transaction runner is not implemented')
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        guard = ConnectionGuard(Path(tmp.name) / 'run.sqlite', Budget(RunContext.create(limits or Limits())))
        self.addCleanup(guard.close)
        return guard, execute_counts(guard, value)

    def test_transactional_ddl_and_commit_failure(self):
        guard, audit = self.execute(case(up='ALTER TABLE t ADD COLUMN y; INSERT INTO missing VALUES(2);'))
        self.assertEqual([s['status'] for s in audit['stages']], ['committed', 'rolled_back', 'not_started', 'not_started'])
        self.assertEqual(guard.execute('SELECT * FROM t;').fetchall(), [(1,)])
        self.assertTrue(all(o['state'] == 'rolled_back' for o in audit['observations'] if o['stage_id'] == 'up:one'))
        self.assertIsNone(audit['stages'][1]['final_snapshot_id'])
        guard2, second = self.execute(case(schema='CREATE TABLE t(x); CREATE TABLE a(x); CREATE TRIGGER tr AFTER INSERT ON t BEGIN INSERT INTO a VALUES(new.x); END;', up='INSERT INTO t VALUES(2); INSERT INTO missing VALUES(2);'))
        self.assertEqual(guard2.execute('SELECT * FROM a;').fetchall(), [(1,)])
        deferred = case(schema='CREATE TABLE p(id PRIMARY KEY); CREATE TABLE t(x REFERENCES p(id) DEFERRABLE INITIALLY DEFERRED);', seed='', up='INSERT INTO t VALUES(9);')
        guard3, third = self.execute(deferred)
        self.assertEqual(third['stages'][1]['status'], 'rolled_back')
        self.assertEqual(guard3.execute('SELECT * FROM t;').fetchall(), [])
        self.assertEqual(third['diagnostics'][0]['code'], 'foreign_key_failed')

    def test_stage_order_and_observation_identity(self):
        value = case()
        value = replace(value, migrations=value.migrations + (Migration('two', split_sql('ALTER TABLE t ADD COLUMN z;', '2.up.sql'), split_sql('ALTER TABLE t DROP COLUMN z;', '2.down.sql')),))
        guard, audit = self.execute(value)
        self.assertEqual([s['id'] for s in audit['stages']], ['baseline', 'up:one', 'up:two', 'down:two', 'down:one', 'reup:one', 'reup:two'])
        self.assertTrue(audit['execution_complete'])
        self.assertEqual([o['id'] for o in audit['observations'][:3]], ['baseline/statement/1', 'baseline/statement/2', 'baseline/final'])
        self.assertTrue(all(o['state'] == 'committed' for o in audit['observations']))
        self.assertEqual(audit['stages'][0]['final_snapshot_id'], 'baseline/final')

    def test_budget_fail_closed(self):
        cases = [(case(seed='INSERT INTO t VALUES(1),(2),(3);'), Limits(rows_per_table=2), 'row_limit'), (case(), Limits(snapshot_bytes_total=90), 'snapshot_limit'), (case(schema='CREATE TABLE t(x,y,z);'), Limits(columns_per_table=2), 'column_limit'), (case(schema='CREATE TABLE t(x); CREATE TABLE a(x);'), Limits(tables=1), 'table_limit')]
        for value, limits, code in cases:
            with self.subTest(code=code):
                _, audit = self.execute(value, limits)
                self.assertFalse(audit['execution_complete'])
                self.assertEqual(audit['diagnostics'][0]['code'], code)

    def test_large_multivalue_rows_and_single_value_rollback(self):
        value = case(schema='CREATE TABLE t(a BLOB,b BLOB);', seed='INSERT INTO t VALUES(zeroblob(600000),zeroblob(600000));', up='SELECT 1;', down='SELECT 1;')
        guard, audit = self.execute(value)
        self.assertTrue(audit['execution_complete'])
        self.assertEqual([stage['status'] for stage in audit['stages']], ['committed'] * 4)
        self.assertEqual(guard.execute('SELECT length(a),length(b) FROM t;').fetchall(), [(600000, 600000)])
        self.assertEqual(audit['observations'][1]['tables']['t']['row_count'], 1)
        bad = replace(value, migrations=(Migration('one', split_sql('UPDATE t SET a=zeroblob(1048577);', 'up.sql'), split_sql('SELECT 1;', 'down.sql')),))
        guard, audit = self.execute(bad)
        self.assertEqual([stage['status'] for stage in audit['stages']], ['committed', 'rolled_back', 'not_started', 'not_started'])
        self.assertEqual(audit['diagnostics'][0]['code'], 'value_limit')
        self.assertEqual(guard.execute('SELECT length(a),length(b) FROM t;').fetchall(), [(600000, 600000)])
        self.assertIsNone(audit['stages'][1]['final_snapshot_id'])

    def test_new_temp_only_and_cleanup(self):
        self.assertIsNotNone(supervise_counts, 'supervised processes are not implemented')
        with tempfile.TemporaryDirectory() as name:
            sentinel = Path(name) / 'sentinel.txt'
            sentinel.write_text('external synthetic sentinel', encoding='utf-8')
            for value, expected in [(case(), 'ok'), (case(up='INSERT INTO missing VALUES(0);'), 'execution_failed')]:
                result = supervise_counts(value, Limits())
                self.assertEqual(result['code'], expected)
                self.assertTrue(result['reaped'])
                self.assertFalse(Path(result['temp_path']).exists())
                self.assertEqual(result['child_count'], 2)
            self.assertEqual(sentinel.read_text(encoding='utf-8'), 'external synthetic sentinel')

    def test_parent_terminates_and_reaps(self):
        self.assertIsNotNone(supervise_counts)
        result = supervise_counts(case(seed='WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n) INSERT INTO t SELECT x FROM n;'), Limits(run_seconds=0.2))
        self.assertEqual(result['code'], 'run_timeout')
        self.assertTrue(result['reaped'])
        self.assertFalse(Path(result['temp_path']).exists())

    def test_run_case_requires_coordinator_context(self):
        self.assertIsNotNone(supervise_counts)
        with self.assertRaises(ExecutionFailed) as result:
            run_case(case(), Limits())
        self.assertEqual(result.exception.code, 'context_mismatch')

    def test_blocked_coordinator_and_worker_are_killed(self):
        for kwargs in [{'_coordinator_target': stalled_child}, {'_worker_target': stalled_child}]:
            with self.subTest(role=tuple(kwargs)):
                result = supervise_counts(case(), Limits(run_seconds=0.3), **kwargs)
                self.assertEqual(result['code'], 'run_timeout')
                self.assertTrue(result['reaped'])
                self.assertEqual(result['child_count'], 2)
                self.assertLess(result['elapsed'], 2)
                self.assertFalse(Path(result['temp_path']).exists())

    def test_ungranted_ipc_is_rejected(self):
        result = supervise_counts(case(), Limits(), _coordinator_target=forged_child)
        self.assertEqual(result['code'], 'protocol_error')
        self.assertTrue(result['reaped'])
        self.assertFalse(Path(result['temp_path']).exists())

    def test_cancel_reaps_children(self):
        timer = threading.Timer(0.2, _thread.interrupt_main)
        timer.start()
        try:
            result = supervise_counts(case(), Limits(), _coordinator_target=stalled_child)
        finally:
            timer.cancel()
            timer.join()
        self.assertEqual(result['code'], 'cancelled')
        self.assertTrue(result['reaped'])
        self.assertFalse(Path(result['temp_path']).exists())

    def test_supervised_ipc_budget_and_snapshot_budget_are_separate(self):
        result = supervise_counts(case(), Limits(ipc_bytes_total=64))
        self.assertEqual(result['code'], 'ipc_limit')
        result = supervise_counts(case(), Limits(snapshot_bytes_total=90))
        self.assertEqual(result['code'], 'snapshot_limit')
        self.assertLessEqual(result['snapshot_bytes_total'], 90)
        self.assertGreater(result['ipc_bytes_total'], 90)

    def test_real_commit_failure_and_failed_rollback_are_diagnosed(self):
        # Bypass only the preflight check, so SQLite itself rejects COMMIT for
        # the actual deferred FK; no transaction or SQLite response is mocked.
        with patch('migreplay.worker.health_checks', return_value=()):
            guard, audit = self.execute(case(schema='CREATE TABLE p(id PRIMARY KEY); CREATE TABLE t(x REFERENCES p(id) DEFERRABLE INITIALLY DEFERRED);', seed='', up='INSERT INTO t VALUES(9);'))
        self.assertEqual(audit['stages'][1]['status'], 'rolled_back')
        self.assertEqual(audit['diagnostics'][0]['code'], 'execution_failed')
        self.assertEqual(guard.execute('SELECT * FROM t;').fetchall(), [])
        _, timed = self.execute(case(seed='WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n) INSERT INTO t SELECT x FROM n;'), Limits(run_seconds=0.02))
        self.assertFalse(timed['execution_complete'])
        self.assertIn('rollback_failed', [d['code'] for d in timed['diagnostics']])

    def test_cleanup_refuses_replaced_directory(self):
        from migreplay.runner import _cleanup_owned
        from migreplay.inputs import no_links
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            owned = parent / 'owned'
            owned.mkdir()
            identity = no_links(owned)[-1][1:]
            owned.rename(parent / 'saved')
            owned.mkdir()
            sentinel = owned / 'sentinel.txt'
            sentinel.write_text('do not remove', encoding='utf-8')
            with self.assertRaises(ExecutionFailed) as result:
                _cleanup_owned(owned, identity)
            self.assertEqual(result.exception.code, 'cleanup_failed')
            self.assertEqual(sentinel.read_text(encoding='utf-8'), 'do not remove')

    def test_frame_length_mismatch_rejected_before_payload(self):
        from migreplay.runner import CONTROL, FRAME, GRANT, _receive_frame
        data_send, data_recv = socket.socketpair()
        control_send, control_recv = socket.socketpair()
        try:
            control_send.sendall(CONTROL.pack(GRANT, 1, 4))
            data_send.sendall(FRAME.pack(1, 999999999))
            with self.assertRaises(ExecutionFailed) as result:
                _receive_frame(data_recv, control_recv, Budget(RunContext.create(Limits())))
            self.assertEqual(result.exception.code, 'protocol_error')
        finally:
            for item in (data_send, data_recv, control_send, control_recv):
                item.close()


if __name__ == '__main__':
    unittest.main()
