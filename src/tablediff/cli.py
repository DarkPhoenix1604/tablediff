"""tablediff command line.

  tablediff postgresql://localhost/shop orders duckdb:///backup.duckdb orders --key id
"""

from __future__ import annotations

import argparse
import json
import sys

from .connect import open_table
from .differ import diff_tables


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="tablediff", description="Find the rows that differ between two tables.")
    p.add_argument("uri_a", help="postgresql://... or duckdb:///path")
    p.add_argument("table_a", help="table name, optionally schema.table")
    p.add_argument("uri_b")
    p.add_argument("table_b")
    p.add_argument("--key", required=True, help="integer primary-key column, same name on both sides")
    p.add_argument("--columns", help="comma-separated columns to compare (default: all except the key)")
    p.add_argument("--segments", type=int, default=32, help="key ranges in the first round (default 32)")
    p.add_argument("--factor", type=int, default=8, help="ranges a differing range splits into (default 8)")
    p.add_argument("--threshold", type=int, default=1024,
                   help="fetch rows once a differing range has at most this many (default 1024)")
    p.add_argument("--threads", type=int, default=8, help="parallel range checks (default 8)")
    p.add_argument("--json", action="store_true", help="print the result as JSON")
    args = p.parse_args(argv)

    columns = [c.strip() for c in args.columns.split(",")] if args.columns else None
    a = open_table(args.uri_a, args.table_a, args.key, columns)
    b = open_table(args.uri_b, args.table_b, args.key, columns)
    r = diff_tables(a, b, segments=args.segments, factor=args.factor, threshold=args.threshold,
                    threads=args.threads)

    if args.json:
        print(json.dumps({"only_in_a": r.only_in_a, "only_in_b": r.only_in_b, "changed": r.changed,
                          "segments_checked": r.segments_checked, "rows_fetched": r.rows_fetched,
                          "rounds": r.rounds, "seconds": round(r.seconds, 3)}, default=str))
    else:
        for k in r.only_in_a:
            print(f"- {k}")
        for k in r.only_in_b:
            print(f"+ {k}")
        for k in r.changed:
            print(f"~ {k}")
        print(f"{r.total} differing rows ({len(r.only_in_a)} only in A, {len(r.only_in_b)} only in B, "
              f"{len(r.changed)} changed); {r.segments_checked} ranges checked in {r.rounds} rounds, "
              f"{r.rows_fetched} rows fetched, {r.seconds:.2f}s", file=sys.stderr)
    return 1 if r.total else 0


if __name__ == "__main__":
    sys.exit(main())
