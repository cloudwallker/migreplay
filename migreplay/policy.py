"""SQLite capability policy and one-run resource accounting."""
from contextlib import contextmanager
import math
import os
from pathlib import Path
import sqlite3
import time
import uuid
import weakref

from .inputs import no_links
from .model import ExecutionFailed, InvalidInput, LimitExceeded, Limits, PolicyDenied


_CONNECTIONS = weakref.WeakValueDictionary()


def _controlled_guard(conn, budget):
    guard = _CONNECTIONS.get(id(conn))
    if guard is None or guard.conn is not conn or guard.budget is not budget or guard._registered_context is not budget.context:
        raise ExecutionFailed('context_mismatch')
    budget.check_deadline()
    return guard


class RunContext:
    def __init__(self, run_id, started_at, deadline, limits, counters=None):
        self.run_id, self.started_at, self.deadline, self.limits = run_id, started_at, deadline, limits
        # One shared record: SQL worker writes snapshot bytes; supervisor writes
        # IPC bytes and cancellation. No competing writers or reset on handoff.
        self._counters = counters if counters is not None else [0, 0, 0]

    snapshot_bytes_total = property(lambda self: self._counters[0], lambda self, value: self._counters.__setitem__(0, value))
    ipc_bytes_total = property(lambda self: self._counters[1], lambda self, value: self._counters.__setitem__(1, value))
    cancelled = property(lambda self: bool(self._counters[2]), lambda self, value: self._counters.__setitem__(2, int(value)))

    @classmethod
    def create(cls, limits):
        started = time.monotonic()
        return cls(uuid.uuid4().hex, started, started + limits.run_seconds, limits)


class Budget:
    def __init__(self, context):
        self.context = context
        self._snapshot_bytes = None
        self._rows = 0
        self._table_rows = 0
        self._sequence = 0
        self._reservations = {}

    @property
    def limits(self):
        return self.context.limits

    def check_deadline(self):
        if self.context.cancelled:
            raise LimitExceeded('cancelled')
        if time.monotonic() >= self.context.deadline:
            raise LimitExceeded('run_timeout')

    def begin_snapshot(self):
        self.check_deadline()
        if self._snapshot_bytes is not None:
            raise ExecutionFailed('protocol_error')
        self._snapshot_bytes = self._rows = self._table_rows = 0

    def begin_table(self):
        self._table_rows = 0

    def charge_snapshot(self, byte_count, row_count):
        self.check_deadline()
        if self._snapshot_bytes is None or type(byte_count) is not int or type(row_count) is not int or min(byte_count, row_count) < 0:
            raise ExecutionFailed('protocol_error')
        if self._snapshot_bytes + byte_count > self.limits.snapshot_bytes or self.context.snapshot_bytes_total + byte_count > self.limits.snapshot_bytes_total:
            raise LimitExceeded('snapshot_limit')
        if self._rows + row_count > self.limits.rows_total or self._table_rows + row_count > self.limits.rows_per_table:
            raise LimitExceeded('row_limit')
        self._snapshot_bytes += byte_count
        self.context.snapshot_bytes_total += byte_count
        self._rows += row_count
        self._table_rows += row_count

    def end_snapshot(self):
        self._snapshot_bytes = None

    def reserve_frame(self, sequence, length):
        self.check_deadline()
        if type(sequence) is not int or sequence != self._sequence + 1 or type(length) is not int or length < 0:
            raise ExecutionFailed('protocol_error')
        if self.context.ipc_bytes_total + length > self.limits.ipc_bytes_total:
            raise LimitExceeded('ipc_limit')
        self.context.ipc_bytes_total += length
        self._sequence = sequence
        self._reservations[sequence] = length

    def verify_frame(self, sequence, length):
        self.check_deadline()
        if self._reservations.pop(sequence, None) != length:
            raise ExecutionFailed('protocol_error')


def _words(sql):
    """Early diagnostics only; the authorizer remains enabled for all SQL."""
    import re
    # Skip strings/comments. Keep quoted identifiers for names of functions.
    parts = re.findall(r"--[^\n]*|/\*[\s\S]*?\*/|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`[^`]*`|\[[^\]]*\]|[A-Za-z_][A-Za-z_0-9]*", sql)
    return [p.strip('"`[]').lower() for p in parts if not p.startswith(("'", '--', '/*'))]


def _dangerous_calls(sql):
    """Early call diagnostics, excluding object/column-list declarations.

    SQLite's authorizer remains the execution boundary, including views and
    triggers. This also diagnoses unavailable file functions before SQLite can
    defer resolving a CREATE VIEW/TRIGGER body until a later statement.
    """
    import re
    raw = re.findall(r"--[^\n]*|/\*[\s\S]*?\*/|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`|\[[^\]]*\]|[A-Za-z_][A-Za-z_0-9]*|[^\s]", sql)
    raw = [token for token in raw if not token.startswith(('--', '/*'))]
    tokens = [token.strip('"`[]').lower() for token in raw]

    def close_paren(start):
        depth = 0
        for index in range(start, len(tokens)):
            if raw[index] == '(':
                depth += 1
            elif raw[index] == ')':
                depth -= 1
                if depth == 0:
                    return index + 1
        return len(tokens)

    # WITH cte(column,...) AS (...) is a table declaration, not a call.
    cte_declarations = set()
    for start, token in enumerate(raw):
        if token.lower() != 'with':
            continue
        index = start + 1
        if index < len(tokens) and tokens[index] == 'recursive':
            index += 1
        while index < len(tokens):
            name = index
            index += 1
            if index < len(tokens) and raw[index] == '(':
                index = close_paren(index)
            if index >= len(tokens) or tokens[index] != 'as':
                break
            cte_declarations.add(name)
            index += 1
            if index < len(tokens) and tokens[index] == 'not':
                index += 1
            if index < len(tokens) and tokens[index] == 'materialized':
                index += 1
            if index >= len(tokens) or raw[index] != '(':
                break
            index = close_paren(index)
            if index >= len(tokens) or raw[index] != ',':
                break
            index += 1

    for index, name in enumerate(tokens[:-1]):
        if raw[index].startswith("'") or raw[index + 1] != '(':
            continue
        if name not in ('load_extension', 'readfile', 'writefile', 'fts3_tokenizer') and not name.startswith('pragma_'):
            continue
        before = index - 1
        # Skip schema qualification: INSERT INTO main.readfile(...).
        if before >= 1 and raw[before] == '.':
            before -= 2
        previous = tokens[before] if before >= 0 else None
        declaration = previous in ('table', 'view', 'into', 'references')
        declaration |= previous == 'exists' and before >= 3 and tokens[before - 2:before + 1] == ['if', 'not', 'exists'] and tokens[before - 3] in ('table', 'view')
        declaration |= previous == 'on' and tokens[0] == 'create' and 'index' in tokens[:before]
        declaration |= previous == 'replace' and before == 0
        if not declaration and index not in cte_declarations:
            return True
    return False


class ConnectionGuard:
    def __init__(self, path: Path, budget: Budget):
        self.path, self.budget = Path(path), budget
        self.actions = set()
        self._mode = 'trusted'
        self._ddl = False
        self._denied = False
        self._query_deadline = budget.context.deadline
        self._timed_out = False
        no_links(self.path.parent)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        except OSError:
            raise InvalidInput('invalid_path') from None
        try:
            self.conn = sqlite3.connect(self.path, autocommit=True, cached_statements=0)
            self.conn.row_factory = self._checked_row
            self.conn.enable_load_extension(False)
            self.conn.set_authorizer(self._authorize)
            self.conn.set_progress_handler(self._progress, budget.limits.progress_instructions)
            for sql in ['PRAGMA foreign_keys=ON', 'PRAGMA trusted_schema=OFF', 'PRAGMA temp_store=MEMORY', 'PRAGMA page_size=4096', f'PRAGMA max_page_count={max(1, budget.limits.database_bytes // 4096)}']:
                self.execute(sql)
            # LENGTH bounds both values and SQLite's complete encoded records.
            # Keep a finite allocation ceiling that admits legal multi-value
            # rows; _checked_row enforces the separate per-value byte limit.
            record_bytes = min(budget.limits.database_bytes, max(budget.limits.snapshot_bytes, budget.limits.value_bytes))
            for category, value in [(sqlite3.SQLITE_LIMIT_SQL_LENGTH, budget.limits.sql_file_bytes), (sqlite3.SQLITE_LIMIT_LENGTH, record_bytes), (sqlite3.SQLITE_LIMIT_COLUMN, budget.limits.columns_per_table), (sqlite3.SQLITE_LIMIT_ATTACHED, 0), (sqlite3.SQLITE_LIMIT_TRIGGER_DEPTH, budget.limits.trigger_depth)]:
                self.conn.setlimit(category, value)
            self._mode = 'user'
            self._registered_context = budget.context
            _CONNECTIONS[id(self.conn)] = self
        except BaseException:
            if hasattr(self, 'conn'):
                self.conn.close()
            raise

    @contextmanager
    def mode(self, mode):
        previous = self._mode
        self._mode = mode
        try:
            yield
        finally:
            self._mode = previous

    def _authorize(self, action, arg1, arg2, database, origin):
        self.actions.add(action)
        deny = False
        forbidden = {sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH, sqlite3.SQLITE_CREATE_VTABLE, sqlite3.SQLITE_DROP_VTABLE, sqlite3.SQLITE_CREATE_TEMP_TABLE, sqlite3.SQLITE_CREATE_TEMP_INDEX, sqlite3.SQLITE_CREATE_TEMP_TRIGGER, sqlite3.SQLITE_CREATE_TEMP_VIEW, sqlite3.SQLITE_DROP_TEMP_TABLE, sqlite3.SQLITE_DROP_TEMP_INDEX, sqlite3.SQLITE_DROP_TEMP_TRIGGER, sqlite3.SQLITE_DROP_TEMP_VIEW}
        internal_temp_schema = self._ddl and database == 'temp' and arg1 == 'sqlite_temp_master' and action in (sqlite3.SQLITE_READ, sqlite3.SQLITE_UPDATE)
        data_action = action in (sqlite3.SQLITE_READ, sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE)
        if action in forbidden or data_action and database not in (None, 'main') and not internal_temp_schema:
            deny = True
        if action == sqlite3.SQLITE_FUNCTION:
            name = (arg2 or arg1 or '').lower()
            if name in ('load_extension', 'readfile', 'writefile', 'fts3_tokenizer') or name.startswith('pragma_'):
                deny = True
        if self._mode != 'trusted':
            if action in (sqlite3.SQLITE_PRAGMA, sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT):
                deny = True
            if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE) and arg1 in ('sqlite_master', 'sqlite_schema', 'sqlite_temp_master', 'sqlite_temp_schema') and not self._ddl:
                deny = True
            if self._mode == 'readonly' and action not in (sqlite3.SQLITE_READ, sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE):
                deny = True
        if deny:
            self._denied = True
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def _progress(self):
        if self.budget.context.cancelled or time.monotonic() >= self._query_deadline:
            self._timed_out = True
            return 1
        return 0

    def _checked_row(self, cursor, row):
        """Check every fetched value, including direct cursor fetch methods.

        DML records are validated by the runner's observation before COMMIT;
        transient allocations remain bounded by SQLite's record ceiling.
        """
        self.budget.check_deadline()
        if time.monotonic() >= self._query_deadline:
            raise LimitExceeded('statement_timeout')
        for value in row:
            length = len(value.encode('utf-8')) if isinstance(value, str) else len(value) if isinstance(value, bytes) else 0
            if length > self.budget.limits.value_bytes:
                raise LimitExceeded('value_limit')
        return row

    def execute(self, sql, params=()):
        self.budget.check_deadline()
        words = _words(sql)
        self._denied = self._timed_out = False
        self._ddl = bool(words and words[0] in ('create', 'alter', 'drop'))
        if self._mode != 'trusted' and (words and words[0] in ('vacuum', 'pragma', 'begin', 'commit', 'end', 'rollback', 'savepoint', 'release') or _dangerous_calls(sql)):
            raise PolicyDenied('policy_denied')
        self._query_deadline = min(self.budget.context.deadline, time.monotonic() + self.budget.limits.statement_seconds)
        try:
            return self.conn.execute(sql, params)
        except sqlite3.Error as exc:
            if self._denied or words and words[0] in ('insert', 'update', 'delete', 'replace') and any(w in ('sqlite_schema', 'sqlite_master') for w in words):
                raise PolicyDenied('policy_denied') from None
            if self._timed_out:
                code = 'run_timeout' if time.monotonic() >= self.budget.context.deadline else 'statement_timeout'
                raise LimitExceeded(code) from None
            if getattr(exc, 'sqlite_errorcode', 0) in (sqlite3.SQLITE_TOOBIG, sqlite3.SQLITE_FULL, sqlite3.SQLITE_NOMEM):
                raise LimitExceeded('database_limit' if exc.sqlite_errorcode == sqlite3.SQLITE_FULL else 'value_limit') from None
            # SQLite reports over-wide schemas using SQLITE_ERROR.
            if 'too many columns' in str(exc):
                raise LimitExceeded('column_limit') from None
            raise ExecutionFailed('execution_failed') from None
        finally:
            self._ddl = False

    def rows(self, cursor):
        """Stream cursor errors through the same safe error vocabulary."""
        try:
            while True:
                self.budget.check_deadline()
                if time.monotonic() >= self._query_deadline:
                    raise LimitExceeded('statement_timeout')
                row = cursor.fetchone()
                if row is None:
                    return
                yield row
        except sqlite3.Error as exc:
            if self._timed_out:
                raise LimitExceeded('statement_timeout') from None
            if getattr(exc, 'sqlite_errorcode', 0) == sqlite3.SQLITE_TOOBIG:
                raise LimitExceeded('value_limit') from None
            raise ExecutionFailed('execution_failed') from None

    def close(self):
        try:
            self.conn.close()
        finally:
            _CONNECTIONS.pop(id(self.conn), None)
