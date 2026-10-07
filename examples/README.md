# Synthetic migration cases / 合成迁移案例

All SQL and values in this directory are synthetic. Each `expected.json` is handwritten and checked by the test suite. Run any manifest from the repository root with a fresh output directory:

```sh
python -m migreplay run --manifest examples/notes-loss/case.json --out-dir reports/notes-loss
```

| Directory / manifest | Expected result / 手写预期 |
| --- | --- |
| `notes-loss/case.json` | Exit 1: schema restored, ten body values lost; NULL 0 → 10 |
| `notes-preserved/case.json` | Exit 0: a regular backup table restores body values and passes assertions |
| `null-default/case.json` | Exit 1: one NULL becomes a replacement value |
| `null-default/allowed.json` | Exit 0: the exact declared change is expected_irreversible |
| `foreign-key-rebuild/case.json` | Exit 1: rebuilt table omits one child row |
| `foreign-key-rebuild/preserved.json` | Exit 0: all child rows retained |
| `foreign-key-rebuild/invalid-reference.json` | Exit 3: foreign-key failure stops execution |

`demo` uses packaged copies of notes-loss and notes-preserved. Keep those copies synchronized when changing the two public examples. Additional edge cases live in the tests: duplicate rows without keys, nullable/composite keys, blobs, storage types, generated columns, indexes, triggers, sequence state, and multiple migrations.

本目录全部数据为合成数据。贡献新案例时先独立写出预期，保留真实 SQL、坏/好变体、准确计数与断言。`demo` 使用包内 notes-loss/notes-preserved 副本，修改示例时同步副本。其他边界由测试中的真实 SQL 覆盖。
