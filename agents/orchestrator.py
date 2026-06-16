"""Orchestrator — delegates to the agentic orchestrator (ReAct loop).

The linear sequential pipeline is preserved here for reference but all
public entry points (run_full_audit, run_all) now delegate to
agents.agentic_orchestrator which wraps each agent run with a reasoning
brain that can skip, reorder, and cross-synthesize across agents.
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
from agents.social_profile import SocialProfileAgent
from agents.social_media_audit import SocialMediaAuditAgent

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
    "brand_basics":       "Brand Basics",
    "content_catalog":    "Content Audit",
    "performance_ads":    "Ad Intelligence",
    "geo_visibility":     "GEO Visibility",
    "store_cro":          "Store & CRO",
    "research":           "Competitive Research",
    "social_profile":     "Social & Brand Presence",
    "social_media_audit": "Social Media Deep Audit",
}

_ROADMAP_PROMPT = """\
You are a D2C ecommerce consultant creating a 30-day action roadmap.
Given this brand audit, return a concrete week-by-week plan.

Output ONLY valid JSON — no preamble, no markdown. Start with { and end with }:
{
  "week_1": [
    {"days": "Day 1-2", "task": "specific action sentence", "effort": "2-4 hours", "impact": "+X% metric", "agent": "Store & CRO"}
  ],
  "week_2_3": [
    {"days": "Day 8-10", "task": "...", "effort": "1 day", "impact": "+Y%", "agent": "GEO & AI Visibility"}
  ],
  "week_4": [
    {"days": "Day 22-28", "task": "...", "effort": "3-5 days", "impact": "+Z%", "agent": "Content & Catalog"}
  ]
}

Rules:
- week_1: exactly 3 tasks — Low effort, highest-impact quick wins from the worst scores
- week_2_3: exactly 3 tasks — Medium effort improvements
- week_4: exactly 2 tasks — Foundation work for long-term growth
- Every task must cite a specific finding from the audit (not generic advice)
- impact must be a specific metric range (e.g. '+5-8% mobile conversion')
- effort must be realistic calendar time (e.g. '1-2 hours', '1 day', '3-5 days')
- agent must be one of: Brand Basics | Content & Catalog | Performance & Ads | GEO & AI Visibility | Store & CRO | Competitive Intel
"""

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


async def _generate_roadmap(llm, results: dict) -> dict:
    """One LLM call that turns all 6 agent outputs into a 30-day action roadmap."""
    try:
        summary = {
            "scores": {
                "geo":           _nested(results, "geo_visibility",  "analysis", "geo_score"),
                "mobile_speed":  _nested(results, "store_cro",       "pagespeed", "mobile_score"),
                "pdp_quality":   _nested(results, "content_catalog", "analysis", "pdp_quality_score"),
                "hook_strength": _nested(results, "performance_ads", "analysis", "hook_strength_score"),
                "cro":           _nested(results, "store_cro",       "analysis", "cro_score"),
                "research":      _nested(results, "research",        "analysis", "research_score"),
            },
            "top_cro_fixes":    (_nested(results, "store_cro",       "analysis", "top_5_cro_fixes")    or [])[:3],
            "schema_missing":   (_nested(results, "geo_visibility",  "analysis", "schema_missing")      or [])[:3],
            "pdp_weaknesses":   (_nested(results, "content_catalog", "analysis", "pdp_weaknesses")      or [])[:3],
            "top_3_content":    (_nested(results, "content_catalog", "analysis", "top_3_improvements")  or [])[:3],
            "ad_quick_wins":    (_nested(results, "performance_ads", "analysis", "top_3_ad_quick_wins") or [])[:2],
            "geo_roadmap":      (_nested(results, "geo_visibility",  "analysis", "geo_improvement_roadmap") or [])[:2],
            "strategic_recs":   (_nested(results, "research",        "analysis", "strategic_recommendations") or [])[:2],
            "whitespace":       _nested(results, "research", "whitespace"),
        }
        raw = await llm.analyze_structured(
            system_prompt=_ROADMAP_PROMPT,
            user_content=f"Audit data:\n{json.dumps(summary, indent=2, default=str)}",
            max_tokens=1400,
            temperature=0.3,
        )
        if "_parse_error" in raw:
            return {}
        return raw
    except Exception as exc:
        print(f"  [roadmap] skipped — {exc}", flush=True)
        return {}


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
        "brand_basics":       BrandBasicsAgent(llm, scraper, search),
        "content_catalog":    ContentCatalogAgent(llm, scraper, search),
        "performance_ads":    PerformanceAdsAgent(llm, scraper, search),
        "geo_visibility":     GEOVisibilityAgent(llm, scraper, search),
        "store_cro":          StoreCROAgent(llm, scraper, search),
        "research":           ResearchAgent(llm, scraper, search),
        "social_profile":     SocialProfileAgent(llm, scraper, search),
        "social_media_audit": SocialMediaAuditAgent(llm, search),
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

async def run_full_audit(url: str, deep_visual: bool = False) -> dict:
    """Run a complete agentic audit without touching the database.

    Delegates to agents.agentic_orchestrator which wraps the pipeline
    in a ReAct reasoning loop (plan → act → observe → synthesize).
    """
    from agents.agentic_orchestrator import run_full_audit as _agentic_run
    return await _agentic_run(url, deep_visual=deep_visual)


# ── DB-backed entry point (called by FastAPI BackgroundTasks) ──────────────────

async def run_all(audit_id: int, deep_visual: bool = False) -> None:
    """DB-backed agentic audit. Delegates to agents.agentic_orchestrator."""
    from agents.agentic_orchestrator import run_all as _agentic_run_all
    await _agentic_run_all(audit_id, deep_visual=deep_visual)
