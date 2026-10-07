"""Read-only SQL invariants, with false, shape, and execution errors distinct."""
from .canonical import encode_value
from .model import AssertionResult, SafeError
from .policy import _controlled_guard


def evaluate_assertions(conn, specs, stage_id, budget):
    guard = _controlled_guard(conn, budget)
    results = []
    for spec in specs:
        if stage_id not in spec.at:
            continue
        try:
            with guard.mode('readonly'):
                cursor = guard.execute(spec.sql)
                rows = []
                try:
                    for row in guard.rows(cursor):
                        for value in row:
                            encode_value(value)
                        rows.append(row)
                        if len(rows) > 1:
                            break
                finally:
                    cursor.close()
            if len(rows) != 1 or len(rows[0]) != 1 or type(rows[0][0]) is not int or rows[0][0] not in (0, 1):
                results.append(AssertionResult(spec.id, stage_id, 'failed', 'assertion_shape'))
            elif rows[0][0] != spec.expect:
                results.append(AssertionResult(spec.id, stage_id, 'failed', 'assertion_false'))
            else:
                results.append(AssertionResult(spec.id, stage_id, 'passed'))
        except SafeError as exc:
            code = 'assertion_error' if exc.code == 'execution_failed' else exc.code
            results.append(AssertionResult(spec.id, stage_id, 'error', code))
            break
    return tuple(results)
