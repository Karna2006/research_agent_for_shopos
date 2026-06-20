"""Persistent SQLite cache for discovered Instagram handles.

TTL: 30 days. Stale entries are re-discovered on next audit.
Keyed by normalised domain (strips www, scheme).

Usage:
    from scrapers.ig_handle_cache import get_cached_handle, store_handle

    cached = get_cached_handle("rarerabbit.in")
    if cached:
        handle, confidence = cached
    else:
        handle, confidence = await discover_handle(...)
        store_handle("rarerabbit.in", handle, confidence, source="website")
"""
from __future__ import annotations

from datetime import datetime, timedelta
from urllib.parse import urlparse

from sqlmodel import Session, select

from db.database import engine
from db.models import IgHandleCache

_TTL_DAYS = 30
# Don't cache low-confidence guesses — re-discover next time
_MIN_CONFIDENCE_TO_CACHE = {"confirmed", "high", "medium"}


def _normalise_domain(url_or_domain: str) -> str:
    """'https://www.rarerabbit.in/path' → 'rarerabbit.in'"""
    s = url_or_domain.strip().lower()
    if "://" in s:
        s = urlparse(s).netloc
    return s.replace("www.", "").split(":")[0]


def get_cached_handle(url_or_domain: str) -> tuple[str | None, str] | None:
    """Return (handle, confidence) if a fresh cache entry exists, else None."""
    domain = _normalise_domain(url_or_domain)
    if not domain:
        return None
    try:
        with Session(engine) as session:
            row = session.exec(
                select(IgHandleCache).where(IgHandleCache.domain == domain)
            ).first()
            if row is None:
                return None
            age = datetime.utcnow() - row.discovered_at
            if age > timedelta(days=_TTL_DAYS):
                # Stale — delete so next audit re-discovers
                session.delete(row)
                session.commit()
                return None
            return (row.handle, row.confidence)
    except Exception:
        return None


def store_handle(
    url_or_domain: str,
    handle: str | None,
    confidence: str,
    source: str | None = None,
) -> None:
    """Upsert handle into cache. Skips low-confidence guesses."""
    if confidence not in _MIN_CONFIDENCE_TO_CACHE and handle is not None:
        return  # don't cache guesses or not_found with a handle
    domain = _normalise_domain(url_or_domain)
    if not domain:
        return
    try:
        with Session(engine) as session:
            row = session.exec(
                select(IgHandleCache).where(IgHandleCache.domain == domain)
            ).first()
            if row:
                row.handle = handle
                row.confidence = confidence
                row.discovered_at = datetime.utcnow()
                row.source = source
            else:
                row = IgHandleCache(
                    domain=domain,
                    handle=handle,
                    confidence=confidence,
                    source=source,
                )
                session.add(row)
            session.commit()
    except Exception:
        pass  # cache is best-effort — never block the audit
