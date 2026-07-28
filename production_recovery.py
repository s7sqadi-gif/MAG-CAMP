"""MAG CAMP 9.0 production recovery.

Restores the verified bundled 7.3 SQLite dataset into an empty or partial
PostgreSQL database. It is deliberately conservative: it runs only when the
critical tables contain far fewer rows than the bundled source.
"""
from __future__ import annotations
import os, sqlite3
from contextlib import closing
from database import DATABASE_URL, IS_POSTGRES

TYPE_MAP = {"INTEGER":"BIGINT","REAL":"DOUBLE PRECISION","BLOB":"BYTEA","TEXT":"TEXT","NUMERIC":"NUMERIC"}

def pg_type(declared: str | None) -> str:
    d=(declared or 'TEXT').upper()
    return next((v for k,v in TYPE_MAP.items() if k in d),'TEXT')

def _tables(src):
    return [r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]

def recover_if_needed(sqlite_path: str) -> None:
    if not IS_POSTGRES or not DATABASE_URL or not os.path.isfile(sqlite_path):
        return
    import psycopg
    from psycopg import sql
    src=sqlite3.connect(sqlite_path); src.row_factory=sqlite3.Row
    try:
        expected={t:src.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in _tables(src)}
        with psycopg.connect(DATABASE_URL, autocommit=False, connect_timeout=20) as dst:
            with dst.cursor() as cur:
                cur.execute("SELECT to_regclass('public.workers'), to_regclass('public.rooms')")
                regs=cur.fetchone()
                actual_workers=actual_rooms=0
                if regs[0]: cur.execute('SELECT COUNT(*) FROM workers'); actual_workers=cur.fetchone()[0]
                if regs[1]: cur.execute('SELECT COUNT(*) FROM rooms'); actual_rooms=cur.fetchone()[0]
                if actual_workers >= max(100, expected.get('workers',0)//2) and actual_rooms >= max(100, expected.get('rooms',0)//2):
                    print(f'[MAG CAMP 9.0] data healthy: workers={actual_workers}, rooms={actual_rooms}',flush=True); return
                print(f'[MAG CAMP 9.0] critical data recovery starting: workers={actual_workers}/{expected.get("workers")}, rooms={actual_rooms}/{expected.get("rooms")}',flush=True)
                tables=_tables(src)
                # Create tables/add missing columns first.
                for table in tables:
                    cols=src.execute(f'PRAGMA table_info("{table}")').fetchall()
                    defs=[]
                    for col in cols:
                        name,decl,notnull,default,pk=col[1],col[2],col[3],col[4],col[5]
                        if name=='id' and pk: part=sql.SQL('{} BIGINT PRIMARY KEY').format(sql.Identifier(name))
                        else:
                            part=sql.SQL('{} {}').format(sql.Identifier(name),sql.SQL(pg_type(decl)))
                            if pk: part += sql.SQL(' PRIMARY KEY')
                        defs.append(part)
                    cur.execute(sql.SQL('CREATE TABLE IF NOT EXISTS {} ({})').format(sql.Identifier(table),sql.SQL(',').join(defs)))
                    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=%s",(table,))
                    existing={r[0] for r in cur.fetchall()}
                    for col in cols:
                        if col[1] not in existing:
                            cur.execute(sql.SQL('ALTER TABLE {} ADD COLUMN {} {}').format(sql.Identifier(table),sql.Identifier(col[1]),sql.SQL(pg_type(col[2]))))
                # Exact stable restore for all bundled tables. Existing partial seeded data is replaced.
                for table in reversed(tables):
                    cur.execute(sql.SQL('DELETE FROM {}').format(sql.Identifier(table)))
                for table in tables:
                    cols=src.execute(f'PRAGMA table_info("{table}")').fetchall(); names=[c[1] for c in cols]
                    rows=src.execute(f'SELECT * FROM "{table}"').fetchall()
                    if rows:
                        stmt=sql.SQL('INSERT INTO {} ({}) VALUES ({})').format(sql.Identifier(table),sql.SQL(',').join(map(sql.Identifier,names)),sql.SQL(',').join(sql.Placeholder()*len(names)))
                        cur.executemany(stmt,[tuple(r[n] for n in names) for r in rows])
                    if 'id' in names:
                        cur.execute("SELECT pg_get_serial_sequence(%s,'id')",(table,)); seq=(cur.fetchone() or [None])[0]
                        if seq:
                            cur.execute(sql.SQL('SELECT COALESCE(MAX(id),0) FROM {}').format(sql.Identifier(table))); mx=cur.fetchone()[0]
                            cur.execute('SELECT setval(%s,%s,%s)',(seq,max(mx,1),bool(mx)))
                cur.execute('SELECT COUNT(*) FROM workers'); w=cur.fetchone()[0]
                cur.execute('SELECT COUNT(*) FROM rooms'); r=cur.fetchone()[0]
                if w != expected.get('workers') or r != expected.get('rooms'):
                    raise RuntimeError(f'verification failed workers={w}, rooms={r}')
            dst.commit()
        print(f'[MAG CAMP 9.0] stable recovery verified: workers={w}, rooms={r}, users={expected.get("users",0)}',flush=True)
    finally:
        src.close()
