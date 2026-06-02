"""Orchestrates all 6 agents sequentially, writes progress to DB + console.

Sequential execution chosen deliberately for Groq free-tier reliability:
parallel LLM calls exhaust the token-per-minute limit (~14-20k tokens/min)
causing cascading 429 retries that make the pipeline slower, not faster.
Revert to parallel when on a paid LLM tier with higher rate limits.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

from sqlmodel import Session

from db.database import engine
from db.models import AuditRun, AGENT_SEQUENCE
from llm.client import get_client
from scrapers.web_scraper import WebScraper
from scrapers.search import SearchAgent
from scrapers.pagespeed import get_scores
from scrapers.meta_ads import get_ads
from scrapers.result import DataResult

from agents.brand_basics import BrandBasicsAgent
from agents.content_catalog import ContentCatalogAgent
from agents.performance_ads import PerformanceAdsAgent
from agents.geo_visibility import GEOVisibilityAgent
from agents.store_cro import StoreCROAgent
from agents.research import ResearchAgent

# ── Helpers ────────────────────────────────────────────────────────────────────

def _brand_name_from_url(url: str) -> str:
    netloc = urlparse(url).netloc.lower()
    netloc = re.sub(r"^www\.", "", netloc)
    name_part = netloc.split(".")[0]
    return " ".join(w.capitalize() for w in re.split(r"[-_]", name_part))


def _db_set(session: Session, audit: AuditRun, **kwargs) -> None:
    for k, v in kwargs.items():
        setattr(audit, k, v)
    session.add(audit)
    session.commit()
    session.refresh(audit)


def _log(idx: int, total: int, label: str, elapsed: float, error: str | None = None) -> None:
    status = f"error — {error}" if error else f"done ({elapsed:.1f}s)"
    print(f"  [{idx}/{total}] {label}… {status}", flush=True)


_AGENT_LABELS = {
    "brand_basics":    "Brand Basics",
    "content_catalog": "Content Audit",
    "performance_ads": "Ad Intelligence",
    "geo_visibility":  "GEO Visibility",
    "store_cro":       "Store & CRO",
    "research":        "Competitive Research",
}

_ONE_THING_PROMPT = (
    "You are a senior ecommerce consultant. Given this complete brand audit data, "
    "identify THE single highest-impact action this brand can take in the next 7 days. "
    "It must be:\n"
    "- Specific (not 'improve SEO' — say 'add aggregateRating schema to all product pages')\n"
    "- Measurable (estimate the impact: '+5-8% organic click-through')\n"
    "- Low effort (can be done in 1-3 days)\n"
    "- Based on the worst score that is easiest to fix\n\n"
    "Format: ONE sentence, max 25 words, action-first.\n"
    "Example: 'Add sticky Add-to-Cart on mobile PDPs — your 61/100 mobile score and "
    "missing ATC are costing ~15% of mobile conversions.'\n\n"
    "Output only the sentence, nothing else."
)


async def _generate_one_thing(llm, results: dict) -> str:
    """Call LLM to identify the single highest-impact 7-day action."""
    try:
        summary = {
            "geo_score":        _nested(results, "geo_visibility",  "analysis",  "geo_score"),
            "mobile_pagespeed": _nested(results, "store_cro",       "pagespeed", "mobile_score"),
            "pdp_quality":      _nested(results, "content_catalog", "analysis",  "pdp_quality_score"),
            "hook_strength":    _nested(results, "performance_ads", "analysis",  "hook_strength_score"),
            "homepage_score":   _nested(results, "content_catalog", "analysis",  "homepage_score"),
            "top_cro_fix":      _nested(results, "store_cro",       "analysis",  "top_5_cro_fixes"),
            "schema_missing":   _nested(results, "geo_visibility",  "analysis",  "schema_missing"),
            "pdp_weaknesses":   _nested(results, "content_catalog", "analysis",  "pdp_weaknesses"),
        }
        text = await llm.analyze(
            system_prompt=_ONE_THING_PROMPT,
            user_content=f"Audit scores:\n{json.dumps(summary, indent=2, default=str)}",
            max_tokens=120,
            temperature=0.2,
        )
        return text.strip().strip("\"'")
    except Exception as exc:
        print(f"  [one_thing] skipped — {exc}", flush=True)
        return ""


def _nested(d: dict, *keys, default=None):
    """Safe nested dict get."""
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k, default)
        if cur is None:
            return default
    return cur


_MANUAL_TOOL_LINKS = [
    "Google PageSpeed: https://pagespeed.web.dev/",
    "Meta Ad Library: https://www.facebook.com/ads/library/",
    "Schema Markup Validator: https://validator.schema.org/",
    "Google Search Console: https://search.google.com/search-console/",
]


def _build_agents(llm, scraper, search):
    return {
        "brand_basics":    BrandBasicsAgent(llm, scraper, search),
        "content_catalog": ContentCatalogAgent(llm, scraper, search),
        "performance_ads": PerformanceAdsAgent(llm, scraper, search),
        "geo_visibility":  GEOVisibilityAgent(llm, scraper, search),
        "store_cro":       StoreCROAgent(llm, scraper, search),
        "research":        ResearchAgent(llm, scraper, search),
    }


def _classify_failure(results: dict) -> tuple[str, list[str]]:
    """Return (overall_coverage, [failed_agent_keys]) based on agent statuses."""
    failed = [
        key for key, res in results.items()
        if "error" in res or res.get("status") == "failed"
    ]
    return ("critical_failure" if len(failed) >= 4 else "partial"), failed


def _err_result(agent_key: str, exc: Exception) -> dict:
    return {
        "agent": agent_key,
        "error": str(exc),
        "status": "failed",
        "sources_used": [],
        "data_coverage": "unavailable",
        "fallbacks_used": [],
    }


def _make_status(agent_key: str, result: dict, elapsed: float, error: str | None) -> dict:
    return {
        "agent": agent_key,
        "label": _AGENT_LABELS[agent_key],
        "status": "error" if error else "done",
        "elapsed_s": round(elapsed, 2),
        "error": error,
        "data_coverage": result.get("data_coverage", "unknown"),
        "fallbacks_used": result.get("fallbacks_used", []),
    }


async def _timed(key: str, coro):
    """Run a coroutine and return (key, result_or_exception, elapsed_s)."""
    t = time.monotonic()
    try:
        result = await coro
    except Exception as exc:
        result = exc
    return key, result, round(time.monotonic() - t, 2)


# ── Phase 1: parallel data gathering ──────────────────────────────────────────

async def _run_phase1(
    url: str,
    brand_name: str,
    scraper: "WebScraper",
    search: "SearchAgent",
    llm,
) -> dict:
    """Scrape homepage, PageSpeed, and Meta Ads concurrently — no LLM calls."""
    t0 = time.monotonic()
    raw = await asyncio.gather(
        scraper.scrape_page(url),
        get_scores(url),
        get_ads(brand_name, search_agent=search, llm_client=llm),
        return_exceptions=True,
    )
    homepage, pagespeed, meta_ads = raw

    def _safe(dr, source: str) -> DataResult:
        if isinstance(dr, Exception):
            return DataResult(value={}, confidence="unavailable", source=source, error=str(dr))
        return dr

    elapsed = round(time.monotonic() - t0, 2)
    print(f"  [phase1] Data gathering done ({elapsed}s)", flush=True)

    return {
        "homepage": _safe(homepage, "homepage_scrape"),
        "pagespeed": _safe(pagespeed, "pagespeed"),
        "meta_ads":  _safe(meta_ads, "meta_ads"),
    }


# ── Standalone entry point (no DB) ────────────────────────────────────────────

async def run_full_audit(url: str) -> dict:
    """Run a complete 6-agent audit sequentially without touching the database."""
    brand_name = _brand_name_from_url(url)
    llm     = get_client()
    scraper = WebScraper()
    search  = SearchAgent()
    agents  = _build_agents(llm, scraper, search)

    overall_start = time.monotonic()
    results: dict = {}
    agent_status: list[dict] = []

    print(f"\nBrand Audit — {url}", flush=True)
    print(f"Brand: {brand_name}", flush=True)
    print("-" * 48, flush=True)

    for idx, key in enumerate(AGENT_SEQUENCE, start=1):
        label = _AGENT_LABELS[key]
        t0 = time.monotonic()
        error = None
        try:
            result = await agents[key].run(url, brand_name)
            if "error" in result:
                error = result["error"]
        except Exception as exc:
            error = str(exc)
            result = _err_result(key, exc)
        elapsed = time.monotonic() - t0
        results[key] = result
        agent_status.append(_make_status(key, result, elapsed, error))
        _log(idx, 6, label, elapsed, error)

    total_time = round(time.monotonic() - overall_start, 2)
    overall_coverage, failed_agents = _classify_failure(results)
    print(f"\n  Completed in {total_time}s", flush=True)
    if overall_coverage == "critical_failure":
        print(f"  ⚠  {len(failed_agents)} agents failed — partial report only.", flush=True)

    print("  Generating highest-impact recommendation…", flush=True)
    one_thing = await _generate_one_thing(llm, results)

    return {
        "url": url,
        "brand_name": brand_name,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_time_seconds": total_time,
        "overall_coverage": overall_coverage,
        "failed_agents": failed_agents,
        "manual_tools": _MANUAL_TOOL_LINKS if overall_coverage == "critical_failure" else [],
        "agent_status": agent_status,
        "one_thing": one_thing,
        "results": results,
    }


# ── DB-backed entry point (called by FastAPI BackgroundTasks) ──────────────────

async def run_all(audit_id: int) -> None:
    """Background entry point — sequential 6-agent run, writes progress to DB."""
    with Session(engine) as session:
        audit = session.get(AuditRun, audit_id)
        if not audit:
            return
        url = audit.url
        _db_set(session, audit, status="running", progress_pct=0)

    brand_name = _brand_name_from_url(url)
    llm     = get_client()
    scraper = WebScraper()
    search  = SearchAgent()
    agents  = _build_agents(llm, scraper, search)

    print(f"\nBrand Audit #{audit_id} — {url}", flush=True)
    print(f"Brand: {brand_name}", flush=True)
    print("-" * 48, flush=True)

    agent_results: dict = {}
    failed_count   = 0
    db_field_map   = {
        "brand_basics":    "brand_basics",
        "content_catalog": "content_catalog",
        "performance_ads": "performance_ads",
        "geo_visibility":  "geo_visibility",
        "store_cro":       "store_cro",
        "research":        "research",
    }

    for idx, key in enumerate(AGENT_SEQUENCE, start=1):
        label    = _AGENT_LABELS[key]
        progress = int((idx - 1) / 6 * 100)

        with Session(engine) as session:
            audit = session.get(AuditRun, audit_id)
            _db_set(session, audit, current_agent=key, progress_pct=progress)

        t0 = time.monotonic()
        try:
            result = await agents[key].run(url, brand_name)
            if "error" in result:
                failed_count += 1
        except Exception as exc:
            result = _err_result(key, exc)
            failed_count += 1
        elapsed = time.monotonic() - t0

        agent_results[key] = result
        _log(idx, 6, label, elapsed, result.get("error"))

        with Session(engine) as session:
            audit = session.get(AuditRun, audit_id)
            _db_set(session, audit,
                    **{db_field_map[key]: json.dumps(result)},
                    progress_pct=int(idx / 6 * 95))

    print("  Generating highest-impact recommendation…", flush=True)
    one_thing = await _generate_one_thing(llm, agent_results)

    overall_coverage = (
        "critical_failure" if failed_count >= 4
        else "partial"     if failed_count > 0
        else "complete"
    )
    with Session(engine) as session:
        audit = session.get(AuditRun, audit_id)
        _db_set(session, audit,
                status="complete",
                current_agent=None,
                progress_pct=100,
                one_thing=one_thing)

    print(f"  Audit #{audit_id} complete (coverage: {overall_coverage}).", flush=True)
    if one_thing:
        print(f"  ⚡ One thing: {one_thing}", flush=True)
