"""Nonblocking supervisor and bounded, explicitly authorized JSON transport."""
from dataclasses import asdict
import json
import multiprocessing as mp
import os
from pathlib import Path
import select
import socket
import stat
import struct
import tempfile
import time

from .inputs import no_links
from .model import (Allowance, AssertionSpec, Case, Diagnostic, ExecutionFailed,
                    Limits, LimitExceeded, Migration, OutputRequest, RunTrace,
                    SafeError, SourceMeta, SourceRef, SqlStatement, Summary)
from .model import AssertionResult, CheckResult, CheckViolation, Snapshot, StageResult, TableSnapshot
from .policy import Budget, ConnectionGuard, RunContext
from .worker import execute_counts, execute_trace
from .status import failure_summary

CONTROL = struct.Struct('!BQQ')
FRAME = struct.Struct('!QQ')
REQUEST, GRANT, RECEIVED, DONE, FAILED = 1, 2, 3, 4, 5
RESULT = 6
STATUSES = ('passed', 'expected_irreversible', 'unexpected_loss', 'incomplete', 'invalid_input')
_ACTIVE = None
CODE_LIST = ('ok', 'run_timeout', 'statement_timeout', 'ipc_limit', 'snapshot_limit', 'row_limit', 'column_limit', 'table_limit', 'value_limit', 'database_limit', 'limit_exceeded', 'policy_denied', 'execution_failed', 'foreign_key_failed', 'integrity_failed', 'check_error', 'cancelled', 'cleanup_failed', 'rollback_failed', 'protocol_error', 'worker_failed', 'analysis_unavailable', 'unsupported_value', 'invalid_path', 'html_limit', 'output_limit', 'output_failed')


def _code_number(code):
    return CODE_LIST.index(code) if code in CODE_LIST else CODE_LIST.index('worker_failed')


def _recv_exact(sock, length, deadline):
    data = bytearray()
    while len(data) < length:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LimitExceeded('run_timeout')
        sock.settimeout(min(remaining, 0.1))
        try:
            piece = sock.recv(min(65536, length - len(data)))
        except socket.timeout:
            continue
        if not piece:
            raise ExecutionFailed('protocol_error')
        data.extend(piece)
    return bytes(data)


def _control(sock, deadline):
    return CONTROL.unpack(_recv_exact(sock, CONTROL.size, deadline))


def _send(sock, data, deadline):
    view = memoryview(data)
    while view:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LimitExceeded('run_timeout')
        sock.settimeout(min(remaining, 0.1))
        try:
            sent = sock.send(view[:65536])
        except socket.timeout:
            continue
        view = view[sent:]


def _receive_frame(data, control, budget):
    command, sequence, length = _control(control, budget.context.deadline)
    if command != GRANT or length > budget.limits.ipc_bytes_total:
        raise ExecutionFailed('protocol_error')
    actual_sequence, actual_length = FRAME.unpack(_recv_exact(data, FRAME.size, budget.context.deadline))
    if (sequence, length) != (actual_sequence, actual_length):
        raise ExecutionFailed('protocol_error')
    payload = _recv_exact(data, length, budget.context.deadline)
    _send(control, CONTROL.pack(RECEIVED, sequence, length), budget.context.deadline)
    budget.check_deadline()
    return payload


def _send_frame(payload, data, control, budget):
    if len(payload) + FRAME.size > budget.limits.ipc_bytes_total:
        raise LimitExceeded('ipc_limit')
    _send(control, CONTROL.pack(REQUEST, 0, len(payload)), budget.context.deadline)
    command, sequence, length = _control(control, budget.context.deadline)
    if command != GRANT or length != len(payload):
        raise ExecutionFailed('protocol_error')
    _send(data, FRAME.pack(sequence, length), budget.context.deadline)
    _send(data, payload, budget.context.deadline)


def _encode(value, budget):
    chunks, size = [], 0
    for part in json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(',', ':')).iterencode(value):
        budget.check_deadline()
        chunk = part.encode('utf-8')
        size += len(chunk)
        if size + FRAME.size > budget.limits.ipc_bytes_total:
            raise LimitExceeded('ipc_limit')
        chunks.append(chunk)
    return b''.join(chunks)


def _decode(payload, budget):
    budget.check_deadline()
    try:
        result = json.loads(payload)
    except (ValueError, RecursionError, UnicodeError):
        raise ExecutionFailed('protocol_error') from None
    budget.check_deadline()
    return result


def _case_dict(case):
    result = asdict(case)
    result['_input_paths'] = [str(path) for path in case._input_paths]
    return result


def _case_from_dict(value):
    def statements(items):
        return tuple(SqlStatement(item['sql'], SourceRef(**item['source'])) for item in items)
    return Case(statements(value['schema']), statements(value['seed']), tuple(Migration(m['id'], statements(m['up']), statements(m['down'])) for m in value['migrations']), {k: tuple(v) for k, v in value['keys'].items()}, tuple(AssertionSpec(a['id'], tuple(a['at']), a['sql'], a['expect']) for a in value['assertions']), tuple(Allowance(a['id'], a['migration'], a['phase'], a['table'], a['kind'], tuple(a['columns']), a['reason']) for a in value['allowances']), tuple(SourceMeta(**s) for s in value['sources']), tuple(Path(p) for p in value['_input_paths']))


def _child_context(identity, limits):
    run_id, started, deadline, counters = identity[:4]
    return RunContext(run_id, started, deadline, limits, counters)


def _trace_dict(trace, budget):
    from .status import stage_view
    stages = []
    for stage in trace.stages:
        value = stage_view(stage)
        for snapshot, item in zip(stage.snapshots, value['snapshots']):
            budget.check_deadline()
            item['_schema'] = snapshot._schema
            for name, table in snapshot.tables.items():
                private = item['tables'][name]
                private['_schema'] = table._schema
                private['_rows'] = []
                for row in table._rows:
                    budget.check_deadline()
                    private['_rows'].append([cell.hex() for cell in row])
                private['_identity_unreliable'] = table._identity_unreliable
                private['_sequence_rows'] = None if table._sequence_rows is None else [[cell.hex() for cell in row] for row in table._sequence_rows]
        stages.append(value)
    return dict(runtime=trace.runtime, stages=stages, diagnostics=[asdict(d) for d in trace.diagnostics], complete=trace.complete)


def _trace_from_dict(value, case, budget):
    stages = []
    for stage in value['stages']:
        snapshots = []
        for snapshot in stage['snapshots']:
            tables = {}
            for name, item in snapshot['tables'].items():
                budget.check_deadline()
                rows = []
                for row in item['_rows']:
                    budget.check_deadline()
                    rows.append(tuple(bytes.fromhex(cell) for cell in row))
                columns = [c[1] for c in item['_schema']['columns']]
                keys = tuple(item['key_columns'])
                positions = [columns.index(c) for c in keys]
                key_index = {tuple(row[p] for p in positions): row for row in rows} if item['identity_mode'] == 'key' else {}
                sequence = item['_sequence_rows']
                tables[name] = TableSnapshot(item['schema_digest'], item['row_count'], item['identity_mode'], keys,
                    item['key_digest'], item['row_digest'], item['column_digests'], item['null_counts'], item['sequence_digest'],
                    item['_schema'], tuple(rows), key_index, item['_identity_unreliable'],
                    None if sequence is None else tuple(tuple(bytes.fromhex(cell) for cell in row) for row in sequence))
            source = snapshot['source']
            snapshots.append(Snapshot(snapshot['stage_id'], SourceRef(**source) if source else None, snapshot['committed'],
                snapshot['schema_digest'], tables, snapshot['id'], snapshot['state'], snapshot['_schema']))
        checks = tuple(CheckResult(c['kind'], c['passed'], c['violations'], tuple(c['tables']), c['code'], tuple(CheckViolation(**d) for d in c['details'])) for c in stage['checks'])
        stages.append(StageResult(stage['id'], stage['migration'], stage['phase'], stage['status'], tuple(snapshots), checks,
                                  tuple(AssertionResult(**a) for a in stage['assertions']), stage['final_snapshot_id']))
    diagnostics = tuple(Diagnostic(d['code'], SourceRef(**d['source']) if d['source'] else None, d['stage_id']) for d in value['diagnostics'])
    return RunTrace(value['runtime'], tuple(stages), diagnostics, value['complete'], budget.context, _case=case, _run_id=budget.context.run_id)


def _sql_worker(control, data, identity, path):
    guard = None
    # The grant bounds the first read; the actual limits are in that frame.
    budget = Budget(_child_context(identity, Limits()))
    try:
        config = _decode(_receive_frame(data, control, budget), budget)
        budget.context.limits = Limits(**config['limits'])
        case = _case_from_dict(config['case'])
        guard = ConnectionGuard(Path(path), budget)
        audit = _trace_dict(execute_trace(guard, case), budget) if config.get('analysis') else execute_counts(guard, case)
        guard.close()
        guard = None
        audit['snapshot_bytes_total'] = budget.context.snapshot_bytes_total
        _send_frame(_encode(audit, budget), data, control, budget)
        _send(control, CONTROL.pack(DONE, 0, 0), budget.context.deadline)
    except BaseException as exc:
        code = exc.code if isinstance(exc, SafeError) else 'worker_failed'
        try:
            _send(control, CONTROL.pack(FAILED, _code_number(code), 0), budget.context.deadline)
        except (OSError, SafeError):
            pass
    finally:
        if guard:
            guard.close()
        control.close()
        data.close()


def _count_coordinator(control, initial, data, identity):
    budget = Budget(_child_context(identity, Limits()))
    try:
        payload = _receive_frame(initial, control, budget)
        config = _decode(payload, budget)
        budget.context.limits = Limits(**config['limits'])
        _send_frame(payload, data, control, budget)
        audit = _decode(_receive_frame(data, control, budget), budget)
        # Count bridge terminal is not Summary or Report and never claims
        # restoration. No SQL, row, path, key, or assertion result leaves here.
        code = audit['diagnostics'][0]['code'] if audit['diagnostics'] else 'ok'
        _send(control, CONTROL.pack(DONE, _code_number(code), audit['snapshot_bytes_total']), budget.context.deadline)
    except BaseException as exc:
        code = exc.code if isinstance(exc, SafeError) else 'worker_failed'
        try:
            _send(control, CONTROL.pack(FAILED, _code_number(code), 0), budget.context.deadline)
        except (OSError, SafeError):
            pass
    finally:
        for sock in (control, initial, data):
            sock.close()


def _analysis_coordinator(control, initial, data, identity):
    global _ACTIVE
    budget = Budget(_child_context(identity, Limits()))
    try:
        payload = _receive_frame(initial, control, budget)
        config = _decode(payload, budget)
        budget.context.limits = Limits(**config['limits'])
        if config['case'] is None:
            import platform
            import sqlite3
            from . import __version__
            from .status import failure_report
            report = failure_report(Diagnostic(config['initial_failure']),
                {'product': __version__, 'python': platform.python_version(), 'sqlite': sqlite3.sqlite_version}, budget.limits)
            report._context = budget.context
        else:
            case = _case_from_dict(config['case'])
            _ACTIVE = (case, budget, data, control)
            _send_frame(payload, data, control, budget)
            trace = run_case(case, budget.limits)
            from .compare import analyze_trace
            report = analyze_trace(case, trace, budget.limits)
        if config.get('sink'):
            from .report import write_reports
            request = config['sink']
            sink = OutputRequest(Path(request['destination']), tuple(Path(p) for p in request['input_paths']))
            budget.context._sink = sink
            budget.context._output_lease = identity[4]
            write_reports(report, sink.destination, sink.input_paths)
        else:
            _encode(report._public_model, budget)
        summary = report.summary
        counts = [summary[k] for k in ('unexpected_count', 'expected_count', 'assertion_failed_count')]
        if any(not 0 <= count < 2 ** 20 for count in counts):
            raise LimitExceeded('limit_exceeded')
        packed = counts[0] | counts[1] << 20 | counts[2] << 40
        _send(control, CONTROL.pack(RESULT, STATUSES.index(summary['status']), packed), budget.context.deadline)
        _send(control, CONTROL.pack(DONE, 0, budget.context.snapshot_bytes_total), budget.context.deadline)
    except BaseException as exc:
        code = exc.code if isinstance(exc, SafeError) else 'worker_failed'
        try:
            _send(control, CONTROL.pack(FAILED, _code_number(code), 0), budget.context.deadline)
        except (OSError, SafeError):
            pass
    finally:
        _ACTIVE = None
        for sock in (control, initial, data):
            sock.close()


def _cleanup_owned(path, ownership):
    """Delete only the freshly owned directory and known SQLite sidecars."""
    try:
        if not path.is_absolute() or no_links(path)[-1][1:] != ownership:
            raise ExecutionFailed('cleanup_failed')
        items = list(path.iterdir())
        for item in items:
            info = item.lstat()
            if item.name not in ('run.sqlite', 'run.sqlite-journal', 'run.sqlite-wal', 'run.sqlite-shm') or not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ExecutionFailed('cleanup_failed')
        for item in items:
            if no_links(path)[-1][1:] != ownership:
                raise ExecutionFailed('cleanup_failed')
            no_links(item)
            item.unlink()
        path.rmdir()
    except (OSError, SafeError):
        raise ExecutionFailed('cleanup_failed') from None


def supervise_counts(case, limits, *, _coordinator_target=None, _worker_target=None, _analysis=False, _sink=None, _initial_failure=None):
    """Private T1 supervisor. Target overrides are in-process test seams only.

    It owns both child handles. After launch it processes only fixed control
    headers, grants, and terminal codes, never decodes a RunTrace or Report.
    """
    context = RunContext.create(limits)
    context._counters = mp.get_context('spawn').RawArray('Q', 3)
    budget = Budget(context)
    processes, sockets = [], []
    path = None
    ownership = None
    code, reaped = 'worker_failed', False
    summary = None
    from .report import LEASE_SIZE, cleanup_output
    output_lease = mp.get_context('spawn').RawArray('Q', LEASE_SIZE)
    try:
        payload = _encode({'case': _case_dict(case) if case is not None else None, 'limits': asdict(limits), 'analysis': _analysis,
            'sink': {'destination': str(_sink.destination), 'input_paths': [str(p) for p in _sink.input_paths]} if _sink else None,
            'initial_failure': _initial_failure.code if _initial_failure else None}, budget)
        budget.check_deadline()
        path = Path(tempfile.mkdtemp(prefix='migreplay-')).absolute()
        os.chmod(path, 0o700)
        ownership = no_links(path)[-1][1:]
        pairs = [socket.socketpair() for _ in range(4)]
        sockets = [sock for pair in pairs for sock in pair]
        (parent_c, child_c), (parent_w, child_w), (initial_p, initial_c), (data_c, data_w) = pairs
        identity = context.run_id, context.started_at, context.deadline, context._counters, output_lease
        spawn = mp.get_context('spawn')
        coordinator = spawn.Process(target=_coordinator_target or _count_coordinator, args=(child_c, initial_c, data_c, identity), daemon=True)
        worker = spawn.Process(target=_worker_target or _sql_worker, args=(child_w, data_w, identity, str(path / 'run.sqlite')), daemon=True)
        for process in ((coordinator, worker) if case is not None else (coordinator,)):
            budget.check_deadline()
            process.start()
            processes.append(process)
        for sock in (child_c, child_w, initial_c, data_c, data_w):
            sock.close()
        controls = [parent_c, parent_w] if case is not None else [parent_c]
        for sock in controls + [initial_p]:
            sock.setblocking(False)
        incoming = [bytearray(), bytearray()]
        outgoing = [bytearray(), bytearray()]
        grants = {}
        sequence = 1
        budget.reserve_frame(sequence, FRAME.size + len(payload))
        grants[1] = (len(payload), 0)
        initial_pending = bytearray(FRAME.pack(1, len(payload)) + payload)

        def charge_control():
            if context.ipc_bytes_total + CONTROL.size > limits.ipc_bytes_total:
                raise LimitExceeded('ipc_limit')
            context.ipc_bytes_total += CONTROL.size

        def enqueue(index, command, a, b):
            charge_control()
            outgoing[index].extend(CONTROL.pack(command, a, b))

        enqueue(0, GRANT, 1, len(payload))
        terminal = None
        summary = None
        worker_done = case is None
        while terminal is None or not worker_done:
            budget.check_deadline()
            readable, writable, _ = select.select(controls, [controls[i] for i in range(len(controls)) if outgoing[i]] + ([initial_p] if initial_pending else []), [], min(0.01, max(0, context.deadline - time.monotonic())))
            budget.check_deadline()
            for sock in writable:
                pending = initial_pending if sock is initial_p else outgoing[controls.index(sock)]
                sent = sock.send(memoryview(pending)[:65536])
                del pending[:sent]
            for sock in readable:
                index = controls.index(sock)
                piece = sock.recv(CONTROL.size - len(incoming[index]))
                if not piece:
                    budget.check_deadline()
                    if index == 1 and worker_done or index == 0 and terminal is not None:
                        continue
                    raise ExecutionFailed('worker_failed')
                incoming[index].extend(piece)
                if len(incoming[index]) < CONTROL.size:
                    continue
                command, a, b = CONTROL.unpack(incoming[index])
                incoming[index].clear()
                charge_control()
                if command == REQUEST:
                    if a != 0 or b > limits.ipc_bytes_total or grants:
                        raise ExecutionFailed('protocol_error')
                    sequence += 1
                    budget.reserve_frame(sequence, FRAME.size + b)
                    receiver = 1 - index
                    grants[sequence] = (b, receiver)
                    enqueue(index, GRANT, sequence, b)
                    enqueue(receiver, GRANT, sequence, b)
                elif command == RECEIVED:
                    if grants.pop(a, None) != (b, index):
                        raise ExecutionFailed('protocol_error')
                    budget.verify_frame(a, FRAME.size + b)
                elif command == RESULT:
                    if not _analysis or index != 0 or summary is not None or a >= len(STATUSES) or b >= 2 ** 60:
                        raise ExecutionFailed('protocol_error')
                    status = STATUSES[a]
                    code_value = {'passed': 0, 'expected_irreversible': 0, 'unexpected_loss': 1, 'incomplete': 3, 'invalid_input': 2}[status]
                    summary = Summary(status, code_value, b & (2 ** 20 - 1), b >> 20 & (2 ** 20 - 1), b >> 40, status not in ('incomplete', 'invalid_input'))
                elif command in (DONE, FAILED):
                    if a >= len(CODE_LIST):
                        raise ExecutionFailed('protocol_error')
                    if command == FAILED:
                        raise ExecutionFailed(CODE_LIST[a] if a else 'worker_failed')
                    if index == 0:
                        if b > limits.snapshot_bytes_total:
                            raise LimitExceeded('snapshot_limit')
                        terminal = CODE_LIST[a]
                        if context.snapshot_bytes_total != b:
                            raise ExecutionFailed('protocol_error')
                    else:
                        worker_done = True
                else:
                    raise ExecutionFailed('protocol_error')
        if grants:
            raise ExecutionFailed('protocol_error')
        if _analysis and summary is None:
            raise ExecutionFailed('protocol_error')
        code = terminal
    except KeyboardInterrupt:
        context.cancelled = True
        code = 'cancelled'
    except SafeError as exc:
        code = exc.code
    except (OSError, ValueError):
        code = 'worker_failed'
    finally:
        # Bounded convergence is cleanup only, never extra productive time.
        for process in processes:
            if process.is_alive():
                process.terminate()
        for process in processes:
            process.join(0.4)
            if process.is_alive():
                process.kill()
                process.join(0.4)
        reaped = all(not process.is_alive() and process.exitcode is not None for process in processes)
        for sock in sockets:
            sock.close()
        if path is not None and ownership is not None:
            if reaped:
                try:
                    _cleanup_owned(path, ownership)
                except SafeError:
                    code = 'cleanup_failed'
            else:
                code = 'cleanup_failed'
        for process in processes:
            if not process.is_alive():
                process.close()
        if _sink is not None and code != 'ok' and reaped:
            try:
                cleanup_output(_sink.destination, output_lease)
            except SafeError:
                code = 'cleanup_failed'
    result = {'code': code, 'reaped': reaped, 'child_count': len(processes), 'temp_path': str(path) if path else '', 'ipc_bytes_total': context.ipc_bytes_total, 'snapshot_bytes_total': context.snapshot_bytes_total, 'elapsed': time.monotonic() - context.started_at}
    if _analysis:
        result['summary'] = summary if code == 'ok' else failure_summary(code)
    return result


def _supervise_analysis(case, limits, *, _coordinator_target=None):
    return supervise_counts(case, limits, _coordinator_target=_coordinator_target or _analysis_coordinator, _analysis=True)


def run_case(case: Case, limits: Limits) -> RunTrace:
    """Coordinator-internal API, using the original transport and RunContext."""
    if _ACTIVE is None:
        raise ExecutionFailed('context_mismatch')
    active_case, budget, data, control = _ACTIVE
    if active_case is not case or budget.limits != limits:
        raise ExecutionFailed('context_mismatch')
    payload = _decode(_receive_frame(data, control, budget), budget)
    return _trace_from_dict(payload, case, budget)


def coordinate_run(case: Case | None, limits: Limits, *, sink: OutputRequest, initial_failure: Diagnostic | None = None) -> Summary:
    if (case is None) != (initial_failure is not None):
        raise ExecutionFailed('protocol_error')
    from .report import validate_destination
    try:
        destination = validate_destination(sink.destination, sink.input_paths)
    except SafeError as exc:
        return failure_summary(exc.code)
    if case is not None and not set(case._input_paths).issubset(sink.input_paths):
        return failure_summary('invalid_path')
    return supervise_counts(case, limits, _analysis=True, _coordinator_target=_analysis_coordinator,
        _sink=OutputRequest(destination, sink.input_paths), _initial_failure=initial_failure)['summary']
