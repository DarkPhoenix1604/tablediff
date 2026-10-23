"""Open a table on either side of the diff from a URI.

  postgresql://user:pass@host:5432/db     Postgres (psycopg)
  duckdb:///path/to/file.duckdb           DuckDB file
  duckdb://:memory:                       DuckDB in-memory (tests)

Each worker thread gets its own connection: neither driver allows concurrent queries on one
connection.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import duckdb
import psycopg

from .dialects import Dialect, DuckDB, Postgres


@dataclass
class Table:
    """One side of a diff: a table, its key, the columns to compare, and how to connect."""

    connect: Callable[[], Any]
    dialect: Dialect
    name: str
    key: str
    columns: list[str] | None = None  # None = every column except the key
    _local: threading.local = field(default_factory=threading.local, repr=False)
    _opened: list[Any] = field(default_factory=list, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def conn(self) -> Any:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self.connect()
            self._local.conn = c
            with self._lock:
                self._opened.append(c)
        return c

    def query(self, sql: str, params: tuple = ()) -> list[tuple]:
        cur = self.conn().cursor()
        cur.execute(sql, params)
        return cur.fetchall()

    def close(self) -> None:
        with self._lock:
            for c in self._opened:
                c.close()
            self._opened.clear()
        self._local = threading.local()

    def column_types(self) -> list[tuple[str, str]]:
        schema, _, table = self.name.rpartition(".")
        rows = self.query(self.dialect.column_types_sql(), (schema or self.dialect.default_schema(), table))
        if not rows:
            raise ValueError(f"table {self.name!r} not found or has no columns")
        types = dict(rows)
        if self.key not in types:
            raise ValueError(f"key column {self.key!r} not in {self.name!r}")
        wanted = self.columns if self.columns is not None else [c for c in types if c != self.key]
        missing = [c for c in wanted if c not in types]
        if missing:
            raise ValueError(f"columns {missing} not in {self.name!r}")
        return [(c, types[c]) for c in wanted]


def open_table(uri: str, name: str, key: str, columns: list[str] | None = None) -> Table:
    if uri.startswith(("postgresql://", "postgres://")):
        return Table(lambda: psycopg.connect(uri, autocommit=True), Postgres(), name, key, columns)
    if uri.startswith("duckdb://"):
        path = uri[len("duckdb://"):]
        if path in ("", ":memory:"):
            raise ValueError("an in-memory DuckDB can't be shared across threads by URI; use duckdb_table()")
        return Table(lambda: duckdb.connect(path, read_only=True), DuckDB(), name, key, columns)
    raise ValueError(f"unsupported URI {uri!r}: use postgresql:// or duckdb://")


def duckdb_table(con: duckdb.DuckDBPyConnection, name: str, key: str, columns: list[str] | None = None) -> Table:
    """A table in an existing DuckDB connection; each thread gets its own cursor on it."""
    return Table(con.cursor, DuckDB(), name, key, columns)
