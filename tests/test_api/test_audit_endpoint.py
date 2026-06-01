"""Tests for FastAPI audit endpoints — uses in-memory SQLite, mocks background work."""
from __future__ import annotations

import os
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

# ── Patch env before importing app ────────────────────────────────────────────
os.environ.setdefault("GROQ_API_KEY", "test-key")
os.environ.setdefault("DATABASE_URL", "")  # force SQLite

# Use in-memory SQLite for all tests in this module
_TEST_ENGINE = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)


@pytest.fixture(autouse=True)
def _setup_test_db():
    """Recreate tables for each test on the in-memory engine."""
    SQLModel.metadata.create_all(_TEST_ENGINE)
    yield
    SQLModel.metadata.drop_all(_TEST_ENGINE)


@pytest.fixture
def app():
    """Return the FastAPI app with DB patched to in-memory SQLite."""
    with (
        patch("db.database.engine", _TEST_ENGINE),
        patch("main.engine", _TEST_ENGINE),
        patch("main._cache") as mock_cache,
        patch("main._try_mastra_audit", new_callable=AsyncMock, return_value=False),
    ):
        mock_cache.get = AsyncMock(return_value=None)
        mock_cache.set = AsyncMock()
        mock_cache.audit_key = MagicMock(return_value="test_key")

        from main import app as _app
        yield _app


@pytest_asyncio.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


# ── Health check ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_health_check(client):
    """GET /health → 200, {"status": "ok"}."""
    resp = await client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"


# ── Home UI ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ui_loads(client):
    """GET / → 200, HTML page with Brand Audit tab."""
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "Brand Audit" in resp.text


# ── Audit start ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_audit_returns_audit_id(client):
    """POST /audit with valid URL → 200, response has audit_id."""
    with patch("main._run_audit_bg", new_callable=AsyncMock):
        resp = await client.post("/audit", json={"url": "https://rarerabbit.in"})

    assert resp.status_code == 200
    data = resp.json()
    assert "audit_id" in data
    assert isinstance(data["audit_id"], int)
    assert "stream_url" in data
    assert "report_url" in data


@pytest.mark.asyncio
async def test_audit_bad_url_returns_422(client):
    """POST /audit with non-URL string → 422 Unprocessable Entity."""
    resp = await client.post("/audit", json={"url": "not-a-url"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_audit_missing_url_returns_422(client):
    """POST /audit with no body → 422."""
    resp = await client.post("/audit", json={})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_audit_status_queued_on_fresh_request(client):
    """Fresh POST /audit → status is 'queued' (not cached)."""
    with patch("main._run_audit_bg", new_callable=AsyncMock):
        resp = await client.post("/audit", json={"url": "https://rarerabbit.in"})

    assert resp.json()["status"] == "queued"
    assert resp.json()["from_cache"] is False


@pytest.mark.asyncio
async def test_audit_cached_response(client, app):
    """When cache returns data, status is 'cached' and from_cache is True."""
    cached = {"url": "https://rarerabbit.in", "results": {}}

    with patch("main._cache") as mock_cache:
        mock_cache.get = AsyncMock(return_value=cached)
        mock_cache.audit_key = MagicMock(return_value="key")

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            with patch("main.generate_audit_report", return_value="<html></html>"):
                resp = await ac.post("/audit", json={"url": "https://rarerabbit.in"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "cached"
    assert data["from_cache"] is True


# ── Stream ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_audit_stream_connects(app):
    """GET /audit/stream/{id} → 200 with SSE content-type.

    Places a completed audit (sentinel current_agent='__cached__') directly in
    the DB so _sse_gen exits immediately on the cache-hit fast path.
    """
    from sqlmodel import Session as _S
    from db.models import AuditRun

    with (
        patch("db.database.engine", _TEST_ENGINE),
        patch("main.engine", _TEST_ENGINE),
        patch("main._cache") as mock_cache,
    ):
        mock_cache.get = AsyncMock(return_value=None)
        mock_cache.set = AsyncMock()
        mock_cache.audit_key = MagicMock(return_value="test_key")

        # Insert a complete audit row that triggers the cache-hit fast path
        with _S(_TEST_ENGINE) as s:
            audit = AuditRun(
                url="https://rarerabbit.in",
                status="complete",
                progress_pct=100,
                current_agent="__cached__",
                report_html="<html>report</html>",
            )
            s.add(audit)
            s.commit()
            s.refresh(audit)
            audit_id = audit.id

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            async with ac.stream("GET", f"/audit/stream/{audit_id}") as stream_resp:
                assert stream_resp.status_code == 200
                assert "text/event-stream" in stream_resp.headers.get("content-type", "")
                # Read first event to confirm the generator yields and exits
                first_chunk = b""
                async for chunk in stream_resp.aiter_bytes():
                    first_chunk = chunk
                    break
                assert b"cache_hit" in first_chunk or b"complete" in first_chunk


@pytest.mark.asyncio
async def test_audit_stream_404_for_unknown_id(client):
    """GET /audit/stream/99999 → 404."""
    resp = await client.get("/audit/stream/99999")
    assert resp.status_code == 404


# ── Report ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_report_404_for_unknown_audit(client):
    """GET /report/99999 → 404."""
    resp = await client.get("/report/99999")
    assert resp.status_code == 404


# ── Demo ───────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_demo_loads_with_brand_basics(client):
    """GET /demo → 200, HTML contains 'Brand Basics'."""
    with patch("main.generate_audit_report", return_value="<html>Brand Basics Section</html>"):
        resp = await client.get("/demo")
    assert resp.status_code == 200
    assert "Brand Basics" in resp.text


# ── Status endpoint ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_status_returns_200(client):
    """GET /status → 200."""
    resp = await client.get("/status")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_status_has_required_keys(client):
    """GET /status response contains all required service keys."""
    resp = await client.get("/status")
    data = resp.json()
    required = {"api", "database", "cache", "groq", "playwright", "mastra", "tribe_v2"}
    assert required.issubset(set(data.keys()))


@pytest.mark.asyncio
async def test_status_api_is_ok(client):
    """api field is always 'ok' when the server is running."""
    resp = await client.get("/status")
    assert resp.json()["api"] == "ok"


@pytest.mark.asyncio
async def test_status_database_field(client):
    """database field is 'sqlite' or 'postgresql'."""
    resp = await client.get("/status")
    assert resp.json()["database"] in ("sqlite", "postgresql")


@pytest.mark.asyncio
async def test_status_cache_field(client):
    """cache field is 'memory' or 'redis'."""
    resp = await client.get("/status")
    assert resp.json()["cache"] in ("memory", "redis")
