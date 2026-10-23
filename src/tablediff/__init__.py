"""Find the rows that differ between two tables, within or across databases."""

from .connect import Table, duckdb_table, open_table
from .differ import DiffResult, diff_tables, naive_diff

__all__ = ["DiffResult", "Table", "diff_tables", "duckdb_table", "naive_diff", "open_table"]
__version__ = "0.1.0"
