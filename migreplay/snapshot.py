"""Bounded snapshots from the existing guarded native SQLite connection."""
from .canonical import (LENGTH, encode_value, frame_row, digest_multiset,
                        metadata_bytes, metadata_digest, normalize_sql,
                        sql_tokens, table_definition)
from .model import LimitExceeded, Snapshot, TableSnapshot
from .policy import _controlled_guard


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def capture_snapshot(conn, stage_id, source, keys, budget):
    guard = _controlled_guard(conn, budget)
    tables, objects, definitions = {}, [], {}
    budget.begin_snapshot()
    try:
        budget.charge_snapshot(4, 0)
        with guard.mode('trusted'):
            for kind, name, table, sql in guard.rows(guard.execute('SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name')):
                if name.startswith('sqlite_'):
                    continue
                normalized = normalize_sql(sql) if sql is not None else None
                obj = [kind, name, table, normalized if normalized is not None else sql, normalized is None and sql is not None]
                budget.charge_snapshot(len(metadata_bytes(obj)), 0)
                objects.append(obj)
                if kind == 'table':
                    definitions[name] = sql
                    if len(definitions) > budget.limits.tables:
                        raise LimitExceeded('table_limit')
            options = {}
            for row in guard.rows(guard.execute('PRAGMA table_list')):
                if row[0] == 'main' and row[1] in definitions:
                    options[row[1]] = list(row[3:])
            for name in sorted(definitions):
                budget.check_deadline()
                columns = []
                for row in guard.rows(guard.execute(f'PRAGMA table_xinfo({_quote(name)})')):
                    entry = list(row)
                    if entry[4] is not None:
                        entry[4] = normalize_sql(entry[4]) or entry[4]
                    columns.append(entry)
                    if len(columns) > budget.limits.columns_per_table:
                        raise LimitExceeded('column_limit')
                indexes = []
                for row in guard.rows(guard.execute(f'PRAGMA index_list({_quote(name)})')):
                    indexes.append(list(row[1:]))
                indexes.sort(key=lambda item: item[0])
                for index in indexes:
                    index.append([list(row) for row in guard.rows(guard.execute(f'PRAGMA index_xinfo({_quote(index[0])})'))])
                foreign_keys = [list(row) for row in guard.rows(guard.execute(f'PRAGMA foreign_key_list({_quote(name)})'))]
                schema = {'columns': columns, 'indexes': indexes, 'foreign_keys': foreign_keys,
                          'options': options.get(name), 'objects': [o for o in objects if o[2] == name],
                          'definition': table_definition(definitions[name])}
                budget.charge_snapshot(len(metadata_bytes(schema)), 0)
                names = [column[1] for column in columns]
                proposed = tuple(keys.get(name, tuple(c[1] for c in sorted(columns, key=lambda c: c[5]) if c[5])))
                usable = bool(proposed) and all(c in names for c in proposed)
                key_positions = [names.index(c) for c in proposed] if usable else []
                rows, key_index = [], {}
                nulls = {column: 0 for column in names}
                column_values = {column: [] for column in names}
                budget.begin_table()
                with guard.mode('readonly'):
                    for row in guard.rows(guard.execute(f'SELECT * FROM {_quote(name)}')):
                        encoded = tuple(encode_value(value) for value in row)
                        budget.charge_snapshot(12 + sum(len(v) for v in encoded), 1)
                        rows.append(encoded)
                        for column, value, encoded_value in zip(names, row, encoded):
                            column_values[column].append(encoded_value)
                            nulls[column] += int(value is None)
                        if usable:
                            key = tuple(encoded[p] for p in key_positions)
                            if any(row[p] is None for p in key_positions) or key in key_index:
                                usable = False
                                key_index.clear()
                            else:
                                key_index[key] = encoded
                if usable:
                    for key in key_index:
                        budget.charge_snapshot(12 + sum(len(v) for v in key), 0)
                else:
                    key_index = {}
                sequence, sequence_rows = None, None
                if 'AUTOINCREMENT' in [t.upper() for t in sql_tokens(definitions[name]) or ()]:
                    sequence_rows = tuple(tuple(encode_value(v) for v in row) for row in guard.rows(guard.execute('SELECT seq FROM sqlite_sequence WHERE name=?', (name,))))
                    for row in sequence_rows:
                        budget.charge_snapshot(12 + sum(len(v) for v in row), 0)
                    sequence = digest_multiset([frame_row(r) for r in sequence_rows], budget)
                tables[name] = TableSnapshot(metadata_digest(schema), len(rows), 'key' if usable else 'multiset',
                    proposed if usable else (), digest_multiset([frame_row(k) for k in key_index], budget) if usable else None,
                    digest_multiset([frame_row(r) for r in rows], budget),
                    {c: digest_multiset(column_values[c], budget) for c in sorted(names)}, dict(sorted(nulls.items())),
                    sequence, schema, tuple(rows), key_index, bool(proposed) and not usable, sequence_rows)
        structure = {'objects': objects, 'tables': {n: t._schema for n, t in tables.items()}}
        budget.check_deadline()
        return Snapshot(stage_id, source, False, metadata_digest(structure), tables, _schema=structure)
    finally:
        budget.end_snapshot()
