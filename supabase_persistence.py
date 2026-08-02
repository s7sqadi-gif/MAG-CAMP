"""Persistent SQLite bridge backed by PostgreSQL/Supabase.

MAG CAMP 7.3 continues to use its proven SQLite data layer. On Render Free,
the SQLite database and uploaded files are mirrored to PostgreSQL so they can
be restored after a restart or redeploy. No application SQL or workflows are
changed.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import tempfile
import threading
from pathlib import Path
from typing import Optional

_LOCK = threading.RLock()
_SCHEMA_READY = False


def _database_url() -> str:
    return os.environ.get("DATABASE_URL", "").strip()


def enabled() -> bool:
    return bool(_database_url())


def _connect():
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - deployment dependency
        raise RuntimeError("psycopg is required when DATABASE_URL is configured") from exc
    return psycopg.connect(_database_url(), connect_timeout=20)


def _ensure_remote_schema(conn) -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS magcamp_sqlite_state (
                singleton_id SMALLINT PRIMARY KEY CHECK (singleton_id = 1),
                database_blob BYTEA NOT NULL,
                sha256 TEXT NOT NULL,
                size_bytes BIGINT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS magcamp_uploaded_files (
                relative_path TEXT PRIMARY KEY,
                content BYTEA NOT NULL,
                sha256 TEXT NOT NULL,
                size_bytes BIGINT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
    conn.commit()
    _SCHEMA_READY = True


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _consistent_sqlite_bytes(db_path: str) -> bytes:
    """Create a transactionally consistent copy using SQLite's backup API."""
    path = Path(db_path)
    if not path.exists():
        raise FileNotFoundError(db_path)
    fd, temp_name = tempfile.mkstemp(prefix="magcamp_snapshot_", suffix=".db")
    os.close(fd)
    try:
        source = sqlite3.connect(str(path), timeout=30)
        target = sqlite3.connect(temp_name)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        return Path(temp_name).read_bytes()
    finally:
        try:
            os.remove(temp_name)
        except OSError:
            pass


def restore_or_seed(runtime_db: str, bundled_db: str, upload_dir: str) -> str:
    """Restore the latest persistent copy, or seed PostgreSQL from bundled 7.3.

    Returns one of: disabled, restored, seeded.
    """
    runtime = Path(runtime_db)
    bundled = Path(bundled_db)
    uploads = Path(upload_dir)
    runtime.parent.mkdir(parents=True, exist_ok=True)
    uploads.mkdir(parents=True, exist_ok=True)

    if not enabled():
        if runtime.resolve() != bundled.resolve() and not runtime.exists():
            shutil.copy2(bundled, runtime)
        return "disabled"

    with _LOCK, _connect() as pg:
        _ensure_remote_schema(pg)
        with pg.cursor() as cur:
            cur.execute(
                "SELECT database_blob, sha256 FROM magcamp_sqlite_state WHERE singleton_id=1"
            )
            row = cur.fetchone()
        if row:
            blob = bytes(row[0])
            if _sha256(blob) != row[1]:
                raise RuntimeError("Persistent database checksum mismatch")
            temp = runtime.with_suffix(".restore.tmp")
            temp.write_bytes(blob)
            # Verify before replacing the active database.
            check = sqlite3.connect(str(temp))
            try:
                integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
            finally:
                check.close()
            if integrity != "ok":
                temp.unlink(missing_ok=True)
                raise RuntimeError(f"Persistent database failed integrity check: {integrity}")
            os.replace(temp, runtime)
            _restore_files(pg, uploads)
            print(f"[MAG CAMP] restored persistent SQLite ({len(blob)} bytes)", flush=True)
            return "restored"

        if not bundled.exists():
            raise FileNotFoundError(f"Bundled database not found: {bundled}")
        shutil.copy2(bundled, runtime)
        snapshot(runtime_db, upload_dir, force=True)
        print("[MAG CAMP] seeded PostgreSQL persistence from stable 7.3 database", flush=True)
        return "seeded"


def _restore_files(pg, upload_dir: Path) -> None:
    with pg.cursor() as cur:
        cur.execute("SELECT relative_path, content, sha256 FROM magcamp_uploaded_files")
        for relative_path, content, expected_sha in cur.fetchall():
            safe = Path(str(relative_path))
            if safe.is_absolute() or ".." in safe.parts:
                continue
            data = bytes(content)
            if _sha256(data) != expected_sha:
                continue
            target = upload_dir / safe
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists() or _sha256(target.read_bytes()) != expected_sha:
                target.write_bytes(data)


def snapshot(db_path: str, upload_dir: str, *, force: bool = False) -> bool:
    """Persist the SQLite database and new/changed uploads to PostgreSQL."""
    if not enabled():
        return False
    with _LOCK:
        db_data = _consistent_sqlite_bytes(db_path)
        db_sha = _sha256(db_data)
        with _connect() as pg:
            _ensure_remote_schema(pg)
            should_write = force
            if not force:
                with pg.cursor() as cur:
                    cur.execute(
                        "SELECT sha256 FROM magcamp_sqlite_state WHERE singleton_id=1"
                    )
                    row = cur.fetchone()
                should_write = not row or row[0] != db_sha
            if should_write:
                with pg.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO magcamp_sqlite_state
                            (singleton_id, database_blob, sha256, size_bytes, updated_at)
                        VALUES (1, %s, %s, %s, NOW())
                        ON CONFLICT (singleton_id) DO UPDATE SET
                            database_blob=EXCLUDED.database_blob,
                            sha256=EXCLUDED.sha256,
                            size_bytes=EXCLUDED.size_bytes,
                            updated_at=NOW()
                        """,
                        (db_data, db_sha, len(db_data)),
                    )
            _snapshot_files(pg, Path(upload_dir))
            pg.commit()
        if should_write:
            print(f"[MAG CAMP] persisted SQLite snapshot ({len(db_data)} bytes)", flush=True)
        return should_write


def _snapshot_files(pg, upload_dir: Path) -> None:
    if not upload_dir.exists():
        return
    with pg.cursor() as cur:
        for path in upload_dir.rglob("*"):
            if not path.is_file():
                continue
            data = path.read_bytes()
            sha = _sha256(data)
            relative = path.relative_to(upload_dir).as_posix()
            cur.execute(
                "SELECT sha256 FROM magcamp_uploaded_files WHERE relative_path=%s",
                (relative,),
            )
            row = cur.fetchone()
            if row and row[0] == sha:
                continue
            cur.execute(
                """
                INSERT INTO magcamp_uploaded_files
                    (relative_path, content, sha256, size_bytes, updated_at)
                VALUES (%s, %s, %s, %s, NOW())
                ON CONFLICT (relative_path) DO UPDATE SET
                    content=EXCLUDED.content,
                    sha256=EXCLUDED.sha256,
                    size_bytes=EXCLUDED.size_bytes,
                    updated_at=NOW()
                """,
                (relative, data, sha, len(data)),
            )


def status() -> dict:
    if not enabled():
        return {"enabled": False}
    try:
        with _connect() as pg:
            _ensure_remote_schema(pg)
            with pg.cursor() as cur:
                cur.execute(
                    "SELECT size_bytes, updated_at FROM magcamp_sqlite_state WHERE singleton_id=1"
                )
                row = cur.fetchone()
                cur.execute("SELECT COUNT(*), COALESCE(SUM(size_bytes),0) FROM magcamp_uploaded_files")
                files = cur.fetchone()
        return {
            "enabled": True,
            "database_size": row[0] if row else 0,
            "updated_at": row[1].isoformat() if row and row[1] else None,
            "file_count": files[0],
            "file_bytes": files[1],
        }
    except Exception as exc:
        return {"enabled": True, "error": str(exc)}
