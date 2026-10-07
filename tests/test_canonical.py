import importlib.util
import math
import sqlite3
import unittest

from migreplay.model import ExecutionFailed


class CanonicalTests(unittest.TestCase):
    def api(self):
        self.assertIsNotNone(importlib.util.find_spec('migreplay.canonical'), 'canonical producer is missing')
        from migreplay import canonical
        return canonical

    def test_canonical_typed_values(self):
        c = self.api()
        expected = [(None, b'N\0\0\0\0\0\0\0\0'),
                    (1, b'I\0\0\0\0\0\0\0\x011'),
                    (1.0, b'F\0\0\0\0\0\0\0\x140x1.0000000000000p+0'),
                    ('1', b'T\0\0\0\0\0\0\0\x011'),
                    (b'\x00\xff', b'B\0\0\0\0\0\0\0\x02\x00\xff')]
        for value, want in expected:
            self.assertEqual(c.encode_value(value), want)
        self.assertNotEqual(c.encode_value(None), c.encode_value('NULL'))
        self.assertNotEqual(c.encode_value('é'), c.encode_value('e\u0301'))
        self.assertNotEqual(c.encode_value(b'\x00\xff'), c.encode_value('00ff'))
        self.assertNotEqual(c.encode_value(-0.0), c.encode_value(0.0))
        self.assertTrue(c.encode_row((1,)).startswith(b'MR1R\0\0\0\0\0\0\0\x01'))
        for value in (math.inf, -math.inf, math.nan, object(), True):
            with self.assertRaises(ExecutionFailed) as caught:
                c.encode_value(value)
            self.assertEqual(caught.exception.code, 'unsupported_value')

    def test_multiset_retains_duplicates_and_ignores_order(self):
        c = self.api()
        rows = [c.encode_row(('x',)), c.encode_row(('x',)), c.encode_row((None,))]
        self.assertEqual(c.digest_multiset(rows), c.digest_multiset(list(reversed(rows))))
        self.assertNotEqual(c.digest_multiset(rows), c.digest_multiset(rows[1:]))

    def test_observed_sqlite_zero_is_encoded_as_observed(self):
        c = self.api()
        with sqlite3.connect(':memory:') as conn:
            value = conn.execute('SELECT -0.0').fetchone()[0]
            self.assertEqual(c.encode_value(value), b'F\0\0\0\0\0\0\0\x09-0x0.0p+0')

    def test_schema_restoration_is_conservative(self):
        c = self.api()
        self.assertEqual(c.normalize_sql("CREATE TABLE t (x TEXT /* hi */ DEFAULT 'a  b');"), c.normalize_sql("CREATE  TABLE t(x TEXT DEFAULT 'a  b')"))
        self.assertNotEqual(c.normalize_sql("CREATE TABLE t(x TEXT DEFAULT 'a  b')"), c.normalize_sql("CREATE TABLE t(x TEXT DEFAULT 'a b')"))
        self.assertNotEqual(c.normalize_sql('CREATE TABLE t(x,y)'), c.normalize_sql('CREATE TABLE t(y,x)'))
        self.assertIsNone(c.normalize_sql("CREATE TABLE t(x DEFAULT 'unfinished)"))
