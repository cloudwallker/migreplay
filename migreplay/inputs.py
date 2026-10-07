"""Read-only, single-read manifest and source validation."""
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import stat

from .model import Allowance, AssertionSpec, Case, InvalidInput, Limits, Migration, SourceMeta
from .sql import split_sql


def _bad():
    raise InvalidInput('invalid_input')


def no_links(path: Path):
    """Validate each existing ancestor, including Windows junctions/reparse points."""
    chain = list(reversed(path.parents)) + [path]
    identities = []
    for part in chain:
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise InvalidInput('invalid_path')
        identities.append((part, info.st_dev, info.st_ino))
    return identities


def _read(path: Path, cap: int) -> bytes:
    try:
        identities = no_links(path)
        flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != identities[-1][1:]:
                raise InvalidInput('invalid_path')
            if info.st_size > cap:
                raise InvalidInput('input_limit')
            data = stream.read(cap + 1)
        if len(data) > cap:
            raise InvalidInput('input_limit')
        if identities != no_links(path):
            raise InvalidInput('invalid_path')
        return data
    except (OSError, ValueError):
        raise InvalidInput('invalid_path') from None


def _decode(data: bytes) -> str:
    if data.startswith(b'SQLite format 3\x00') or b'\x00' in data:
        raise InvalidInput('invalid_input')
    try:
        return data.decode('utf-8-sig')
    except UnicodeError:
        raise InvalidInput('invalid_utf8') from None


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _bad()
        result[key] = value
    return result


def _shape(obj, required, optional=()):
    if not isinstance(obj, dict) or set(required) - obj.keys() or obj.keys() - set(required) - set(optional):
        _bad()


def _name(value):
    if not isinstance(value, str) or not value or '\x00' in value:
        _bad()
    return value


def _names(values):
    if not isinstance(values, list):
        _bad()
    result = tuple(_name(v) for v in values)
    if len(set(result)) != len(result):
        _bad()
    return result


def load_case(manifest: Path, limits: Limits) -> Case:
    manifest = Path(os.path.abspath(manifest))
    sources, paths, seen = [], [], {}

    def read(path, label, cap):
        canonical = os.path.normcase(str(path))
        if canonical in seen:
            return seen[canonical][0]
        data = _read(path, cap)
        decoded = _decode(data)
        seen[canonical] = (decoded, label)
        paths.append(path)
        sources.append(SourceMeta(label, hashlib.sha256(data).hexdigest(), len(data)))
        return decoded

    try:
        doc = json.loads(read(manifest, manifest.name, limits.manifest_bytes), object_pairs_hook=_pairs, parse_constant=lambda _: _bad())
    except (ValueError, RecursionError):
        raise InvalidInput('invalid_input') from None
    _shape(doc, ('format_version', 'synthetic_seed', 'schema', 'seed', 'migrations'), ('keys', 'assertions', 'allow_changes'))
    if type(doc['format_version']) is not int or doc['format_version'] != 1 or doc['synthetic_seed'] is not True:
        _bad()
    total = 0

    def sql_file(label):
        nonlocal total
        _name(label)
        win = PureWindowsPath(label)
        parts = label.replace('\\', '/').split('/')
        if win.is_absolute() or win.drive or ':' in label or any(p in ('', '.', '..') for p in parts):
            raise InvalidInput('invalid_path')
        path = manifest.parent.joinpath(*parts)
        previous = len(sources)
        text = read(path, '/'.join(parts), limits.sql_file_bytes)
        if len(sources) != previous:
            total += sources[-1].bytes
        if total > limits.sql_total_bytes:
            raise InvalidInput('input_limit')
        stmts = split_sql(text, seen[os.path.normcase(str(path))][1])
        if len(stmts) > limits.statements_per_file:
            raise InvalidInput('input_limit')
        return stmts

    schema, seed = sql_file(doc['schema']), sql_file(doc['seed'])
    migrations, ids = [], set()
    if not isinstance(doc['migrations'], list) or len(doc['migrations']) > limits.migrations:
        raise InvalidInput('input_limit')
    for obj in doc['migrations']:
        _shape(obj, ('id', 'up', 'down'))
        ident = _name(obj['id'])
        if ident in ids or any(c in ident for c in '/\\:'):
            _bad()
        ids.add(ident)
        migrations.append(Migration(ident, sql_file(obj['up']), sql_file(obj['down'])))
    if len(schema) + len(seed) + sum(2 * len(m.up) + len(m.down) for m in migrations) > limits.executed_statements:
        raise InvalidInput('input_limit')
    keys = doc.get('keys', {})
    if not isinstance(keys, dict):
        _bad()
    keys = {_name(k): _names(v) for k, v in keys.items()}
    if any(not v for v in keys.values()):
        _bad()
    stages = {'baseline'} | {f'{phase}:{ident}' for phase in ('up', 'down', 'reup') for ident in ids}
    assertions, assertion_ids = [], set()
    if not isinstance(doc.get('assertions', []), list):
        _bad()
    for obj in doc.get('assertions', []):
        _shape(obj, ('id', 'at', 'sql', 'expect'))
        ident, ats = _name(obj['id']), _names(obj['at'])
        if ident in assertion_ids or not ats:
            _bad()
        if set(ats) - stages:
            raise InvalidInput('unknown_stage')
        assertion_ids.add(ident)
        text = _name(obj['sql'])
        # Assertions permit the familiar single SELECT without a trailing semicolon.
        if len(split_sql(text if text.rstrip().endswith(';') else text + ';', manifest.name)) != 1:
            _bad()
        if type(obj['expect']) is not int or obj['expect'] not in (0, 1):
            _bad()
        assertions.append(AssertionSpec(ident, ats, text, obj['expect']))
    allowances, allowance_ids = [], set()
    if not isinstance(doc.get('allow_changes', []), list):
        _bad()
    for obj in doc.get('allow_changes', []):
        _shape(obj, ('id', 'migration', 'phase', 'table', 'kind', 'columns', 'reason'))
        ident, columns = _name(obj['id']), _names(obj['columns'])
        for key in ('migration', 'phase', 'kind'):
            _name(obj[key])
        if ident in allowance_ids or obj['migration'] not in ids or obj['phase'] not in ('down', 'reup'):
            _bad()
        kind = obj['kind']
        if kind not in ('column_missing', 'value_changed', 'rows_added', 'rows_removed', 'table_missing') or bool(columns) != (kind in ('column_missing', 'value_changed')):
            _bad()
        allowance_ids.add(ident)
        allowances.append(Allowance(ident, obj['migration'], obj['phase'], _name(obj['table']), kind, columns, _name(obj['reason'])))
    return Case(schema, seed, tuple(migrations), keys, tuple(assertions), tuple(allowances), tuple(sources), tuple(paths))
