#!/usr/bin/env python3
"""One-time migration of MAG CAMP SQLite data to PostgreSQL/Supabase."""
import os, re, sqlite3, sys
from pathlib import Path

try:
    import psycopg
    from psycopg import sql
except ImportError:
    raise SystemExit("Install requirements first: pip install -r requirements.txt")

SQLITE_PATH = Path(os.environ.get("SQLITE_PATH", "data/mhoms.db"))
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
if not DATABASE_URL:
    raise SystemExit("DATABASE_URL is required")
if not SQLITE_PATH.exists():
    raise SystemExit(f"SQLite file not found: {SQLITE_PATH}")

TYPE_MAP = {"INTEGER":"BIGINT", "REAL":"DOUBLE PRECISION", "TEXT":"TEXT", "BLOB":"BYTEA", "NUMERIC":"NUMERIC"}

def pg_type(declared):
    d=(declared or "TEXT").upper()
    for key,val in TYPE_MAP.items():
        if key in d:return val
    return "TEXT"

src=sqlite3.connect(SQLITE_PATH)
src.row_factory=sqlite3.Row
tables=[r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]

with psycopg.connect(DATABASE_URL, autocommit=False) as dst:
    with dst.cursor() as cur:
        for table in tables:
            cols=src.execute(f'PRAGMA table_info("{table}")').fetchall()
            defs=[]; pk_cols=[]
            for c in cols:
                name,decl,notnull,default,pk=c[1],c[2],c[3],c[4],c[5]
                if pk and name=='id' and 'INT' in (decl or '').upper():
                    defs.append(sql.SQL('{} BIGSERIAL PRIMARY KEY').format(sql.Identifier(name)))
                    pk_cols.append(name); continue
                part=sql.SQL('{} {}').format(sql.Identifier(name),sql.SQL(pg_type(decl)))
                if notnull:part+=sql.SQL(' NOT NULL')
                if default is not None:part+=sql.SQL(' DEFAULT ')+sql.SQL(str(default))
                defs.append(part)
                if pk:pk_cols.append(name)
            if pk_cols and not (len(pk_cols)==1 and pk_cols[0]=='id'):
                defs.append(sql.SQL('PRIMARY KEY ({})').format(sql.SQL(',').join(map(sql.Identifier,pk_cols))))
            cur.execute(sql.SQL('CREATE TABLE IF NOT EXISTS {} ({})').format(sql.Identifier(table),sql.SQL(',').join(defs)))
            rows=src.execute(f'SELECT * FROM "{table}"').fetchall()
            if rows:
                names=[d[0] for d in src.execute(f'SELECT * FROM "{table}" LIMIT 0').description]
                stmt=sql.SQL('INSERT INTO {} ({}) VALUES ({}) ON CONFLICT DO NOTHING').format(
                    sql.Identifier(table),sql.SQL(',').join(map(sql.Identifier,names)),sql.SQL(',').join(sql.Placeholder()*len(names)))
                cur.executemany(stmt,[tuple(r[n] for n in names) for r in rows])
            if any(c[1]=='id' for c in cols):
                cur.execute(sql.SQL("SELECT setval(pg_get_serial_sequence(%s,'id'), COALESCE(MAX(id),1), MAX(id) IS NOT NULL) FROM {}").format(sql.Identifier(table)),(table,))
            print(f"{table}: {len(rows)} rows")
        dst.commit()
print("Migration completed successfully.")
