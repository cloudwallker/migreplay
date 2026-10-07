"""One status policy, explicit public projection, and safe failure factory."""
from dataclasses import asdict
from types import MappingProxyType

from .model import CODES, Diagnostic, ExecutionFailed, Finding, Limits, Report, Summary

DIFFERENCES = frozenset('column_missing table_missing value_changed rows_added rows_removed keys_changed schema_not_restored schema_text_diff'.split())
INPUT_CODES = frozenset('invalid_input input_limit invalid_path invalid_utf8 invalid_sql unknown_stage'.split())
TABLE_FIELDS = 'schema_digest row_count identity_mode key_columns key_digest row_digest column_digests null_counts sequence_digest'.split()
EVIDENCE_FIELDS = 'comparison_id before_digest after_digest before_count after_count snapshot_id'.split()


def evidence(**values):
    return {key: values.get(key) for key in EVIDENCE_FIELDS}


def source_view(source):
    if source is None:
        return None
    get = source.get if isinstance(source, dict) else lambda key: getattr(source, key)
    return {key: get(key) for key in ('file', 'statement', 'start_line', 'end_line')}


def table_view(table):
    if table is None:
        return None
    get = table.get if isinstance(table, dict) else lambda key: getattr(table, key)
    value = {key: get(key) for key in TABLE_FIELDS}
    value['key_columns'] = list(value['key_columns'])
    for key in ('column_digests', 'null_counts'):
        value[key] = dict(sorted(value[key].items()))
    return value


def assertion_view(value):
    get = value.get if isinstance(value, dict) else lambda key: getattr(value, key)
    return {key: get(key) for key in ('id', 'stage_id', 'status', 'code')}


def snapshot_view(value):
    return dict(id=value.id, stage_id=value.stage_id, source=source_view(value.source), state=value.state,
                committed=value.committed, schema_digest=value.schema_digest,
                tables={name: table_view(table) for name, table in sorted(value.tables.items())})


def stage_view(stage):
    return dict(id=stage.id, migration=stage.migration, phase=stage.phase, status=stage.status,
                final_snapshot_id=stage.final_snapshot_id, snapshots=[snapshot_view(s) for s in stage.snapshots],
                checks=[dict(kind=c.kind, passed=c.passed, violations=c.violations, tables=list(c.tables), code=c.code,
                             details=[dict(table=d.table, constraint_index=d.constraint_index, count=d.count) for d in c.details]) for c in stage.checks],
                assertions=[assertion_view(a) for a in stage.assertions])


def summarize(findings, assertions, *, complete, invalid=False):
    differences = [f for f in findings if f.kind in DIFFERENCES]
    expected = sum(f.allowance_id is not None for f in differences)
    unexpected = len(differences) - expected
    failed = sum(a['status'] == 'failed' for a in assertions)
    if not complete and not invalid:
        status, code = 'incomplete', 3
    elif invalid:
        status, code = 'invalid_input', 2
    elif unexpected or failed:
        status, code = 'unexpected_loss', 1
    elif expected:
        status, code = 'expected_irreversible', 0
    else:
        status, code = 'passed', 0
    return dict(status=status, exit_code=code, unexpected_count=unexpected, expected_count=expected,
                assertion_failed_count=failed, complete=bool(complete and not invalid))


def diagnostic_kind(code):
    if code in INPUT_CODES:
        return 'invalid_input'
    if code in ('foreign_key_failed', 'integrity_failed', 'check_error'):
        return 'check_failed'
    if code in ('assertion_error', 'assertion_shape', 'assertion_false'):
        return 'assertion_error'
    if code in ('policy_denied', 'unsupported_value', 'cancelled', 'cleanup_failed'):
        return code
    if code.endswith('_limit') or code in ('limit_exceeded', 'statement_timeout', 'run_timeout'):
        return 'limit_exceeded'
    return 'execution_error'


def project_report(report, budget=None):
    """The only projection: select every nested key; never serialize RunTrace."""
    def check():
        if budget:
            budget.check_deadline()
    stages = []
    for s in report.stages:
        check()
        snapshots = []
        for v in s['snapshots']:
            check()
            snapshots.append(dict(id=v['id'], stage_id=v['stage_id'], source=source_view(v['source']), state=v['state'], committed=v['committed'], schema_digest=v['schema_digest'], tables={n: table_view(t) for n, t in sorted(v['tables'].items())}))
        checks = [dict(kind=c['kind'], passed=c['passed'], violations=c['violations'], code=c['code'], tables=list(c['tables']), details=[{k: d[k] for k in ('table', 'constraint_index', 'count')} for d in c['details']]) for c in s['checks']]
        stages.append(dict(id=s['id'], migration=s['migration'], phase=s['phase'], status=s['status'], final_snapshot_id=s['final_snapshot_id'], snapshots=snapshots, checks=checks, assertions=[assertion_view(a) for a in s['assertions']]))
    comparisons = []
    for c in report.comparisons:
        check()
        tables = []
        for t in c['table_results']:
            check()
            tables.append(dict(table=t['table'], schema_restored=t['schema_restored'], identity_mode=t['identity_mode'], before=table_view(t['before']), after=table_view(t['after']), added_count=t['added_count'], removed_count=t['removed_count'], changed_count=t['changed_count'], column_changed_counts=dict(sorted(t['column_changed_counts'].items())), incomparable_reasons=list(t['incomparable_reasons'])))
        comparisons.append(dict(id=c['id'], kind=c['kind'], before_stage_id=c['before_stage_id'], after_stage_id=c['after_stage_id'], before_snapshot_id=c['before_snapshot_id'], after_snapshot_id=c['after_snapshot_id'], status=c['status'], table_results=tables, first_observed_change=source_view(c['first_observed_change']), first_observed_snapshot_id=c['first_observed_snapshot_id'], first_failed_restoration=source_view(c['first_failed_restoration'])))
    findings = []
    for f in report.findings:
        check()
        findings.append(dict(id=f.id, kind=f.kind, severity=f.severity, stage_id=f.stage_id, table=f.table,
                             columns=list(f.columns), count=f.count, source=source_view(f.source), allowance_id=f.allowance_id, evidence=evidence(**f.evidence)))
    usage = [{k: (list(a[k]) if k in ('columns', 'matched_finding_ids') else a[k]) for k in ('id', 'migration', 'phase', 'table', 'kind', 'columns', 'reason', 'matched_finding_ids', 'unused')} for a in report.allowance_usage]
    check()
    result = dict(schema_version=1, runtime={k: report.runtime.get(k) for k in ('product', 'python', 'sqlite')},
                  sources=[dict(label=s.label, sha256=s.sha256, bytes=s.bytes) for s in report.sources],
                  limits={k: report.limits[k] for k in Limits.__dataclass_fields__}, stages=stages, comparisons=comparisons,
                  assertions=[assertion_view(a) for a in report.assertions], findings=findings, allowance_usage=usage,
                  summary={k: report.summary[k] for k in Summary.__dataclass_fields__},
                  diagnostics=[dict(code=d.code, source=source_view(d.source), stage_id=d.stage_id, message=d.message) for d in report.diagnostics])
    report._public_model = result
    return result


def failure_report(diagnostic, runtime, limits, safe_sources=(), *, partial=None):
    if partial is not None and partial._public_model is None:
        raise ExecutionFailed('protocol_error')
    budget = None
    if partial is not None and partial._context is not None:
        from .policy import Budget
        budget = Budget(partial._context)
        if budget.limits != limits:
            raise ExecutionFailed('context_mismatch')
        budget.check_deadline()
    findings = list(partial.findings) if partial else []
    identifiers = {f.id for f in findings}
    index = len(findings) + 1
    while f'failure:{index}' in identifiers:
        index += 1
    findings.append(Finding(diagnostic_kind(diagnostic.code), 'error', diagnostic.stage_id, None, (), None,
                            diagnostic.source, None, evidence(), f'failure:{index}'))
    invalid = diagnostic.code in INPUT_CODES and (partial is None or partial.summary['status'] != 'incomplete')
    assertions = partial.assertions if partial else ()
    report = Report(1, runtime, partial.sources if partial else safe_sources, asdict(limits),
                    partial.stages if partial else (), partial.comparisons if partial else (), assertions,
                    tuple(findings), partial.allowance_usage if partial else (),
                    summarize(findings, assertions, complete=False, invalid=invalid),
                    (diagnostic,) + (partial.diagnostics if partial else ()),
                    _context=partial._context if partial else None)
    project_report(report, budget)
    return report


def exit_code(report):
    return report.summary['exit_code']


# Constructed once from the same factory, before any run starts. The supervisor
# reads immutable, bounded terminals without constructing or decoding a Report.
_FAILURE_SUMMARIES = MappingProxyType({code: Summary(**failure_report(Diagnostic(code), {}, Limits()).summary) for code in CODES})


def failure_summary(code):
    return _FAILURE_SUMMARIES[code]
