"""Find differing rows by comparing checksums of key ranges, narrowing in only where they differ.

1. Split the key range [min, max] into `segments` equal ranges.
2. For each range, ask both databases for (row count, hash of all rows). Equal on both sides
   means the range matches, and none of its rows ever leave the database.
3. A range that differs is split into `factor` smaller ranges, checked again in the next round.
4. Once a differing range holds at most `threshold` rows on each side, fetch (key, row hash)
   pairs from both sides and compare them in Python.

Each round's ranges are checked in parallel on a thread pool. Rounds run one after another,
so no worker ever waits on another worker's result.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .connect import Table


@dataclass
class DiffResult:
    only_in_a: list = field(default_factory=list)
    only_in_b: list = field(default_factory=list)
    changed: list = field(default_factory=list)
    segments_checked: int = 0
    rows_fetched: int = 0
    rounds: int = 0
    seconds: float = 0.0

    @property
    def total(self) -> int:
        return len(self.only_in_a) + len(self.only_in_b) + len(self.changed)


def _split(lo: int, hi: int, parts: int) -> list[tuple[int, int]]:
    """Split [lo, hi) into at most `parts` non-empty ranges."""
    parts = max(1, min(parts, hi - lo))
    step, extra = divmod(hi - lo, parts)
    out, start = [], lo
    for i in range(parts):
        end = start + step + (1 if i < extra else 0)
        out.append((start, end))
        start = end
    return out


def diff_tables(a: Table, b: Table, *, segments: int = 32, factor: int = 8, threshold: int = 1024,
                threads: int = 8) -> DiffResult:
    start = time.perf_counter()
    cols_a, cols_b = a.column_types(), b.column_types()
    if [c for c, _ in cols_a] != [c for c, _ in cols_b]:
        raise ValueError(f"column lists differ: {[c for c, _ in cols_a]} vs {[c for c, _ in cols_b]}")

    bounds = [t.query(t.dialect.key_bounds_sql(t.name, t.key))[0] for t in (a, b)]
    lows = [lo for lo, _ in bounds if lo is not None]
    highs = [hi for _, hi in bounds if hi is not None]
    result = DiffResult()
    if not lows:
        result.seconds = time.perf_counter() - start
        return result
    lo, hi = min(lows), max(highs) + 1

    checksum_a = a.dialect.segment_checksum_sql(a.name, a.key, cols_a)
    checksum_b = b.dialect.segment_checksum_sql(b.name, b.key, cols_b)
    hashes_a = a.dialect.row_hashes_sql(a.name, a.key, cols_a)
    hashes_b = b.dialect.row_hashes_sql(b.name, b.key, cols_b)

    def check(rng: tuple[int, int]):
        """Returns ("same", None), ("split", ranges) or ("rows", (only_a, only_b, changed, fetched))."""
        (ca, ha), = a.query(checksum_a, rng)
        (cb, hb), = b.query(checksum_b, rng)
        if ca == cb and ha == hb:
            return "same", None
        if max(ca, cb) > threshold and rng[1] - rng[0] > 1:
            return "split", _split(rng[0], rng[1], factor)
        rows_a = dict(a.query(hashes_a, rng))
        rows_b = dict(b.query(hashes_b, rng))
        only_a = [k for k in rows_a if k not in rows_b]
        only_b = [k for k in rows_b if k not in rows_a]
        changed = [k for k in rows_a if k in rows_b and rows_a[k] != rows_b[k]]
        return "rows", (only_a, only_b, changed, len(rows_a) + len(rows_b))

    pending = _split(lo, hi, segments)
    try:
        with ThreadPoolExecutor(max_workers=threads) as pool:
            while pending:
                result.rounds += 1
                result.segments_checked += len(pending)
                next_round = []
                for kind, payload in pool.map(check, pending):
                    if kind == "split":
                        next_round += payload
                    elif kind == "rows":
                        only_a, only_b, changed, fetched = payload
                        result.only_in_a += only_a
                        result.only_in_b += only_b
                        result.changed += changed
                        result.rows_fetched += fetched
                pending = next_round
    finally:
        a.close()
        b.close()

    for keys in (result.only_in_a, result.only_in_b, result.changed):
        keys.sort()
    result.seconds = time.perf_counter() - start
    return result


def naive_diff(a: Table, b: Table) -> DiffResult:
    """Baseline: fetch every (key, row hash) from both sides and compare. Used by tests and bench."""
    start = time.perf_counter()
    cols_a, cols_b = a.column_types(), b.column_types()
    full = (-(2**63), 2**63 - 1)
    rows_a = dict(a.query(a.dialect.row_hashes_sql(a.name, a.key, cols_a), full))
    rows_b = dict(b.query(b.dialect.row_hashes_sql(b.name, b.key, cols_b), full))
    a.close()
    b.close()
    return DiffResult(
        only_in_a=sorted(k for k in rows_a if k not in rows_b),
        only_in_b=sorted(k for k in rows_b if k not in rows_a),
        changed=sorted(k for k in rows_a if k in rows_b and rows_a[k] != rows_b[k]),
        rows_fetched=len(rows_a) + len(rows_b),
        seconds=time.perf_counter() - start,
    )
