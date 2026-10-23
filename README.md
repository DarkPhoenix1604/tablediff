# tablediff

Find the rows that differ between two tables, within or across databases (PostgreSQL, DuckDB), without copying either table.

Built for checking data migrations: after moving a table from one database to another, tablediff tells you exactly which rows were lost, added or changed. Each database does most of the work itself, so only a tiny fraction of rows ever crosses the wire.

```
$ tablediff postgresql://localhost/shop orders duckdb:///warehouse.duckdb orders --key id
- 1048          # only in A
+ 5000001       # only in B
~ 734212        # in both, values differ
...
30 differing rows (10 only in A, 10 only in B, 10 changed); 568 ranges checked in 4 rounds, 6388 rows fetched, 4.90s
```
(Example output; the summary line matches the 5M-row benchmark below.)

## How it works

```mermaid
flowchart TD
    S[Split key range min..max into N ranges] --> C
    C["For each range, in parallel:<br/>ask both databases for<br/>count(*) + md5 of all its rows"] --> Q{same on both sides?}
    Q -- yes --> D[range matches: no rows leave the database]
    Q -- "no, and > threshold rows" --> B[split the range into F smaller ranges] --> C
    Q -- "no, and <= threshold rows" --> F["fetch (key, row hash) from both sides<br/>compare in Python"] --> R[only in A / only in B / changed]
```

1. **Range checksums.** For a key range, each database computes `count(*)` and one hash of all its rows. Equal on both sides means the range matches.
2. **Bisection.** A range that differs is split into `--factor` smaller ranges and checked again, so the search narrows only where differences are.
3. **Row level.** Once a differing range holds at most `--threshold` rows, tablediff fetches `(key, row hash)` pairs from both sides and compares them.
4. **Parallel rounds.** Each round's ranges are checked on a thread pool (`--threads`), one connection per worker thread per database. Rounds run one after another, so workers never wait on each other.

**Cross-database hashing.** The two databases must turn the same row into the same text before hashing. Each dialect normalizes every column to a canonical string: NULL becomes `\N`, floats are rounded to 6 decimals, booleans print as `true`/`false`, and timestamps print as ISO 8601 (UTC for `timestamptz`). Columns are joined with `|`, and both sides hash with `md5`. A test proves that identical rows in PostgreSQL and DuckDB produce identical hashes.

## Run it

```bash
uv sync
uv run tablediff postgresql://user:pass@localhost:5432/db orders duckdb:///copy.duckdb orders --key id
uv run tablediff ... --columns name,amount --json     # compare a subset of columns, JSON output
```

Options: `--segments` (first-round ranges, default 32), `--factor` (split factor, default 8), `--threshold` (rows before fetching, default 1024), `--threads` (default 8). Exit code 1 means the tables differ.

From Python:

```python
from tablediff import diff_tables, open_table

result = diff_tables(open_table("postgresql://...", "orders", "id"),
                     open_table("duckdb:///copy.duckdb", "orders", "id"))
print(result.only_in_a, result.only_in_b, result.changed)
```

## Tests

```bash
uv run pytest                                   # DuckDB tests
docker run -d -e POSTGRES_PASSWORD=tablediff -e POSTGRES_DB=tablediff -p 5433:5432 postgres:17
TABLEDIFF_PG_URL=postgresql://postgres:tablediff@localhost:5433/tablediff uv run pytest   # + cross-database test
```

The tests compare tablediff against a full row-by-row comparison on random data with inserts, deletes, updates, NULLs, floats, booleans and timestamps. They also cover empty tables, column subsets, and that only a small fraction of rows is fetched when few rows differ. CI runs all of them against a PostgreSQL 17 service container.

## Benchmarks

`bench/bench.py` generates the same rows in both tables, then applies 30 changes to table B (10 deletes, 10 updates, 10 inserts). It times `diff_tables` against a full comparison that fetches every row's hash from both sides, and asserts that both find exactly the 30 changes.

Measured on 2026-10-23, single run per row. Setup: MacBook (Apple silicon, 10 CPUs), Python 3.13.17, DuckDB 2.0.0, PostgreSQL 17 in Docker (Colima, 4 CPUs, 6 GB) on the same machine. Settings: threads 8, segments 64, factor 8, threshold 1024.

| setup | rows per table | method | time | rows fetched |
|---|---|---|---|---|
| DuckDB → DuckDB | 1,000,000 | tablediff | 0.45 s | 10,234 (0.512%) |
| DuckDB → DuckDB | 1,000,000 | full comparison | 0.62 s | 2,000,000 |
| PostgreSQL → DuckDB | 1,000,000 | tablediff | 0.98 s | 10,234 (0.512%) |
| PostgreSQL → DuckDB | 1,000,000 | full comparison | 1.98 s | 2,000,000 |
| PostgreSQL → DuckDB | 5,000,000 | tablediff | 4.90 s | 6,388 (0.064%) |
| PostgreSQL → DuckDB | 5,000,000 | full comparison | 11.22 s | 10,000,000 |

Both databases ran on one machine, so no network was involved. Over a real network, the full comparison also pays to transfer every row, while tablediff transfers almost none; that case isn't measured here. Reproduce with:

```bash
uv run python bench/bench.py --rows 5000000 --changes 30 --pg postgresql://postgres:tablediff@localhost:5433/tablediff
```

## Limits

- The key must be a single integer column with the same name on both sides.
- Floats are compared at 6 decimal places. In DuckDB, DECIMAL values are converted through DOUBLE, so very large decimals can lose precision.
- Only PostgreSQL and DuckDB so far. DuckDB can also read Parquet and Delta files, so they can be compared through a DuckDB view.

## License

MIT, see [LICENSE](LICENSE).
