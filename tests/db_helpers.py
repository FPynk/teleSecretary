from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from sqlite3 import Connection
from typing import Iterator

from tele_secretary.persistence.connection import connect


@contextmanager
def open_test_database(db_path: Path) -> Iterator[Connection]:
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()
