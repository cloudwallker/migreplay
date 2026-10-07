"""Offline presentation and exclusively owned, bounded report output."""
import html
import json
import os
from pathlib import Path, PureWindowsPath
import stat

from .inputs import no_links
from .model import ExecutionFailed, InvalidInput, LimitExceeded, Limits, SafeError
from .policy import Budget

FILE_NAMES = ('report.json.part', 'report.html.part', 'report.json', 'report.html')
MAX_DIRS = 64
LEASE_SIZE = 1 + MAX_DIRS * 3 + len(FILE_NAMES) * 2


def validate_destination(destination, input_paths):
    destination = Path(destination)
    components = [p for p in destination.parts if p != destination.anchor]
    if '..' in components or any(':' in p or p.endswith((' ', '.')) or PureWindowsPath(p).is_reserved() for p in components):
        raise InvalidInput('invalid_path')
    destination = Path(os.path.abspath(destination))
    try:
        existing = destination
        missing = []
        while not os.path.lexists(existing):
            missing.append(existing)
            existing = existing.parent
        no_links(existing)
        if not missing or len(missing) > MAX_DIRS or not existing.is_dir():
            raise InvalidInput('invalid_path')
        for input_path in input_paths:
            protected = Path(os.path.abspath(input_path))
            if destination == protected or destination in protected.parents:
                raise InvalidInput('invalid_path')
        return destination
    except (OSError, ValueError):
        raise InvalidInput('invalid_path') from None


def _identity(path):
    return no_links(path)[-1][1:]


def _create_owned(destination, input_paths, lease):
    validate_destination(destination, input_paths)
    missing, current = [], destination
    while not current.exists():
        missing.append(current)
        current = current.parent
    parent_identity = _identity(current)
    for directory in reversed(missing):
        if _identity(directory.parent) != parent_identity:
            raise InvalidInput('invalid_path')
        directory.mkdir(mode=0o700)
        identity = _identity(directory)
        offset = 1 + int(lease[0]) * 3
        lease[offset:offset + 3] = (len(directory.relative_to(destination.anchor).parts), *identity)
        lease[0] += 1
        parent_identity = identity


def cleanup_output(destination, lease):
    """Only remove recorded objects whose current identities still match."""
    if not lease[0]:
        return
    try:
        last = 1 + (int(lease[0]) - 1) * 3
        if destination.exists():
            if _identity(destination) != tuple(lease[last + 1:last + 3]):
                raise ExecutionFailed('cleanup_failed')
            for index, name in enumerate(FILE_NAMES):
                offset = 1 + MAX_DIRS * 3 + index * 2
                identity = tuple(lease[offset:offset + 2])
                path = destination / name
                if any(identity) and os.path.lexists(path):
                    info = path.lstat()
                    own_links = sum(1 for other in FILE_NAMES if os.path.lexists(destination / other) and _identity(destination / other) == identity)
                    if _identity(path) != identity or not stat.S_ISREG(info.st_mode) or info.st_nlink > own_links:
                        raise ExecutionFailed('cleanup_failed')
                    path.unlink()
        for index in reversed(range(int(lease[0]))):
            offset = 1 + index * 3
            depth = int(lease[offset])
            directory = Path(destination.anchor).joinpath(*destination.relative_to(destination.anchor).parts[:depth])
            if _identity(directory) != tuple(lease[offset + 1:offset + 3]):
                raise ExecutionFailed('cleanup_failed')
            directory.rmdir()
        lease[0] = 0
    except (OSError, SafeError):
        raise ExecutionFailed('cleanup_failed') from None


def _budget(report):
    return Budget(report._context) if report._context is not None else None


def report_to_dict(report):
    """Consume the producer's explicit nested whitelist, never dataclass fields."""
    if report._public_model is None:
        raise ExecutionFailed('protocol_error')
    budget = _budget(report)
    if budget:
        budget.check_deadline()
    return report._public_model


STYLE = '''
:root{color-scheme:light;--ink:#142c38;--muted:#48616e;--line:#cddce0;--paper:#f4f8f8;--accent:#096a69}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.55 system-ui,sans-serif}
main{max-width:1160px;margin:auto;padding:32px 24px 80px}header{padding:24px 0;border-bottom:3px solid var(--accent)}
h1{font-size:clamp(2rem,5vw,3.5rem);line-height:1.1;margin:12px 0}h2{margin-top:32px}h3{margin:4px 0 12px}
a{color:#075d66}a:focus-visible,summary:focus-visible{outline:3px solid #b4440a;outline-offset:5px}
.eyebrow{font-weight:700;letter-spacing:.12em;font-size:.8rem}.status{display:inline-block;background:#d9eeee;border:1px solid #88b7b7;padding:8px 14px;border-radius:6px;font-weight:700}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,220px),1fr));gap:14px;margin:22px 0}.card,article,details{background:white;border:1px solid var(--line);border-radius:8px;padding:18px;min-width:0}
.card strong{font-size:1.6rem;display:block}nav{display:flex;gap:20px;flex-wrap:wrap;margin:22px 0}
article{margin:14px 0}details{margin:12px 0}summary{cursor:pointer;font-weight:650}p,dd,dt,h2,h3,summary,a,td,th{overflow-wrap:anywhere}
dl{display:grid;grid-template-columns:minmax(110px,1fr) minmax(0,3fr);gap:8px 16px}dt{color:var(--muted)}dd{margin:0}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.5 ui-monospace,monospace;margin:0}.pair{display:grid;grid-template-columns:1fr 1fr;gap:14px}.pair>div{min-width:0}
.evidence{padding:14px;background:#f1f6f7;border-radius:6px}.notice{border-left:4px solid #b4440a;padding:10px 16px;background:#fff4e9}
footer{color:var(--muted);margin-top:36px;font-size:.85rem}@media(max-width:600px){main{padding:18px 14px 44px}.pair{grid-template-columns:1fr}dl{grid-template-columns:1fr}dd{margin-bottom:8px}article,details{padding:14px}}
'''


def render_html(report):
    model, budget = report_to_dict(report), _budget(report)
    cap = report.limits['html_bytes']
    parts, size = [], 0
    def add(value):
        nonlocal size
        if budget:
            budget.check_deadline()
        size += len(value.encode('utf-8'))
        if size > cap:
            raise LimitExceeded('html_limit')
        parts.append(value)
    def esc(value):
        return html.escape(str(value), quote=True)
    def data(value):
        return '<pre>' + esc(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)) + '</pre>'
    summary = model['summary']
    add('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'">'
        '<title>MigReplay · Migration rehearsal</title><style>' + STYLE + '</style></head><body><main id="top">')
    add('<header><div class="eyebrow">MIGREPLAY / SQLITE REHEARSAL</div><h1>What survived the rollback?</h1>'
        '<p>结构、数据与不变量 · Schema, data and invariants</p><span class="status">' + esc(summary['status']) + '</span></header>')
    add('<div class="cards">')
    for label, key in [('Exit code', 'exit_code'), ('Unexpected findings', 'unexpected_count'), ('Allowed findings', 'expected_count'), ('Failed assertions', 'assertion_failed_count')]:
        add('<div class="card">' + label + '<strong>' + esc(summary[key]) + '</strong></div>')
    add('</div><nav aria-label="Report sections"><a href="#comparisons">Comparisons</a><a href="#stages">Stages</a><a href="#findings">Findings</a><a href="#rules">Allowances</a><a href="#metadata">Run metadata</a></nav>')
    add('<section id="comparisons"><h2>Restoration evidence / 恢复证据</h2>')
    for comparison in model['comparisons']:
        add('<article><h3>' + esc(comparison['before_stage_id']) + ' → ' + esc(comparison['after_stage_id']) + '</h3><p class="status">' + esc(comparison['status']) + '</p>')
        add('<details><summary>Comparison locations and committed snapshot IDs</summary>' + data({k: v for k, v in comparison.items() if k != 'table_results'}) + '</details>')
        for table in comparison['table_results']:
            add('<h3>' + esc(table['table']) + '</h3>')
            if table['schema_restored'] and table['changed_count']:
                add('<p class="notice">结构恢复，数据未恢复 · Schema restored, data not restored. Changed rows: ' + esc(table['changed_count']) + '.</p>')
            before, after = table['before'], table['after']
            if before and after:
                for column, count in table['column_changed_counts'].items():
                    if count:
                        add('<p>' + esc(column) + ': changed ' + esc(count) + ' · NULL ' + esc(before['null_counts'].get(column)) + ' → ' + esc(after['null_counts'].get(column)) + '</p>')
            add('<div class="pair"><div><h4>Before</h4><div class="evidence">' + data(before) + '</div></div><div><h4>After</h4><div class="evidence">' + data(after) + '</div></div></div>')
            add('<details><summary>Counts and comparability</summary>' + data({k: v for k, v in table.items() if k not in ('before', 'after')}) + '</details>')
        add('</article>')
    add('</section><section id="stages"><h2>Execution stages / 执行阶段</h2>')
    for stage in model['stages']:
        add('<details><summary>' + esc(stage['id']) + ' · ' + esc(stage['status']) + '</summary>' + data(stage) + '</details>')
    add('<details><summary>All assertion instances</summary>' + data(model['assertions']) + '</details></section>')
    add('<section id="findings"><h2>Findings and diagnostics</h2>' + data(model['findings']) + data(model['diagnostics']) + '</section>')
    add('<section id="rules"><h2>Allowances / 允许规则</h2>' + data(model['allowance_usage']) + '</section>')
    add('<section id="metadata"><h2>Run metadata</h2><details><summary>Runtime, source digests and limits</summary>' + data({k: model[k] for k in ('schema_version', 'runtime', 'sources', 'limits', 'summary')}) + '</details></section>')
    add('<footer>Canonical typed-value summaries from this synthetic rehearsal. Unknown counts are null; known zero counts are 0. <a href="#top">Back to top</a></footer></main></body></html>')
    return ''.join(parts)


def _encoded_chunks(parts, cap, budget):
    size = 0
    for part in parts:
        for offset in range(0, len(part), 8192):
            if budget:
                budget.check_deadline()
            chunk = part[offset:offset + 8192].encode('utf-8')
            size += len(chunk)
            if size > cap:
                raise LimitExceeded('output_limit')
            yield chunk


def _write_chunk(stream, chunk):
    stream.write(chunk)


def write_reports(report, destination, input_paths):
    destination = validate_destination(destination, input_paths)
    context = report._context
    budget = _budget(report)
    if context is not None and getattr(context, '_sink', None) is not None:
        if destination != context._sink.destination or tuple(input_paths) != context._sink.input_paths:
            raise ExecutionFailed('context_mismatch')
    lease = getattr(context, '_output_lease', None) if context is not None else None
    if lease is None:
        lease = [0] * LEASE_SIZE
    try:
        if budget:
            budget.check_deadline()
        model = report_to_dict(report)
        markup = render_html(report)
        _create_owned(destination, input_paths, lease)
        identity = _identity(destination)
        encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False, indent=2)
        for index, (parts, cap) in enumerate(((encoder.iterencode(model), report.limits['json_bytes']), ((markup,), report.limits['html_bytes']))):
            if budget:
                budget.check_deadline()
            if _identity(destination) != identity:
                raise InvalidInput('invalid_path')
            temp = destination / FILE_NAMES[index]
            fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_BINARY', 0), 0o600)
            with os.fdopen(fd, 'wb') as stream:
                info = os.fstat(stream.fileno())
                offset = 1 + MAX_DIRS * 3 + index * 2
                lease[offset:offset + 2] = (info.st_dev, info.st_ino)
                for chunk in _encoded_chunks(parts, cap, budget):
                    _write_chunk(stream, chunk)
                stream.flush()
            if budget:
                budget.check_deadline()
        for index in range(2):
            if _identity(destination) != identity:
                raise InvalidInput('invalid_path')
            source, target = destination / FILE_NAMES[index], destination / FILE_NAMES[index + 2]
            offset = 1 + MAX_DIRS * 3 + index * 2
            expected = tuple(lease[offset:offset + 2])
            if _identity(source) != expected or source.stat().st_nlink != 1:
                raise ExecutionFailed('output_failed')
            # An exclusive hard link publishes without replacing a raced-in file.
            # Register its expected identity before publishing so a hard kill
            # between link and unlink still leaves both names reclaimable.
            lease[offset + 4:offset + 6] = expected
            os.link(source, target)
            if _identity(source) != expected or _identity(target) != expected:
                raise ExecutionFailed('output_failed')
            source.unlink()
            lease[offset:offset + 2] = (0, 0)
        if budget:
            budget.check_deadline()
        return destination / 'report.json', destination / 'report.html'
    except BaseException as exc:
        cleanup_output(destination, lease)
        if isinstance(exc, (SafeError, KeyboardInterrupt)):
            raise
        raise ExecutionFailed('output_failed') from None
