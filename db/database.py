"""Database engine — SQLite for dev, PostgreSQL (Neon) for prod.

Auto-detects via DATABASE_URL env var:
  unset / blank  →  SQLite  (./shopos.db)
  postgres://…   →  PostgreSQL via psycopg2 with pool_size=5
"""
from __future__ import annotations

import os

from sqlmodel import SQLModel, Session, create_engine

_raw_url = os.getenv("DATABASE_URL", "").strip()

if _raw_url:
    # Normalise Neon / Heroku shorthand  postgres:// → postgresql://
    if _raw_url.startswith("postgres://"):
        _raw_url = "postgresql" + _raw_url[len("postgres"):]

    # Force psycopg2 driver for sync SQLModel (asyncpg only works with async engine)
    if "postgresql+asyncpg" in _raw_url:
        _raw_url = _raw_url.replace("postgresql+asyncpg", "postgresql+psycopg2")
    elif _raw_url.startswith("postgresql://") or _raw_url.startswith("postgresql+psycopg2"):
        pass  # already correct
    # else keep as-is

    engine = create_engine(
        _raw_url,
        echo=False,
        pool_size=5,        # Neon free tier: max 5 concurrent connections
        max_overflow=0,     # never exceed pool_size on free tier
        pool_pre_ping=True, # detect stale connections (serverless idle)
        pool_recycle=300,   # recycle connections every 5 min (Neon idle timeout)
    )
    _DB_BACKEND = "postgresql"
else:
    engine = create_engine(
        "sqlite:////tmp/shopos.db",
        echo=False,
        connect_args={"check_same_thread": False},
    )
    _DB_BACKEND = "sqlite"


def _migrate_columns() -> None:
    """Add columns introduced in later schema versions without dropping data."""
    from sqlalchemy import inspect, text

    with engine.begin() as conn:
        existing = {c["name"] for c in inspect(engine).get_columns("auditrun")}
        for col in ("share_token", "one_thing", "changes_summary"):
            if col not in existing:
                if _DB_BACKEND == "sqlite":
                    conn.execute(text(f"ALTER TABLE auditrun ADD COLUMN {col} VARCHAR"))
                else:
                    conn.execute(
                        text(f"ALTER TABLE auditrun ADD COLUMN IF NOT EXISTS {col} VARCHAR")
                    )
        if "monitoring" not in existing:
            if _DB_BACKEND == "sqlite":
                conn.execute(text("ALTER TABLE auditrun ADD COLUMN monitoring BOOLEAN DEFAULT 0"))
            else:
                conn.execute(
                    text("ALTER TABLE auditrun ADD COLUMN IF NOT EXISTS monitoring BOOLEAN DEFAULT FALSE")
                )

        # CompareRun migrations
        try:
            existing_cr = {c["name"] for c in inspect(engine).get_columns("comparerun")}
            for col in ("compare_share_token", "swot_json", "strategy_json_a", "strategy_json_b"):
                if col not in existing_cr:
                    if _DB_BACKEND == "sqlite":
                        conn.execute(text(f"ALTER TABLE comparerun ADD COLUMN {col} VARCHAR"))
                    else:
                        conn.execute(
                            text(f"ALTER TABLE comparerun ADD COLUMN IF NOT EXISTS {col} VARCHAR")
                        )
        except Exception:
            pass  # comparerun may not exist on first run

        # Backfill NULL share_tokens so every audit has a shareable link
        try:
            if _DB_BACKEND == "sqlite":
                conn.execute(text(
                    "UPDATE auditrun SET share_token = lower(hex(randomblob(6))) "
                    "WHERE share_token IS NULL OR share_token = ''"
                ))
            else:
                conn.execute(text(
                    "UPDATE auditrun SET share_token = encode(gen_random_bytes(6), 'hex') "
                    "WHERE share_token IS NULL OR share_token = ''"
                ))
        except Exception:
            pass  # table may not exist yet; create_all handles it


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    try:
        _migrate_columns()
    except Exception:
        pass  # table may not exist yet on first run — create_all handles it


def get_session():
    with Session(engine) as session:
        yield session


def db_backend() -> str:
    """Return 'sqlite' or 'postgresql' — used for logging / health checks."""
    return _DB_BACKEND
