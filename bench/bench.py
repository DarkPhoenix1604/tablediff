"""Benchmark tablediff against a full comparison (naive_diff) on generated tables.

  uv run python bench/bench.py --rows 1000000 --changes 30
  uv run python bench/bench.py --rows 1000000 --changes 30 --pg postgresql://postgres:pw@localhost:5433/db

Table A and table B hold the same generated rows, then B gets `changes` mutations: a third
deleted, a third updated, a third inserted. Without --pg both tables are DuckDB files; with
--pg, A is in Postgres and B in DuckDB (a cross-database diff). Prints a Markdown table plus
the machine and settings, so the numbers can go in the README as measured.
"""

from __future__ import annotations

import argparse
import os
import platform
import tempfile

import duckdb

from tablediff import diff_tables, naive_diff, open_table

# Same expressions in both databases, so identical ids produce identical rows.
COLUMNS = """
    i AS id,
    'user' || CAST(i AS VARCHAR) AS name,
    CAST((CAST(i AS BIGINT) * 7919) % 1000000 AS DOUBLE) / 100 AS amount,
    i % 2 = 0 AS active,
    CAST(TIMESTAMP '2026-01-01 00:00:00' + (i % 100000) * INTERVAL '1 second' AS TIMESTAMP) AS created,
    CASE WHEN i % 5 = 0 THEN NULL ELSE 'note ' || CAST(i % 10 AS VARCHAR) END AS note
"""


def mutations(rows: int, changes: int) -> tuple[list[int], list[int], list[int]]:
    """Evenly spread ids to delete and update, plus new ids to insert."""
    third = max(1, changes // 3)
    step = max(1, rows // (2 * third + 1))
    picked = list(range(step, rows + 1, step))[: 2 * third]
    return picked[0::2], picked[1::2], list(range(rows + 1, rows + 1 + third))


def build_duckdb(path: str, rows: int, deletes, updates, inserts) -> None:
    con = duckdb.connect(path)
    con.execute(f"CREATE TABLE a AS SELECT {COLUMNS} FROM range(1, {rows + 1}) t(i)")
    con.execute("CREATE TABLE b AS SELECT * FROM a")
    con.execute("DELETE FROM b WHERE id IN (SELECT unnest(?))", [deletes])
    con.execute("UPDATE b SET note = 'changed' WHERE id IN (SELECT unnest(?))", [updates])
    con.execute(f"INSERT INTO b SELECT {COLUMNS} FROM (SELECT unnest(?) AS i)", [inserts])
    con.close()


def build_postgres(url: str, rows: int) -> None:
    import psycopg

    cols = COLUMNS.replace("AS DOUBLE", "AS DOUBLE PRECISION").replace("AS VARCHAR", "AS TEXT")
    with psycopg.connect(url, autocommit=True) as pg:
        pg.execute("DROP TABLE IF EXISTS bench_a")
        pg.execute(f"CREATE TABLE bench_a AS SELECT {cols} FROM generate_series(CAST(1 AS BIGINT), {rows}) t(i)")
        pg.execute("ALTER TABLE bench_a ADD PRIMARY KEY (id)")
        pg.execute("ANALYZE bench_a")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--rows", type=int, default=1_000_000)
    p.add_argument("--changes", type=int, default=30)
    p.add_argument("--pg", help="Postgres URL: put table A in Postgres for a cross-database run")
    p.add_argument("--threads", type=int, default=8)
    p.add_argument("--segments", type=int, default=64)
    p.add_argument("--factor", type=int, default=8)
    p.add_argument("--threshold", type=int, default=1024)
    args = p.parse_args()

    deletes, updates, inserts = mutations(args.rows, args.changes)
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "bench.duckdb")
        build_duckdb(path, args.rows, deletes, updates, inserts)
        duck = f"duckdb://{path}"
        if args.pg:
            build_postgres(args.pg, args.rows)

        def tables():
            a = open_table(args.pg, "bench_a", "id") if args.pg else open_table(duck, "a", "id")
            return a, open_table(duck, "b", "id")

        fast = diff_tables(*tables(), segments=args.segments, factor=args.factor, threshold=args.threshold,
                           threads=args.threads)
        slow = naive_diff(*tables())

    expected = (sorted(deletes), sorted(inserts), sorted(updates))
    assert (fast.only_in_a, fast.only_in_b, fast.changed) == expected, "tablediff missed or invented a row"
    assert (slow.only_in_a, slow.only_in_b, slow.changed) == expected, "naive diff disagrees"

    total = 2 * args.rows - len(deletes) + len(inserts)
    setup = "Postgres -> DuckDB" if args.pg else "DuckDB -> DuckDB"
    print(f"setup: {setup}; rows per table: {args.rows:,}; differing rows: {fast.total}; "
          f"threads {args.threads}, segments {args.segments}, factor {args.factor}, threshold {args.threshold}")
    print(f"machine: {platform.platform()}, {platform.processor() or platform.machine()}, "
          f"{os.cpu_count()} CPUs, Python {platform.python_version()}, duckdb {duckdb.__version__}")
    print()
    print("| method | time (s) | rows fetched | % of rows fetched | ranges checked |")
    print("|---|---|---|---|---|")
    print(f"| tablediff | {fast.seconds:.2f} | {fast.rows_fetched:,} | {100 * fast.rows_fetched / total:.3f}% | "
          f"{fast.segments_checked} in {fast.rounds} rounds |")
    print(f"| full comparison | {slow.seconds:.2f} | {slow.rows_fetched:,} | 100% | - |")


if __name__ == "__main__":
    main()
