import os
import random

import duckdb
import pytest

from tablediff import diff_tables, duckdb_table, naive_diff, open_table
from tablediff.connect import Table
from tablediff.dialects import DuckDB, Postgres
from tablediff.differ import _split

SCHEMA = """(id BIGINT PRIMARY KEY, name VARCHAR, amount DOUBLE, active BOOLEAN,
             created TIMESTAMP, note VARCHAR)"""


def make_rows(n, seed=0):
    rnd = random.Random(seed)
    rows = []
    for i in range(1, n + 1):
        rows.append((
            i,
            f"user{i}",
            round(rnd.uniform(-1e6, 1e6), 2),
            rnd.random() < 0.5,
            f"2026-01-{rnd.randint(1, 28):02d} {rnd.randint(0, 23):02d}:{rnd.randint(0, 59):02d}:00",
            None if rnd.random() < 0.2 else f"note {rnd.randint(0, 9)}",
        ))
    return rows


def duck_pair(rows_a, rows_b):
    con = duckdb.connect(":memory:")
    for t, rows in (("a", rows_a), ("b", rows_b)):
        con.execute(f"CREATE TABLE {t} {SCHEMA}")
        if rows:
            con.executemany(f"INSERT INTO {t} VALUES (?, ?, ?, ?, ?, ?)", rows)
    return con, duckdb_table(con, "a", "id"), duckdb_table(con, "b", "id")


def mutate(rows, seed=1, deletes=5, inserts=5, updates=5):
    rnd = random.Random(seed)
    rows = list(rows)
    by_id = {r[0]: r for r in rows}
    ids = sorted(by_id)
    deleted = set(rnd.sample(ids, deletes))
    remaining = [i for i in ids if i not in deleted]
    updated = set(rnd.sample(remaining, updates))
    out = []
    for r in rows:
        if r[0] in deleted:
            continue
        if r[0] in updated:
            r = r[:5] + ("changed",)
        out.append(r)
    top = max(ids)
    added = list(range(top + 1, top + 1 + inserts))
    out += [(i, f"new{i}", 1.0, True, "2026-02-01 00:00:00", None) for i in added]
    return out, sorted(deleted), sorted(added), sorted(updated)


def test_split_covers_range_without_gaps():
    for lo, hi, parts in [(0, 10, 3), (5, 6, 8), (0, 1000, 32), (-50, 50, 7)]:
        ranges = _split(lo, hi, parts)
        assert ranges[0][0] == lo and ranges[-1][1] == hi
        assert all(r[0] < r[1] for r in ranges)
        assert all(ranges[i][1] == ranges[i + 1][0] for i in range(len(ranges) - 1))


def test_identical_tables_have_no_diff():
    rows = make_rows(5000)
    _, a, b = duck_pair(rows, rows)
    r = diff_tables(a, b, segments=16, threshold=64)
    assert r.total == 0
    assert r.rows_fetched == 0


def test_finds_inserts_deletes_and_updates():
    rows = make_rows(20000)
    rows_b, deleted, added, updated = mutate(rows)
    _, a, b = duck_pair(rows, rows_b)
    r = diff_tables(a, b, segments=16, factor=4, threshold=64, threads=4)
    assert r.only_in_a == deleted
    assert r.only_in_b == added
    assert r.changed == updated


def test_matches_naive_diff():
    rows = make_rows(3000, seed=7)
    rows_b, *_ = mutate(rows, seed=9, deletes=20, inserts=3, updates=40)
    con, a, b = duck_pair(rows, rows_b)
    fast = diff_tables(a, b, segments=8, threshold=32)
    slow = naive_diff(duckdb_table(con, "a", "id"), duckdb_table(con, "b", "id"))
    assert (fast.only_in_a, fast.only_in_b, fast.changed) == (slow.only_in_a, slow.only_in_b, slow.changed)


def test_fetches_far_fewer_rows_than_naive_when_few_rows_differ():
    rows = make_rows(50000)
    rows_b, *_ = mutate(rows, deletes=2, inserts=2, updates=2)
    _, a, b = duck_pair(rows, rows_b)
    r = diff_tables(a, b, segments=32, factor=8, threshold=256)
    assert r.total == 6
    assert r.rows_fetched < 0.05 * (len(rows) + len(rows_b))


def test_null_and_value_differ():
    rows = make_rows(100)
    rows_b = [r[:5] + (None,) if r[0] == 10 and r[5] is not None else r for r in rows]
    rows_b = [r[:5] + ("x",) if r[0] == 11 and r[5] is None else r for r in rows_b]
    expected = sorted(r[0] for r, s in zip(rows, rows_b, strict=True) if r != s)
    _, a, b = duck_pair(rows, rows_b)
    assert diff_tables(a, b, threshold=4).changed == expected


def test_empty_tables():
    _, a, b = duck_pair([], [])
    assert diff_tables(a, b).total == 0


def test_one_side_empty():
    rows = make_rows(500)
    _, a, b = duck_pair(rows, [])
    r = diff_tables(a, b, threshold=16)
    assert r.only_in_a == [row[0] for row in rows]


def test_column_subset_ignores_other_columns():
    rows = make_rows(1000)
    rows_b = [r[:5] + ("different",) for r in rows]
    con, _, _ = duck_pair(rows, rows_b)
    a = duckdb_table(con, "a", "id", ["name", "amount", "active", "created"])
    b = duckdb_table(con, "b", "id", ["name", "amount", "active", "created"])
    assert diff_tables(a, b).total == 0


def test_mismatched_columns_raise():
    con, a, _ = duck_pair(make_rows(10), make_rows(10))
    con.execute("CREATE TABLE c (id BIGINT, other VARCHAR)")
    with pytest.raises(ValueError, match="column lists differ"):
        diff_tables(a, duckdb_table(con, "c", "id"))


def test_dialects_render_the_same_canonical_forms():
    # Same expression shape on both sides; real cross-database equality is tested below.
    assert "round" in Postgres().float_text('"x"') and "printf" in DuckDB().float_text('"x"')
    assert "UTC" in Postgres().timestamp_text('"t"', utc=True)


PG_URL = os.environ.get("TABLEDIFF_PG_URL")


@pytest.mark.skipif(not PG_URL, reason="set TABLEDIFF_PG_URL to run the Postgres tests")
def test_postgres_and_duckdb_hash_rows_identically(tmp_path):
    import psycopg

    rows = make_rows(5000, seed=3)
    rows_b, deleted, added, updated = mutate(rows, seed=4)
    pg_schema = """(id BIGINT PRIMARY KEY, name TEXT, amount DOUBLE PRECISION, active BOOLEAN,
                   created TIMESTAMP, note TEXT)"""
    with psycopg.connect(PG_URL, autocommit=True) as pg:
        pg.execute("DROP TABLE IF EXISTS tablediff_test")
        pg.execute(f"CREATE TABLE tablediff_test {pg_schema}")
        with pg.cursor() as cur:
            cur.executemany("INSERT INTO tablediff_test VALUES (%s, %s, %s, %s, %s, %s)", rows)

    path = str(tmp_path / "b.duckdb")
    con = duckdb.connect(path)
    for name, data in (("same", rows), ("changed", rows_b)):
        con.execute(f"CREATE TABLE {name} {SCHEMA}")
        con.executemany(f"INSERT INTO {name} VALUES (?, ?, ?, ?, ?, ?)", data)
    con.close()

    try:
        r = diff_tables(open_table(PG_URL, "tablediff_test", "id"),
                        open_table(f"duckdb://{path}", "same", "id"))
        assert r.total == 0, "identical rows must hash identically in Postgres and DuckDB"
        r = diff_tables(open_table(PG_URL, "tablediff_test", "id"),
                        open_table(f"duckdb://{path}", "changed", "id"), threshold=64)
        assert (r.only_in_a, r.only_in_b, r.changed) == (deleted, added, updated)
    finally:
        with psycopg.connect(PG_URL, autocommit=True) as pg:
            pg.execute("DROP TABLE IF EXISTS tablediff_test")


def test_table_requires_known_key():
    con, _, _ = duck_pair(make_rows(5), make_rows(5))
    t = Table(con.cursor, DuckDB(), "a", "missing")
    with pytest.raises(ValueError, match="key column"):
        t.column_types()
