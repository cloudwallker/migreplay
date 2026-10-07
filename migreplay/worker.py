"""SQL execution and private count observations; no restoration claims."""
from dataclasses import asdict
import math
import sqlite3
import platform

from .model import CheckResult, CheckViolation, Diagnostic, ExecutionFailed, LimitExceeded, SafeError, RunTrace, StageResult


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _value_size(value):
    if value is None:
        return 9
    if type(value) is int:
        return 9 + len(str(value).encode('ascii'))
    if type(value) is float:
        if not math.isfinite(value):
            raise ExecutionFailed('unsupported_value')
        return 9 + len(value.hex().encode('ascii'))
    if isinstance(value, str):
        return 9 + len(value.encode('utf-8'))
    if isinstance(value, bytes):
        return 9 + len(value)
    raise ExecutionFailed('unsupported_value')


def observe_counts(guard, stage_id, source, budget):
    """Private T1 bridge. These counts are never promoted to Snapshot digests.

    Exercise streaming row and incremental byte limits over the actual database.
    T2 replaces this bridge with its canonical v1 snapshot producer.
    """
    tables = {}
    budget.begin_snapshot()
    try:
        budget.charge_snapshot(8, 0)
        with guard.mode('trusted'):
            names = []
            for row in guard.rows(guard.execute("SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name")):
                budget.charge_snapshot(8 + sum(_value_size(v) for v in row), 0)
                if row[0] == 'table' and not row[1].startswith('sqlite_'):
                    names.append(row[1])
                    if len(names) > budget.limits.tables:
                        raise LimitExceeded('table_limit')
            for name in names:
                columns = list(guard.rows(guard.execute(f'PRAGMA table_xinfo({_quote(name)})')))
                if len(columns) > budget.limits.columns_per_table:
                    raise LimitExceeded('column_limit')
                for row in columns:
                    budget.charge_snapshot(8 + sum(_value_size(v) for v in row), 0)
                budget.begin_table()
                count = 0
                with guard.mode('readonly'):
                    for row in guard.rows(guard.execute(f'SELECT * FROM {_quote(name)}')):
                        sizes = [_value_size(v) for v in row]
                        if any(size - 9 > budget.limits.value_bytes for size in sizes):
                            raise LimitExceeded('value_limit')
                        budget.charge_snapshot(8 + sum(sizes), 1)
                        count += 1
                tables[name] = {'row_count': count, 'columns': [row[1] for row in columns]}
        return {'stage_id': stage_id, 'source': asdict(source) if source else None, 'tables': tables}
    finally:
        budget.end_snapshot()


def health_checks(guard):
    results = []
    for kind, sql in [('foreign_key', 'PRAGMA foreign_key_check'), ('integrity', 'PRAGMA integrity_check')]:
        try:
            with guard.mode('trusted'):
                counts = {}
                violations = 0
                for row in guard.rows(guard.execute(sql)):
                    if kind == 'foreign_key':
                        key = row[0], row[3]
                        counts[key] = counts.get(key, 0) + 1
                        violations += 1
                    elif row != ('ok',):
                        violations += 1
                details = tuple(CheckViolation(table, index, count) for (table, index), count in sorted(counts.items()))
                code = None if not violations else 'foreign_key_failed' if kind == 'foreign_key' else 'integrity_failed'
                results.append(CheckResult(kind, violations == 0, violations, tuple(sorted({key[0] for key in counts})), code, details))
        except SafeError as exc:
            results.append(CheckResult(kind, None, None, (), exc.code))
            # Budget and SQL failures must stop; an unexecuted check is not invented.
            break
    return tuple(results)


def stage_plan(case):
    return [('baseline', None, 'baseline', case.schema + case.seed)] + [(f'up:{m.id}', m.id, 'up', m.up) for m in case.migrations] + [(f'down:{m.id}', m.id, 'down', m.down) for m in reversed(case.migrations)] + [(f'reup:{m.id}', m.id, 'reup', m.up) for m in case.migrations]


def execute_counts(guard, case):
    """Actual transaction engine with an explicitly private observation bridge."""
    stages, observations, diagnostics = [], [], []
    stopped = False
    executed = 0
    for stage_id, migration, phase, statements in stage_plan(case):
        stage = dict(id=stage_id, migration=migration, phase=phase, status='not_started', final_snapshot_id=None, checks=[])
        stages.append(stage)
        if stopped:
            continue
        candidates = []
        source = None
        entered = False
        try:
            guard.budget.check_deadline()
            with guard.mode('trusted'):
                guard.execute('BEGIN')
            entered = True
            for index, statement in enumerate(statements, 1):
                source = statement.source
                executed += 1
                if executed > guard.budget.limits.executed_statements:
                    raise LimitExceeded('limit_exceeded')
                # A SELECT can produce a large/slow result; drain it under the same
                # statement deadline instead of dropping its cursor after first row.
                for _ in guard.rows(guard.execute(statement.sql)):
                    pass
                observed = observe_counts(guard, stage_id, source, guard.budget)
                observed.update(id=f'{stage_id}/statement/{index}', state='provisional')
                candidates.append(observed)
            source = None
            checks = health_checks(guard)
            stage['checks'] = [asdict(check) for check in checks]
            if any(check.passed is not True for check in checks):
                raise ExecutionFailed(next(check.code for check in checks if check.passed is not True))
            observed = observe_counts(guard, stage_id, None, guard.budget)
            observed.update(id=f'{stage_id}/final', state='provisional')
            candidates.append(observed)
            with guard.mode('trusted'):
                guard.execute('COMMIT')
            stage['status'] = 'committed'
            stage['final_snapshot_id'] = observed['id']
            for observed in candidates:
                observed['state'] = 'committed'
            if case.assertions:
                # The T2 assertion producer is deliberately not replaced by a
                # pretend passing result in the T1 bridge.
                raise ExecutionFailed('analysis_unavailable')
        except SafeError as exc:
            stopped = True
            diagnostics.append(asdict(Diagnostic(exc.code, source, stage_id)))
            if entered and stage['status'] != 'committed':
                stage['status'] = 'rolled_back'
                for observed in candidates:
                    observed['state'] = 'rolled_back'
                try:
                    with guard.mode('trusted'):
                        guard.execute('ROLLBACK')
                except SafeError:
                    diagnostics.append(asdict(Diagnostic('rollback_failed', None, stage_id)))
        observations.extend(candidates)
    return dict(stages=stages, observations=observations, diagnostics=diagnostics, execution_complete=not stopped, executed_statements=executed)


def execute_trace(guard, case):
    """Worker producer; the observation closure owns the validated case keys."""
    from . import __version__
    from .snapshot import capture_snapshot
    from .assertions import evaluate_assertions

    def observe(stage_id, source):
        return capture_snapshot(guard.conn, stage_id, source, case.keys, guard.budget)

    stages, diagnostics = [], []
    stopped, executed = False, 0
    for stage_id, migration, phase, statements in stage_plan(case):
        stage = StageResult(stage_id, migration, phase, 'not_started')
        stages.append(stage)
        if stopped:
            continue
        candidates, source, entered = [], None, False
        try:
            with guard.mode('trusted'):
                guard.execute('BEGIN')
            entered = True
            for index, statement in enumerate(statements, 1):
                source = statement.source
                executed += 1
                if executed > guard.budget.limits.executed_statements:
                    raise LimitExceeded('limit_exceeded')
                for _ in guard.rows(guard.execute(statement.sql)):
                    pass
                snapshot = observe(stage_id, source)
                snapshot.id = f'{stage_id}/statement/{index}'
                candidates.append(snapshot)
            source = None
            stage.checks = health_checks(guard)
            if any(check.passed is not True for check in stage.checks):
                raise ExecutionFailed(next(check.code for check in stage.checks if check.passed is not True))
            snapshot = observe(stage_id, None)
            snapshot.id = f'{stage_id}/final'
            candidates.append(snapshot)
            with guard.mode('trusted'):
                guard.execute('COMMIT')
            stage.status, stage.final_snapshot_id = 'committed', snapshot.id
            for snapshot in candidates:
                snapshot.state, snapshot.committed = 'committed', True
            stage.assertions = evaluate_assertions(guard.conn, case.assertions, stage_id, guard.budget)
            for assertion in stage.assertions:
                if assertion.status == 'error':
                    diagnostics.append(Diagnostic(assertion.code, None, stage_id))
                    stopped = True
        except SafeError as exc:
            stopped = True
            diagnostics.append(Diagnostic(exc.code, source, stage_id))
            if entered and stage.status != 'committed':
                stage.status = 'rolled_back'
                for snapshot in candidates:
                    snapshot.state, snapshot.committed = 'rolled_back', False
                try:
                    with guard.mode('trusted'):
                        guard.execute('ROLLBACK')
                except SafeError:
                    diagnostics.append(Diagnostic('rollback_failed', None, stage_id))
        stage.snapshots = tuple(candidates)
    context = guard.budget.context
    return RunTrace(dict(product=__version__, python=platform.python_version(), sqlite=sqlite3.sqlite_version),
                    tuple(stages), tuple(diagnostics), not stopped, context, _case=case, _run_id=context.run_id)
