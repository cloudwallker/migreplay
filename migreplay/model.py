"""Shared data contracts. Underscored fields are private and never public JSON."""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


CODES = frozenset('invalid_input input_limit invalid_path invalid_utf8 invalid_sql unknown_stage policy_denied execution_failed limit_exceeded statement_timeout run_timeout snapshot_limit ipc_limit row_limit column_limit table_limit value_limit database_limit cancelled cleanup_failed rollback_failed foreign_key_failed integrity_failed check_error assertion_false assertion_shape assertion_error unsupported_value protocol_error worker_failed analysis_unavailable output_unavailable context_mismatch html_limit output_limit output_failed'.split())


class SafeError(Exception):
    def __init__(self, code: str, source=None):
        if code not in CODES:
            raise ValueError('Unknown diagnostic code')
        self.code, self.source = code, source
        super().__init__(code)


class InvalidInput(SafeError):
    pass


class LimitExceeded(SafeError):
    pass


class PolicyDenied(SafeError):
    pass


class ExecutionFailed(SafeError):
    pass


@dataclass(frozen=True)
class Limits:
    manifest_bytes: int = 262144
    sql_file_bytes: int = 1048576
    sql_total_bytes: int = 4194304
    migrations: int = 20
    statements_per_file: int = 200
    executed_statements: int = 1000
    tables: int = 50
    columns_per_table: int = 100
    rows_per_table: int = 10000
    rows_total: int = 50000
    value_bytes: int = 1048576
    snapshot_bytes: int = 16777216
    snapshot_bytes_total: int = 67108864
    ipc_bytes_total: int = 67108864
    database_bytes: int = 67108864
    json_bytes: int = 16777216
    html_bytes: int = 25165824
    statement_seconds: float = 2
    run_seconds: float = 30
    progress_instructions: int = 1000
    trigger_depth: int = 10
    attached: int = 0

    def __post_init__(self):
        for name, info in self.__dataclass_fields__.items():
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= info.default:
                raise InvalidInput('input_limit')
            if name != 'attached' and value == 0:
                raise InvalidInput('input_limit')
            if name not in ('statement_seconds', 'run_seconds') and type(value) is not int:
                raise InvalidInput('input_limit')


@dataclass(frozen=True)
class SourceRef:
    file: str
    statement: int
    start_line: int
    end_line: int


@dataclass(frozen=True)
class SqlStatement:
    sql: str
    source: SourceRef


@dataclass(frozen=True)
class Migration:
    id: str
    up: tuple[SqlStatement, ...]
    down: tuple[SqlStatement, ...]


@dataclass(frozen=True)
class AssertionSpec:
    id: str
    at: tuple[str, ...]
    sql: str
    expect: int


@dataclass(frozen=True)
class Allowance:
    id: str
    migration: str
    phase: str
    table: str
    kind: str
    columns: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class SourceMeta:
    label: str
    sha256: str
    bytes: int


@dataclass(frozen=True)
class Case:
    schema: tuple[SqlStatement, ...]
    seed: tuple[SqlStatement, ...]
    migrations: tuple[Migration, ...]
    keys: dict[str, tuple[str, ...]] = field(default_factory=dict)
    assertions: tuple[AssertionSpec, ...] = ()
    allowances: tuple[Allowance, ...] = ()
    sources: tuple[SourceMeta, ...] = ()
    _input_paths: tuple[Path, ...] = field(default=(), repr=False, compare=False)


@dataclass
class TableSnapshot:
    schema_digest: str
    row_count: int
    identity_mode: str
    key_columns: tuple[str, ...]
    key_digest: str | None
    row_digest: str
    column_digests: dict[str, str]
    null_counts: dict[str, int]
    sequence_digest: str | None = None
    _schema: Any = field(default=None, repr=False)
    _rows: Any = field(default=None, repr=False)
    _key_index: Any = field(default=None, repr=False)
    _identity_unreliable: bool = field(default=False, repr=False)
    _sequence_rows: Any = field(default=None, repr=False)


@dataclass
class Snapshot:
    stage_id: str
    source: SourceRef | None
    committed: bool
    schema_digest: str
    tables: dict[str, TableSnapshot]
    id: str = ''
    state: str = 'provisional'
    _schema: Any = field(default=None, repr=False)


@dataclass(frozen=True)
class Diagnostic:
    code: str
    source: SourceRef | None = None
    stage_id: str | None = None
    message: str = ''

    def __post_init__(self):
        if self.code not in CODES:
            raise ValueError('Unknown diagnostic code')
        object.__setattr__(self, 'message', self.code.replace('_', ' '))


@dataclass(frozen=True)
class CheckViolation:
    table: str
    constraint_index: int
    count: int


@dataclass(frozen=True)
class CheckResult:
    kind: str
    passed: bool | None
    violations: int | None
    tables: tuple[str, ...]
    code: str | None = None
    details: tuple[CheckViolation, ...] = ()


@dataclass(frozen=True)
class AssertionResult:
    id: str
    stage_id: str
    status: str
    code: str | None = None


@dataclass
class StageResult:
    id: str
    migration: str | None
    phase: str
    status: str
    snapshots: tuple[Snapshot, ...] = ()
    checks: tuple[CheckResult, ...] = ()
    assertions: tuple[AssertionResult, ...] = ()
    final_snapshot_id: str | None = None


@dataclass
class RunTrace:
    runtime: dict[str, str | None]
    stages: tuple[StageResult, ...]
    diagnostics: tuple[Diagnostic, ...]
    complete: bool
    _context: Any = field(default=None, repr=False)
    _observations: tuple[dict, ...] = field(default=(), repr=False)
    _case: Any = field(default=None, repr=False)
    _run_id: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class Finding:
    kind: str
    severity: str
    stage_id: str | None
    table: str | None
    columns: tuple[str, ...]
    count: int | None
    source: SourceRef | None
    allowance_id: str | None
    evidence: dict
    id: str = ''


@dataclass
class Report:
    schema_version: int
    runtime: dict
    sources: tuple[SourceMeta, ...]
    limits: dict
    stages: tuple[dict, ...]
    comparisons: tuple[dict, ...]
    assertions: tuple[dict, ...]
    findings: tuple[Finding, ...]
    allowance_usage: tuple[dict, ...]
    summary: dict
    diagnostics: tuple[Diagnostic, ...] = ()
    _public_model: dict | None = field(default=None, repr=False)
    _context: Any = field(default=None, repr=False)


@dataclass(frozen=True)
class Summary:
    status: str
    exit_code: int
    unexpected_count: int
    expected_count: int
    assertion_failed_count: int
    complete: bool


@dataclass(frozen=True)
class OutputRequest:
    destination: Path
    input_paths: tuple[Path, ...]
