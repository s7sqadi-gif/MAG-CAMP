#!/usr/bin/env python3
"""Post-migration PostgreSQL verification for MAG CAMP 8.1."""
from __future__ import annotations
import os
import sys
try:
    import psycopg
except ImportError:
    raise SystemExit("Install requirements first: pip install -r requirements.txt")

url = os.environ.get("DATABASE_URL", "").strip()
if not url:
    raise SystemExit("DATABASE_URL is required")
required = {"users", "rooms", "workers", "maintenance_tickets", "audit_logs"}
with psycopg.connect(url, connect_timeout=20) as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
        tables = {r[0] for r in cur.fetchall()}
        missing = required - tables
        if missing:
            print("Missing required tables: " + ", ".join(sorted(missing)), file=sys.stderr)
            raise SystemExit(1)
        for table in sorted(required):
            cur.execute(f'SELECT COUNT(*) FROM "{table}"')
            print(f"{table}: {cur.fetchone()[0]} rows")
        cur.execute("SELECT 1")
print("PostgreSQL verification passed.")
