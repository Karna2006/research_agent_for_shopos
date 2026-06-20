"""Tests for FastAPI virality endpoints."""
from __future__ import annotations

import os
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlmodel import SQLModel, create_engine
from sqlmodel.pool import StaticPool

os.environ.setdefault("GROQ_API_KEY", "test-key")
os.environ.setdefault("GEMINI_API_KEY", "test-key")
os.environ.setdefault("DATABASE_URL", "")

_TEST_ENGINE = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)

_VIRALITY_RESULT = {
    "agent": "virality",
    "url": None,
    "product_name": "Classic Shirt",
    "score": 72,
    "grade": "A (Strong Potential)",
    "analysis": {
        "overall_virality_score": 72,
        "grade": "A (Strong Potential)",
        "dimensions": {},
        "killer_hook": "This shirt is elite.",
        "viral_content_angles": ["Angle 1"],
        "best_platforms": ["TikTok"],
        "ideal_creator_profile": "Fashion creator",
        "risk_factors": [],
        "comparable_viral_products": [],
    },
    "product_data_used": {"name": "Classic Shirt", "category": "fashion", "price": "", "scraped": False},
    "virality_trajectory": {"trajectory": "linear", "viral_probability": 0.72},
}


@pytest.fixture(autouse=True)
def _setup_db():
    SQLModel.metadata.create_all(_TEST_ENGINE)
    yield
    SQLModel.metadata.drop_all(_TEST_ENGINE)


@pytest.fixture
def app():
    with (
        patch("db.database.engine", _TEST_ENGINE),
        patch("main.engine", _TEST_ENGINE),
        patch("main._cache") as mock_cache,
        patch("main._notify_mastra_start", new_callable=AsyncMock, return_value=None),
    ):
        mock_cache.get = AsyncMock(return_value=None)
        mock_cache.set = AsyncMock()
        mock_cache.virality_key = MagicMock(return_value="test_key")

        from main import app as _app
        yield _app


@pytest_asyncio.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_virality_text_input_returns_score(client):
    """POST /virality with product_name + description → 200, score returned."""
    mock_predictor = AsyncMock()
    mock_predictor.predict = AsyncMock(return_value=_VIRALITY_RESULT)

    with patch("main.ViralityPredictor", return_value=mock_predictor):
        resp = await client.post("/virality", json={
            "product_name": "Classic Shirt",
            "description": "Premium cotton shirt for modern men.",
        })

    assert resp.status_code == 200
    data = resp.json()
    assert "run_id" in data
    assert "score" in data
    assert data["score"] == 72


@pytest.mark.asyncio
async def test_virality_url_input_returns_score(client):
    """POST /virality with URL → 200, score returned."""
    mock_predictor = AsyncMock()
    mock_predictor.predict = AsyncMock(return_value=_VIRALITY_RESULT)

    with patch("main.ViralityPredictor", return_value=mock_predictor):
        resp = await client.post("/virality", json={
            "url": "https://testbrand.in/products/shirt",
            "product_name": "Classic Shirt",
            "description": "Premium shirt.",
        })

    assert resp.status_code == 200
    assert resp.json()["score"] is not None


@pytest.mark.asyncio
async def test_virality_missing_required_fields_422(client):
    """POST /virality with empty body → 422."""
    resp = await client.post("/virality", json={})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_virality_score_in_response(client):
    """Score in response matches what the predictor returned."""
    mock_predictor = AsyncMock()
    mock_predictor.predict = AsyncMock(return_value={**_VIRALITY_RESULT, "score": 55})

    with patch("main.ViralityPredictor", return_value=mock_predictor):
        resp = await client.post("/virality", json={
            "product_name": "Shirt",
            "description": "Great shirt.",
        })

    assert resp.json()["score"] == 55


@pytest.mark.asyncio
async def test_virality_report_endpoint(client):
    """GET /virality/{run_id}/report → 200 with HTML when run is complete."""
    mock_predictor = AsyncMock()
    mock_predictor.predict = AsyncMock(return_value=_VIRALITY_RESULT)

    with patch("main.ViralityPredictor", return_value=mock_predictor):
        create_resp = await client.post("/virality", json={
            "product_name": "Classic Shirt",
            "description": "Premium shirt.",
        })
    run_id = create_resp.json()["run_id"]

    with patch("main.generate_virality_card", return_value="<html>Virality Report</html>"):
        report_resp = await client.get(f"/virality/{run_id}/report")

    assert report_resp.status_code == 200
    assert "Virality" in report_resp.text


@pytest.mark.asyncio
async def test_virality_report_404_unknown_id(client):
    """GET /virality/99999/report → 404."""
    resp = await client.get("/virality/99999/report")
    assert resp.status_code == 404
