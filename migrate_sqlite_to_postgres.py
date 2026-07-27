#!/usr/bin/env python3
"""Safe one-time migration of MAG CAMP SQLite data to PostgreSQL/Supabase.

MAG CAMP 8.1 adds preflight checks, transactional migration, schema/data
verification, sequence repair, and a machine-readable migration report.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import psycopg
    from psycopg import sql
except ImportError:
    raise SystemExit("Install requirements first: pip install -r requirements.txt")

SQLITE_PATH = Path(os.environ.get("SQLITE_PATH", "data/mhoms.db"))
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
REPORT_PATH = Path(os.environ.get("MIGRATION_REPORT", "migration_report_8_1.json"))
ALLOW_NONEMPTY = os.environ.get("ALLOW_NONEMPTY_TARGET", "0") == "1"

if not DATABASE_URL:
    raise SystemExit("DATABASE_URL is required")
if not SQLITE_PATH.exists():
    raise SystemExit(f"SQLite file not found: {SQLITE_PATH}")
if SQLITE_PATH.stat().st_size == 0:
    raise SystemExit(f"SQLite file is empty: {SQLITE_PATH}")

TYPE_MAP = {
    "INTEGER": "BIGINT",
    "REAL": "DOUBLE PRECISION",
    "TEXT": "TEXT",
    "BLOB": "BYTEA",
    "NUMERIC": "NUMERIC",
}


def pg_type(declared: str | None) -> str:
    value = (declared or "TEXT").upper()
    for key, mapped in TYPE_MAP.items():
        if key in value:
            return mapped
    return "TEXT"


def sqlite_tables(conn: sqlite3.Connection) -> list[str]:
    return [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]


def source_counts(conn: sqlite3.Connection, tables: list[str]) -> dict[str, int]:
    return {
        table: conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        for table in tables
    }


def target_public_tables(cur) -> set[str]:
    cur.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='public' AND table_type='BASE TABLE'"
    )
    return {row[0] for row in cur.fetchall()}


def create_table(cur, src: sqlite3.Connection, table: str) -> list[str]:
    cols = src.execute(f'PRAGMA table_info("{table}")').fetchall()
    definitions = []
    pk_cols: list[str] = []
    column_names: list[str] = []

    for col in cols:
        name, declared, notnull, default, pk = col[1], col[2], col[3], col[4], col[5]
        column_names.append(name)
        if pk and name == "id" and "INT" in (declared or "").upper():
            definitions.append(
                sql.SQL("{} BIGSERIAL PRIMARY KEY").format(sql.Identifier(name))
            )
            pk_cols.append(name)
            continue

        part = sql.SQL("{} {}").format(sql.Identifier(name), sql.SQL(pg_type(declared)))
        if notnull:
            part += sql.SQL(" NOT NULL")
        if default is not None:
            # SQLite returns the original SQL literal; preserve safe common literals.
            normalized = str(default).strip()
            if normalized.upper() in {"CURRENT_TIMESTAMP", "CURRENT_DATE", "NULL"}:
                part += sql.SQL(" DEFAULT ") + sql.SQL(normalized.upper())
            elif normalized.startswith(("'", '"')) or normalized.lstrip("-").replace(".", "", 1).isdigit():
                part += sql.SQL(" DEFAULT ") + sql.SQL(normalized)
        definitions.append(part)
        if pk:
            pk_cols.append(name)

    if pk_cols and not (len(pk_cols) == 1 and pk_cols[0] == "id"):
        definitions.append(
            sql.SQL("PRIMARY KEY ({})").format(
                sql.SQL(",").join(map(sql.Identifier, pk_cols))
            )
        )

    cur.execute(
        sql.SQL("CREATE TABLE IF NOT EXISTS {} ({})").format(
            sql.Identifier(table), sql.SQL(",").join(definitions)
        )
    )
    return column_names


def copy_rows(cur, src: sqlite3.Connection, table: str, names: list[str]) -> int:
    rows = src.execute(f'SELECT * FROM "{table}"').fetchall()
    if not rows:
        return 0
    stmt = sql.SQL("INSERT INTO {} ({}) VALUES ({}) ON CONFLICT DO NOTHING").format(
        sql.Identifier(table),
        sql.SQL(",").join(map(sql.Identifier, names)),
        sql.SQL(",").join(sql.Placeholder() * len(names)),
    )
    cur.executemany(stmt, [tuple(row[name] for name in names) for row in rows])
    return len(rows)


def repair_sequence(cur, table: str, names: list[str]) -> None:
    if "id" not in names:
        return
    cur.execute("SELECT pg_get_serial_sequence(%s, 'id')", (table,))
    result = cur.fetchone()
    sequence = result[0] if result else None
    if not sequence:
        return
    cur.execute(
        sql.SQL("SELECT COALESCE(MAX(id), 0) FROM {}").format(sql.Identifier(table))
    )
    max_id = cur.fetchone()[0]
    if max_id:
        cur.execute("SELECT setval(%s, %s, true)", (sequence, max_id))
    else:
        cur.execute("SELECT setval(%s, 1, false)", (sequence,))


def main() -> int:
    started = datetime.now(timezone.utc).isoformat()
    src = sqlite3.connect(SQLITE_PATH)
    src.row_factory = sqlite3.Row
    integrity = src.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise SystemExit(f"SQLite integrity check failed: {integrity}")

    tables = sqlite_tables(src)
    counts_before = source_counts(src, tables)
    report = {
        "release": "MAG CAMP 8.1",
        "started_at": started,
        "sqlite_path": str(SQLITE_PATH),
        "sqlite_integrity": integrity,
        "tables": {},
        "status": "running",
    }

    try:
        with psycopg.connect(DATABASE_URL, autocommit=False, connect_timeout=20) as dst:
            with dst.cursor() as cur:
                existing = target_public_tables(cur)
                nonempty = {}
                for table in existing.intersection(tables):
                    cur.execute(sql.SQL("SELECT COUNT(*) FROM {}").format(sql.Identifier(table)))
                    count = cur.fetchone()[0]
                    if count:
                        nonempty[table] = count
                if nonempty and not ALLOW_NONEMPTY:
                    raise RuntimeError(
                        "Target contains data. To intentionally merge using ON CONFLICT, "
                        "set ALLOW_NONEMPTY_TARGET=1. Non-empty tables: " + json.dumps(nonempty)
                    )

                for table in tables:
                    names = create_table(cur, src, table)
                    attempted = copy_rows(cur, src, table, names)
                    repair_sequence(cur, table, names)
                    cur.execute(sql.SQL("SELECT COUNT(*) FROM {}").format(sql.Identifier(table)))
                    target_count = cur.fetchone()[0]
                    expected = counts_before[table]
                    report["tables"][table] = {
                        "source_rows": expected,
                        "rows_attempted": attempted,
                        "target_rows": target_count,
                        "verified": target_count >= expected if ALLOW_NONEMPTY else target_count == expected,
                    }
                    if not report["tables"][table]["verified"]:
                        raise RuntimeError(
                            f"Row-count verification failed for {table}: "
                            f"source={expected}, target={target_count}"
                        )
                    print(f"{table}: source={expected}, target={target_count}")

            dst.commit()
        report["status"] = "success"
        print("Migration completed and verified successfully.")
        return 0
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = str(exc)
        print(f"Migration failed: {exc}", file=sys.stderr)
        return 1
    finally:
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        src.close()
        print(f"Migration report: {REPORT_PATH}")


if __name__ == "__main__":
    raise SystemExit(main())
