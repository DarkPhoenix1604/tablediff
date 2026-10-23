"""SQL that differs between databases.

The diff only works if both databases turn the same row into the same text, and the same
text into the same hash. Each dialect normalizes every column to a canonical string (NULLs,
numbers, booleans and timestamps all print alike), joins them with a separator, and hashes
with md5, which Postgres and DuckDB both provide.
"""

from __future__ import annotations

NULL_MARKER = "\\N"
SEPARATOR = "|"
FLOAT_DIGITS = 6


class Dialect:
    name = "base"

    def quote(self, ident: str) -> str:
        return '"' + ident.replace('"', '""') + '"'

    def table(self, name: str) -> str:
        return ".".join(self.quote(part) for part in name.split("."))

    def column_types_sql(self) -> str:
        """Query returning (column_name, data_type) for a table; params: schema, table."""
        return (
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position"
        )

    def default_schema(self) -> str:
        raise NotImplementedError

    def normalize(self, col: str, data_type: str) -> str:
        """SQL expression rendering one column as canonical text (NULL stays NULL)."""
        t = data_type.lower()
        q = self.quote(col)
        if "timestamp" in t:
            return self.timestamp_text(q, utc="with time zone" in t)
        if t in ("real", "double precision", "double", "float", "float4", "float8") or t.startswith("decimal") \
                or t.startswith("numeric"):
            return self.float_text(q)
        return f"CAST({q} AS VARCHAR)"

    def timestamp_text(self, q: str, utc: bool) -> str:
        raise NotImplementedError

    def float_text(self, q: str) -> str:
        raise NotImplementedError

    def row_text(self, cols: list[tuple[str, str]]) -> str:
        parts = [f"COALESCE({self.normalize(c, t)}, '{NULL_MARKER}')" for c, t in cols]
        return f" || '{SEPARATOR}' || ".join(parts) if parts else "''"

    def segment_checksum_sql(self, table: str, key: str, cols: list[tuple[str, str]]) -> str:
        """count(*) and one hash over the rows of a key range; params: lo, hi."""
        k = self.quote(key)
        return (
            f"SELECT count(*), md5(string_agg(md5({self.row_text(cols)}), '' ORDER BY {k})) "
            f"FROM {self.table(table)} WHERE {k} >= %s AND {k} < %s"
        )

    def key_bounds_sql(self, table: str, key: str) -> str:
        k = self.quote(key)
        return f"SELECT min({k}), max({k}) FROM {self.table(table)}"

    def row_hashes_sql(self, table: str, key: str, cols: list[tuple[str, str]]) -> str:
        """(key, row hash) for each row of a key range; params: lo, hi."""
        k = self.quote(key)
        return (
            f"SELECT {k}, md5({self.row_text(cols)}) FROM {self.table(table)} "
            f"WHERE {k} >= %s AND {k} < %s ORDER BY {k}"
        )


class Postgres(Dialect):
    name = "postgresql"

    def default_schema(self) -> str:
        return "public"

    def timestamp_text(self, q: str, utc: bool) -> str:
        src = f"({q} AT TIME ZONE 'UTC')" if utc else q
        return f"to_char({src}, 'YYYY-MM-DD\"T\"HH24:MI:SS.US')"

    def float_text(self, q: str) -> str:
        return f"CAST(round(CAST({q} AS numeric), {FLOAT_DIGITS}) AS VARCHAR)"


class DuckDB(Dialect):
    name = "duckdb"

    def default_schema(self) -> str:
        return "main"

    def column_types_sql(self) -> str:
        return super().column_types_sql().replace("%s", "?")

    def timestamp_text(self, q: str, utc: bool) -> str:
        src = f"timezone('UTC', {q})" if utc else q
        return f"strftime({src}, '%Y-%m-%dT%H:%M:%S.%f')"

    def float_text(self, q: str) -> str:
        return f"printf('%.{FLOAT_DIGITS}f', CAST({q} AS DOUBLE))"

    def segment_checksum_sql(self, table: str, key: str, cols: list[tuple[str, str]]) -> str:
        return super().segment_checksum_sql(table, key, cols).replace("%s", "?")

    def row_hashes_sql(self, table: str, key: str, cols: list[tuple[str, str]]) -> str:
        return super().row_hashes_sql(table, key, cols).replace("%s", "?")
