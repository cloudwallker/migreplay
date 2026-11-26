"""Exact restoration comparisons over committed final snapshots."""
from collections import Counter
from dataclasses import asdict, replace

from .canonical import identifier
from .model import ExecutionFailed, Finding, Report
from .policy import Budget
from . import status


_ROW_FINDINGS = {'rows_added', 'rows_removed', 'keys_changed'}


def _row_targets(before, after):
    """Keep just the key or multiplicity differences present at the final comparison."""
    if before.identity_mode == 'key':
        original, final = before._key_index.keys(), after._key_index.keys()
        return final - original, original - final
    original, final = Counter(before._rows), Counter(after._rows)
    return ({row: original[row] for row in final - original},
            {row: original[row] for row in original - final})


def _observed_row_findings(before, observed, targets):
    if observed is None or observed.identity_mode != before.identity_mode:
        return set()
    added, removed = targets
    if before.identity_mode == 'key':
        if observed.key_columns != before.key_columns:
            return set()
        gained = not added.isdisjoint(observed._key_index)
        lost = not removed.issubset(observed._key_index)
    else:
        if _columns(before) != _columns(observed):
            return set()
        current = Counter(observed._rows)
        gained = any(current[row] > n for row, n in added.items())
        lost = any(current[row] < n for row, n in removed.items())
    kinds = set()
    if gained:
        kinds.add('rows_added')
    if lost:
        kinds.add('rows_removed')
    if gained and lost and before.identity_mode == 'key':
        kinds.add('keys_changed')
    return kinds


def _final(stage):
    if stage.status != 'committed' or stage.final_snapshot_id is None:
        return None
    matches = [s for s in stage.snapshots if s.id == stage.final_snapshot_id and s.state == 'committed' and s.committed and s.source is None]
    return matches[0] if len(matches) == 1 else None


def _columns(table):
    return [c[1] for c in table._schema['columns']]


def _residual_objects(snapshot):
    """Exact schema objects not already owned by an ordinary table comparison."""
    return [obj for obj in snapshot._schema['objects'] if obj[2] not in snapshot.tables]


def _structure_without(schema, missing):
    """Remove only the direct column declaration, never dependent constraints."""
    columns = [c[1:] for c in schema['columns'] if c[1] not in missing]
    definition = schema['definition']
    if definition is not None:
        definition = dict(definition, parts=[p for p in definition['parts'] if not p or identifier(p[0]) not in missing])
    objects = [[o[0], o[1], o[2], definition if o[0] == 'table' else o[3], o[4]] for o in schema['objects']]
    options = list(schema['options']) if schema['options'] else None
    if options:
        options[0] -= len(missing)
    return dict(columns=columns, definition=definition, objects=objects, options=options,
                indexes=schema['indexes'], foreign_keys=schema['foreign_keys'])


def _table_differences(name, before, after, budget):
    result = dict(table=name, schema_restored=None, identity_mode='incomparable', before=status.table_view(before),
                  after=status.table_view(after), added_count=None, removed_count=None, changed_count=None,
                  column_changed_counts={}, incomparable_reasons=[])
    changes = []
    if before is None or after is None:
        result.update(schema_restored=False, incomparable_reasons=['table_missing'])
        changes.append(('table_missing' if after is None else 'schema_not_restored', (), None))
        return result, changes
    left, right = _columns(before), _columns(after)
    names = sorted(set(left) | set(right))
    result['column_changed_counts'] = {n: None for n in names}
    result['schema_restored'] = before._schema == after._schema
    missing = set(left) - set(right)
    if missing:
        changes.append(('column_missing', tuple(sorted(missing)), len(missing)))
    if before._schema != after._schema:
        residual = not missing or _structure_without(before._schema, missing) != _structure_without(after._schema, set())
        if residual:
            fallback = any(o[4] for o in before._schema['objects'] + after._schema['objects'])
            changes.append(('schema_text_diff' if fallback else 'schema_not_restored', (), None))
    if before._sequence_rows != after._sequence_rows and not any(c[0] == 'schema_not_restored' for c in changes):
        changes.append(('schema_not_restored', (), None))
    if before.identity_mode == after.identity_mode == 'key' and before.key_columns == after.key_columns:
        result['identity_mode'] = 'key'
        old, new = before._key_index, after._key_index
        removed, added = old.keys() - new.keys(), new.keys() - old.keys()
        result['removed_count'], result['added_count'] = len(removed), len(added)
        common = sorted(set(left) & set(right))
        positions = [(c, left.index(c), right.index(c)) for c in common]
        counts = {c: 0 for c in common}
        changed = 0
        for key in old.keys() & new.keys():
            budget.check_deadline()
            row_changed = False
            for column, a, b in positions:
                if old[key][a] != new[key][b]:
                    counts[column] += 1
                    row_changed = True
            changed += int(row_changed)
        result['changed_count'] = changed
        result['column_changed_counts'].update(counts)
        changed_columns = tuple(c for c in common if counts[c])
        if changed_columns:
            changes.append(('value_changed', changed_columns, changed))
        if added and removed:
            changes.append(('keys_changed', before.key_columns, len(added) + len(removed)))
    elif before.identity_mode == after.identity_mode == 'multiset' and left == right:
        result['identity_mode'] = 'multiset'
        old, new = Counter(before._rows), Counter(after._rows)
        result['added_count'] = sum((new - old).values())
        result['removed_count'] = sum((old - new).values())
    else:
        reason = 'column_layout_changed' if before.identity_mode == after.identity_mode == 'multiset' else 'key_definition_changed'
        result['incomparable_reasons'].append(reason)
    for kind, key in [('rows_added', 'added_count'), ('rows_removed', 'removed_count')]:
        if result[key]:
            changes.append((kind, (), result[key]))
    return result, changes


def _different_for(finding, before, observed):
    name = finding.table
    if name is None:
        return _residual_objects(before) != _residual_objects(observed)
    a, b = before.tables.get(name), observed.tables.get(name)
    if a is None or b is None:
        return a is not b
    if finding.kind == 'value_changed':
        names_a, names_b = _columns(a), _columns(b)
        if not set(finding.columns) <= set(names_a) & set(names_b):
            return True
        if a.identity_mode == b.identity_mode == 'key' and a.key_columns == b.key_columns:
            for key in a._key_index.keys() & b._key_index.keys():
                if any(a._key_index[key][names_a.index(c)] != b._key_index[key][names_b.index(c)] for c in finding.columns):
                    return True
            return False
    if finding.kind == 'column_missing':
        return not set(finding.columns) <= set(_columns(b))
    if finding.kind in ('schema_not_restored', 'schema_text_diff'):
        return a._schema != b._schema or a._sequence_rows != b._sequence_rows
    return Counter(a._rows) != Counter(b._rows)


def analyze_trace(case, trace, limits):
    context = trace._context
    if context is None or context.limits != limits or trace._run_id != context.run_id or trace._case is not case:
        raise ExecutionFailed('context_mismatch')
    budget = Budget(context)
    budget.check_deadline()
    stages = {s.id: s for s in trace.stages}
    findings, comparisons = [], []
    usages = [dict(id=a.id, migration=a.migration, phase=a.phase, table=a.table, kind=a.kind,
                   columns=list(a.columns), reason=a.reason, matched_finding_ids=[], unused=True) for a in case.allowances]

    def add(kind, stage, table=None, columns=(), count=None, source=None, evidence=None, severity='error'):
        f = Finding(kind, severity, stage, table, columns, count, source, None,
                    evidence or status.evidence(), f'finding:{len(findings) + 1}')
        findings.append(f)
        return f

    # Each unreliable snapshot is explicit, but never makes an equal multiset incomplete.
    for stage in trace.stages:
        for snapshot in stage.snapshots:
            for name, table in sorted(snapshot.tables.items()):
                budget.check_deadline()
                if table._identity_unreliable:
                    add('identity_unreliable', stage.id, name, source=snapshot.source,
                        evidence=status.evidence(snapshot_id=snapshot.id), severity='warning')
    slots = []
    for index in range(len(case.migrations) - 1, -1, -1):
        migration = case.migrations[index]
        slots.append(('down_restore', 'baseline' if index == 0 else 'up:' + case.migrations[index - 1].id, 'down:' + migration.id))
    slots += [('reup_repeat', 'up:' + m.id, 'reup:' + m.id) for m in case.migrations]
    evidence_complete = trace.complete
    for kind, before_id, after_id in slots:
        budget.check_deadline()
        prior, later = stages[before_id], stages[after_id]
        before, after = _final(prior), _final(later)
        ident = f'{kind}:{later.migration}'
        comparison = dict(id=ident, kind=kind, before_stage_id=before_id, after_stage_id=after_id,
                          before_snapshot_id=before.id if before else None, after_snapshot_id=after.id if after else None,
                          status='incomparable', table_results=[], first_observed_change=None,
                          first_observed_snapshot_id=None, first_failed_restoration=None)
        comparisons.append(comparison)
        names = sorted(set(before.tables if before else ()) | set(after.tables if after else ()))
        if before is None or after is None:
            evidence_complete = False
            for name in names:
                a = before.tables.get(name) if before else None
                b = after.tables.get(name) if after else None
                comparison['table_results'].append(dict(table=name, schema_restored=None, identity_mode='incomparable',
                    before=status.table_view(a), after=status.table_view(b), added_count=None, removed_count=None, changed_count=None,
                    column_changed_counts={c: None for c in sorted(set(_columns(a) if a else ()) | set(_columns(b) if b else ()))},
                    incomparable_reasons=['missing_committed_snapshot']))
            continue
        start = len(findings)
        row_targets = {}
        for name in names:
            budget.check_deadline()
            a, b = before.tables.get(name), after.tables.get(name)
            result, changes = _table_differences(name, a, b, budget)
            comparison['table_results'].append(result)
            if any(change in _ROW_FINDINGS for change, _, _ in changes):
                row_targets[name] = _row_targets(a, b)
            for change, columns, count in changes:
                add(change, after_id, name, columns, count, evidence=status.evidence(comparison_id=ident,
                    before_digest=a.row_digest if a else None, after_digest=b.row_digest if b else None,
                    before_count=a.row_count if a else None, after_count=b.row_count if b else None, snapshot_id=after.id))
        if _residual_objects(before) != _residual_objects(after):
            add('schema_not_restored', after_id, evidence=status.evidence(comparison_id=ident, before_digest=before.schema_digest, after_digest=after.schema_digest, snapshot_id=after.id))
        differences = findings[start:]
        if differences:
            # First observation is an execution-ordered statement; final events
            # provide conclusions but cannot invent a SQL location.
            begin = next(i for i, s in enumerate(trace.stages) if s.id == before_id)
            end = next(i for i, s in enumerate(trace.stages) if s.id == after_id)
            pending_rows = {i for i in range(start, len(findings)) if findings[i].kind in _ROW_FINDINGS}
            for stage in trace.stages[begin + 1:end + 1]:
                for snapshot in stage.snapshots:
                    budget.check_deadline()
                    locate_first = comparison['first_observed_change'] is None
                    locate_failure = stage.id == after_id and snapshot.committed and comparison['first_failed_restoration'] is None
                    if snapshot.source is None or not (pending_rows or locate_first or locate_failure):
                        continue
                    observed_rows = {}
                    for i in range(start, len(findings)):
                        if i not in pending_rows and not (locate_first or locate_failure):
                            continue
                        budget.check_deadline()
                        f = findings[i]
                        if f.kind in _ROW_FINDINGS:
                            if f.table not in observed_rows:
                                observed_rows[f.table] = _observed_row_findings(
                                    before.tables[f.table], snapshot.tables.get(f.table), row_targets[f.table])
                            matches = f.kind in observed_rows[f.table]
                        else:
                            matches = _different_for(f, before, snapshot)
                        if not matches:
                            continue
                        if i in pending_rows:
                            findings[i] = replace(f, source=snapshot.source)
                            pending_rows.remove(i)
                        if locate_first:
                            comparison['first_observed_change'] = status.source_view(snapshot.source)
                            comparison['first_observed_snapshot_id'] = snapshot.id
                            locate_first = False
                        if locate_failure:
                            comparison['first_failed_restoration'] = status.source_view(snapshot.source)
                            locate_failure = False
            for i in range(start, len(findings)):
                f = findings[i]
                for allowance, usage in zip(case.allowances, usages):
                    if (allowance.migration, allowance.phase, allowance.table, allowance.kind, tuple(sorted(allowance.columns))) == (later.migration, later.phase, f.table, f.kind, tuple(sorted(f.columns))):
                        findings[i] = replace(f, allowance_id=allowance.id)
                        usage['matched_finding_ids'].append(f.id)
                        usage['unused'] = False
                        break
        differences = findings[start:]
        reasons = {r for t in comparison['table_results'] for r in t['incomparable_reasons']}
        if any(f.allowance_id is None for f in differences):
            comparison['status'] = 'unexpected_loss'
        elif reasons and not differences:
            evidence_complete = False
        elif reasons - {'table_missing', 'column_layout_changed'}:
            evidence_complete = False
        elif differences:
            comparison['status'] = 'expected_irreversible'
        else:
            comparison['status'] = 'restored'
    assertions = []
    observed = {(a.id, a.stage_id): a for s in trace.stages for a in s.assertions}
    for spec in case.assertions:
        for stage in trace.stages:
            if stage.id not in spec.at:
                continue
            result = observed.get((spec.id, stage.id))
            view = status.assertion_view(result) if result else dict(id=spec.id, stage_id=stage.id, status='not_started', code=None)
            assertions.append(view)
            if result and result.status != 'passed':
                add('assertion_failed' if result.status == 'failed' else 'assertion_error', stage.id)
            if result and result.status == 'error':
                evidence_complete = False
    for diagnostic in trace.diagnostics[1:]:
        add(status.diagnostic_kind(diagnostic.code), diagnostic.stage_id, source=diagnostic.source)
    report = Report(1, trace.runtime, case.sources, asdict(limits), tuple(status.stage_view(s) for s in trace.stages),
                    tuple(comparisons), tuple(assertions), tuple(findings), tuple(usages),
                    status.summarize(findings, assertions, complete=evidence_complete), trace.diagnostics[1:], _context=context)
    status.project_report(report, budget)
    if trace.diagnostics:
        return status.failure_report(trace.diagnostics[0], trace.runtime, limits, case.sources, partial=report)
    return report
