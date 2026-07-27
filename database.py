"""Database compatibility layer for MAG CAMP 8.0.

Uses SQLite when DATABASE_URL is absent, and PostgreSQL (Supabase/Render)
when DATABASE_URL starts with postgresql:// or postgres://.
"""
from __future__ import annotations

import os
import re
import sqlite3
from collections.abc import Mapping
from typing import Any, Iterable

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
IS_POSTGRES = DATABASE_URL.startswith(("postgresql://", "postgres://"))


class HybridRow(Mapping):
    """Row supporting both row['column'] and row[0], like sqlite3.Row."""
    def __init__(self, columns: list[str], values: tuple[Any, ...]):
        self._columns = columns
        self._values = values
        self._map = dict(zip(columns, values))

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return self._map[key]

    def __iter__(self):
        return iter(self._columns)

    def __len__(self):
        return len(self._columns)

    def keys(self):
        return self._map.keys()


class PostgresCursor:
    def __init__(self, cursor):
        self._cursor = cursor
        self.lastrowid = None

    def _row(self, raw):
        if raw is None:
            return None
        columns = [d.name if hasattr(d, "name") else d[0] for d in self._cursor.description]
        return HybridRow(columns, tuple(raw))

    def fetchone(self):
        return self._row(self._cursor.fetchone())

    def fetchall(self):
        return [self._row(r) for r in self._cursor.fetchall()]

    def __iter__(self):
        while True:
            row = self.fetchone()
            if row is None:
                break
            yield row

    @property
    def rowcount(self):
        return self._cursor.rowcount


_INSERT_RE = re.compile(r"^\s*INSERT\s+(?:OR\s+IGNORE\s+)?INTO\s+([\w\"]+)", re.I)


def _translate_sql(sql: str) -> str:
    sql = sql.strip()
    sql = re.sub(r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b", "BIGSERIAL PRIMARY KEY", sql, flags=re.I)
    sql = re.sub(r"\bAUTOINCREMENT\b", "", sql, flags=re.I)
    sql = re.sub(r"INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", sql, flags=re.I)
    sql = sql.replace("datetime('now')", "CURRENT_TIMESTAMP")
    sql = sql.replace("date('now')", "CURRENT_DATE")
    sql = re.sub(r"datetime\('now','-30 minutes'\)", "(CURRENT_TIMESTAMP - INTERVAL '30 minutes')", sql, flags=re.I)
    # SQLite positional placeholders to psycopg placeholders.
    sql = sql.replace("?", "%s")
    # For former INSERT OR IGNORE statements, conflict-safe behavior.
    if re.search(r"^\s*INSERT\s+INTO", sql, re.I) and " OR IGNORE " in (" " + sql.upper() + " "):
        sql += " ON CONFLICT DO NOTHING"
    return sql


def _split_script(script: str) -> list[str]:
    # Current schema scripts contain no semicolons inside string literals.
    return [part.strip() for part in script.split(";") if part.strip()]


class PostgresConnection:
    def __init__(self, dsn: str):
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError("PostgreSQL mode requires psycopg[binary].") from exc
        self._conn = psycopg.connect(dsn, autocommit=False, connect_timeout=15)

    def execute(self, sql: str, params: Iterable[Any] | None = None):
        original = sql
        translated = _translate_sql(sql)
        was_ignore = bool(re.match(r"^\s*INSERT\s+OR\s+IGNORE", original, re.I))
        if was_ignore and "ON CONFLICT" not in translated.upper():
            translated += " ON CONFLICT DO NOTHING"

        insert_match = _INSERT_RE.match(original)
        needs_id = bool(insert_match) and "RETURNING" not in translated.upper()
        if needs_id:
            translated += " RETURNING id"

        cur = self._conn.cursor()
        try:
            cur.execute(translated, tuple(params or ()))
            wrapped = PostgresCursor(cur)
            if needs_id:
                raw = cur.fetchone()
                wrapped.lastrowid = raw[0] if raw else None
            return wrapped
        except Exception:
            cur.close()
            raise

    def executescript(self, script: str):
        result = None
        for statement in _split_script(script):
            result = self.execute(statement)
        return result

    def commit(self):
        return self._conn.commit()

    def rollback(self):
        return self._conn.rollback()

    def close(self):
        return self._conn.close()



def connect(sqlite_path: str):
    if IS_POSTGRES:
        return PostgresConnection(DATABASE_URL)
    c = sqlite3.connect(sqlite_path, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA busy_timeout=30000")
    try:
        c.execute("PRAGMA journal_mode=WAL")
    except sqlite3.DatabaseError:
        pass
    return c


def column_names(connection, table: str) -> set[str]:
    if IS_POSTGRES:
        rows = connection.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=?",
            (table,),
        ).fetchall()
        return {r[0] for r in rows}
    return {r[1] for r in connection.execute(f"PRAGMA table_info({table})")}
