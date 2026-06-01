"""Orchestrates all 6 agents in phased parallelism, writes progress to DB + console."""
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
    """Run a complete audit without touching the database."""
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

    # ── Phase 1: parallel data gathering ──────────────────────────────────────
    prefetched = await _run_phase1(url, brand_name, scraper, search, llm)

    # ── Phase 2: brand_basics (sequential — others depend on its output) ──────
    t0 = time.monotonic()
    try:
        bb_result = await agents["brand_basics"].run(url, brand_name, prefetched=prefetched)
        bb_error = bb_result.get("error")
    except Exception as exc:
        bb_error = str(exc)
        bb_result = _err_result("brand_basics", exc)
    bb_elapsed = time.monotonic() - t0
    results["brand_basics"] = bb_result
    agent_status.append(_make_status("brand_basics", bb_result, bb_elapsed, bb_error))
    _log(1, 6, "Brand Basics", bb_elapsed, bb_error)

    # ── Phase 3: four agents in parallel ──────────────────────────────────────
    phase3_keys = ["content_catalog", "performance_ads", "geo_visibility", "store_cro"]
    t3 = time.monotonic()
    phase3_outcomes = await asyncio.gather(
        *[_timed(k, agents[k].run(url, brand_name, prefetched=prefetched)) for k in phase3_keys],
        return_exceptions=True,
    )
    for p3_idx, (key, outcome, elapsed) in enumerate(phase3_outcomes, start=2):
        if isinstance(outcome, Exception):
            err = str(outcome)
            res = _err_result(key, outcome)
        else:
            err = outcome.get("error")
            res = outcome
        results[key] = res
        agent_status.append(_make_status(key, res, elapsed, err))
        _log(p3_idx, 6, _AGENT_LABELS[key], elapsed, err)

    print(f"  [phase3] Parallel agents done ({round(time.monotonic() - t3, 1)}s total)", flush=True)

    # ── Phase 4: research (uses brand_basics context) ─────────────────────────
    context = {
        "category":          _nested(bb_result, "analysis", "core_categories"),
        "brand_positioning": _nested(bb_result, "analysis", "brand_positioning"),
    }
    t0 = time.monotonic()
    try:
        rs_result = await agents["research"].run(
            url, brand_name, prefetched=prefetched, context=context
        )
        rs_error = rs_result.get("error")
    except Exception as exc:
        rs_error = str(exc)
        rs_result = _err_result("research", exc)
    rs_elapsed = time.monotonic() - t0
    results["research"] = rs_result
    agent_status.append(_make_status("research", rs_result, rs_elapsed, rs_error))
    _log(6, 6, "Competitive Research", rs_elapsed, rs_error)

    # ── Phase 5: compile ──────────────────────────────────────────────────────
    total_time = round(time.monotonic() - overall_start, 2)
    overall_coverage, failed_agents = _classify_failure(results)
    print(f"\n  Completed in {total_time}s", flush=True)
    if overall_coverage == "critical_failure":
        print(
            f"  ⚠  {len(failed_agents)} agents failed — partial report only.",
            flush=True,
        )

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
    """Background entry point — reads/writes AuditRun row in SQLite."""
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
    failed_count = 0

    # ── Phase 1: parallel data gathering ──────────────────────────────────────
    with Session(engine) as session:
        audit = session.get(AuditRun, audit_id)
        _db_set(session, audit, current_agent="gathering_data", progress_pct=5)

    prefetched = await _run_phase1(url, brand_name, scraper, search, llm)

    # ── Phase 2: brand_basics ─────────────────────────────────────────────────
    with Session(engine) as session:
        audit = session.get(AuditRun, audit_id)
        _db_set(session, audit, current_agent="brand_basics", progress_pct=15)

    try:
        bb_result = await agents["brand_basics"].run(url, brand_name, prefetched=prefetched)
        if "error" in bb_result:
            failed_count += 1
    except Exception as exc:
        bb_result = _err_result("brand_basics", exc)
        failed_count += 1
    agent_results["brand_basics"] = bb_result

    with Session(engine) as session:
        audit = session.get(AuditRun, audit_id)
        _db_set(session, audit, brand_basics=json.dumps(bb_result), progress_pct=25)

    # ── Phase 3: four agents in parallel ──────────────────────────────────────
    with Session(engine) as session:
        audit = session.get(AuditRun, audit_id)
        _db_set(session, audit, current_agent="content_catalog", progress_pct=30)

    phase3_keys = ["content_catalog", "performance_ads", "geo_visibility", "store_cro"]
    phase3_outcomes = await asyncio.gather(
        *[_timed(k, agents[k].run(url, brand_name, prefetched=prefetched)) for k in phase3_keys],
        return_exceptions=True,
    )
    phase3_writes: dict = {}
    for key, outcome, elapsed in phase3_outcomes:
        if isinstance(outcome, Exception):
            res = _err_result(key, outcome)
            failed_count += 1
        else:
            if "error" in outcome:
                failed_count += 1
            res = outcome
        agent_results[key] = res
        phase3_writes[key] = json.dumps(res)
        _log(len(agent_results), 6, _AGENT_LABELS[key], elapsed, res.get("error"))

    with Session(engine) as session:
        audit = session.get(AuditRun, audit_id)
        _db_set(session, audit, **phase3_writes, progress_pct=80)

    # ── Phase 4: research ─────────────────────────────────────────────────────
    with Session(engine) as session:
        audit = session.get(AuditRun, audit_id)
        _db_set(session, audit, current_agent="research", progress_pct=85)

    context = {
        "category":          _nested(bb_result, "analysis", "core_categories"),
        "brand_positioning": _nested(bb_result, "analysis", "brand_positioning"),
    }
    try:
        rs_result = await agents["research"].run(
            url, brand_name, prefetched=prefetched, context=context
        )
        if "error" in rs_result:
            failed_count += 1
    except Exception as exc:
        rs_result = _err_result("research", exc)
        failed_count += 1
    agent_results["research"] = rs_result

    with Session(engine) as session:
        audit = session.get(AuditRun, audit_id)
        _db_set(session, audit, research=json.dumps(rs_result), progress_pct=95)

    # ── Phase 5: one_thing compile ─────────────────────────────────────────────
    print("  Generating highest-impact recommendation…", flush=True)
    one_thing = await _generate_one_thing(llm, agent_results)

    overall_coverage = (
        "critical_failure" if failed_count >= 4
        else "partial" if failed_count > 0
        else "complete"
    )
    with Session(engine) as session:
        audit = session.get(AuditRun, audit_id)
        _db_set(
            session,
            audit,
            status="complete",
            current_agent=None,
            progress_pct=100,
            one_thing=one_thing,
        )

    print(f"  Audit #{audit_id} complete (coverage: {overall_coverage}).", flush=True)
    if one_thing:
        print(f"  ⚡ One thing: {one_thing}", flush=True)
