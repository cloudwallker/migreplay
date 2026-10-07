"""Canonical v1 byte framing and conservative SQL token normalization."""
import hashlib
import json
import math
import re
import struct

from .model import ExecutionFailed

LENGTH = struct.Struct('!Q')


def encode_value(value):
    if value is None:
        tag, payload = b'N', b''
    elif type(value) is int:
        tag, payload = b'I', str(value).encode('ascii')
    elif type(value) is float and math.isfinite(value):
        tag, payload = b'F', value.hex().encode('ascii')
    elif type(value) is str:
        tag, payload = b'T', value.encode('utf-8')
    elif type(value) is bytes:
        tag, payload = b'B', value
    else:
        raise ExecutionFailed('unsupported_value')
    return tag + LENGTH.pack(len(payload)) + payload


def frame_row(values):
    return b'MR1R' + LENGTH.pack(len(values)) + b''.join(values)


def encode_row(values):
    return frame_row(tuple(encode_value(v) for v in values))


def digest_multiset(items, budget=None):
    ordered = sorted(items)
    digest = hashlib.sha256(b'MR1M' + LENGTH.pack(len(ordered)))
    for value in ordered:
        if budget:
            budget.check_deadline()
        digest.update(LENGTH.pack(len(value)))
        digest.update(value)
    return digest.hexdigest()


def metadata_bytes(value):
    return encode_value(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False))


def metadata_digest(value):
    return hashlib.sha256(b'MR1S' + metadata_bytes(value)).hexdigest()


def sql_tokens(sql):
    """Preserve token contents, including quoted identifiers and literals."""
    result, i = [], 0
    while i < len(sql):
        if sql[i].isspace():
            i += 1
        elif sql.startswith('--', i):
            end = sql.find('\n', i + 2)
            i = len(sql) if end < 0 else end + 1
        elif sql.startswith('/*', i):
            end = sql.find('*/', i + 2)
            if end < 0:
                return None
            i = end + 2
        elif sql[i] in "'\"`[":
            start, close = i, ']' if sql[i] == '[' else sql[i]
            i += 1
            while i < len(sql):
                if sql[i] == close:
                    if close != ']' and i + 1 < len(sql) and sql[i + 1] == close:
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            else:
                return None
            result.append(sql[start:i])
        else:
            match = re.match(r'[\w$]+|(?:<=|>=|<>|!=|==|\|\||->>|->)|[^\s]', sql[i:])
            token = match.group()
            result.append(token)
            i += len(token)
    if result and result[-1] == ';':
        result.pop()
    return result


def normalize_sql(sql):
    tokens = sql_tokens(sql)
    return None if tokens is None else ' '.join(tokens)


def table_definition(sql):
    tokens = sql_tokens(sql)
    if tokens is None or '(' not in tokens:
        return None
    start = tokens.index('(')
    parts, current, depth = [], [], 1
    for index in range(start + 1, len(tokens)):
        token = tokens[index]
        if token == ')':
            depth -= 1
            if depth == 0:
                parts.append(current)
                return {'head': tokens[:start], 'parts': parts, 'tail': tokens[index + 1:]}
        if token == '(':
            depth += 1
        if token == ',' and depth == 1:
            parts.append(current)
            current = []
        else:
            current.append(token)
    return None


def identifier(token):
    if token.startswith('[') and token.endswith(']'):
        return token[1:-1]
    if len(token) > 1 and token[0] in "'\"`" and token[-1] == token[0]:
        return token[1:-1].replace(token[0] * 2, token[0])
    return token
