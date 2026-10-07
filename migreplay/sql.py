"""Physical statement locations, with SQLite deciding trigger boundaries."""
import sqlite3
from .model import InvalidInput, SourceRef, SqlStatement


def split_sql(text: str, label: str) -> tuple[SqlStatement, ...]:
    text = text.removeprefix('\ufeff')
    statements = []
    start = None
    line = 1
    start_line = 1
    quote = None
    comment = None
    i = 0
    while i < len(text):
        char = text[i]
        nxt = text[i:i + 2]
        if comment == 'line':
            if char == '\n':
                comment = None
        elif comment == 'block':
            if nxt == '*/':
                comment = None
                i += 1
        elif quote:
            if char == quote:
                if quote != ']' and i + 1 < len(text) and text[i + 1] == quote:
                    i += 1
                else:
                    quote = None
        elif nxt in ('--', '/*'):
            comment = 'line' if nxt == '--' else 'block'
            i += 1
        elif not char.isspace():
            if start is None:
                start, start_line = i, line
            if char in "'\"`[":
                quote = ']' if char == '[' else char
            elif char == ';' and sqlite3.complete_statement(text[start:i + 1]):
                source = SourceRef(label, len(statements) + 1, start_line, line)
                statements.append(SqlStatement(text[start:i + 1], source))
                start = None
        if char == '\n':
            line += 1
        i += 1
    if start is not None or quote or comment == 'block':
        raise InvalidInput('invalid_sql', SourceRef(label, len(statements) + 1, start_line, line))
    return tuple(statements)
