"""Agentic orchestrator — phased parallel pipeline with bounded retry loops.

Architecture (applying constrained-loop principles from Greg Isenberg / Ross Mike):
============
  BAD loop: LLM-decides-at-every-step → token-burning slot machine, makes assumptions
  GOOD loop: constrained, binary-feedback, bounded max-attempts — like a code review gate

  ┌─ PHASE 0: Prefetch (parallel, zero LLM) ──────────────────────────────────┐
  │  homepage scrape + pagespeed                                                 │
  └──────────────────────────────────────────────────────────────────────────────┘
           ↓
  ┌─ PHASE 1: Brand foundation (sequential, 1 LLM call) ──────────────────────┐
  │  brand_basics — validates site is live, detects Shopify catalog             │
  │  ABORT if brand URL is completely unreachable                               │
  └──────────────────────────────────────────────────────────────────────────────┘
           ↓
  ┌─ PHASE 2: Core intelligence (fully parallel, bounded retry) ───────────────┐
  │  content_catalog  │ performance_ads  │ geo_visibility                       │
  │  store_cro        │ research         │ social_profile                       │
  │                                                                              │
  │  Each agent: attempt 1 → quality_check → pass OR retry once → accept best  │
  └──────────────────────────────────────────────────────────────────────────────┘
           ↓
  ┌─ PHASE 3: Social depth (sequential, depends on social_profile result) ─────┐
  │  social_media_audit — skipped if social_profile found zero presence         │
  └──────────────────────────────────────────────────────────────────────────────┘
           ↓
  ┌─ PHASE 4: Synthesis (3 LLM calls total, not per-step) ────────────────────┐
  │  cross_validate_findings (rule-based, zero LLM)                             │
  │  analyst_brief           (1 LLM call)                                       │
  │  one_thing               (1 LLM call)                                       │
  │  roadmap                 (1 LLM call)                                       │
  └──────────────────────────────────────────────────────────────────────────────┘

  Total LLM calls: 3–4 (not 30+).
  Bounded retry: each agent max 2 attempts — binary check "did we get useful data?"
  Meta Ads: deprioritized — removed from Phase 0, remains importable but not blocking.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from sqlmodel import Session

from db.database import engine
from db.models import AuditRun, AGENT_SEQUENCE
from llm.client import get_client
from scrapers.web_scraper import WebScraper
from scrapers.search import SearchAgent
from scrapers.pagespeed import get_scores
from scrapers.result import DataResult

from agents.working_memory import WorkingMemory
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


def _nested(d: dict, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k, default)
        if cur is None:
            return default
    return cur


def _db_set(session: Session, audit: AuditRun, **kwargs) -> None:
    for k, v in kwargs.items():
        setattr(audit, k, v)
    session.add(audit)
    session.commit()
    session.refresh(audit)


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


# ── Quality gates (binary feedback — the good kind of loop signal) ────────────

def _is_useful(agent_key: str, result: dict) -> bool:
    """Binary check: did this agent return usable data?

    This is the feedback signal for the retry loop — not a vague score,
    a hard yes/no per agent. If False, we retry once. Max 2 attempts.
    """
    if not isinstance(result, dict):
        return False
    if result.get("status") == "failed" or "error" in result:
        return False

    checks = {
        "brand_basics":    lambda r: bool(r.get("analysis")),
        "content_catalog": lambda r: bool(r.get("analysis") or r.get("pdps_scraped")),
        "performance_ads": lambda r: bool(r.get("analysis") or r.get("ads_scrape")),
        "geo_visibility":  lambda r: bool(r.get("analysis")),
        "store_cro":       lambda r: bool(r.get("analysis") or r.get("pagespeed")),
        "research":        lambda r: bool(r.get("analysis")),
        "social_profile":  lambda r: (
            bool(r.get("instagram", {}).get("followers"))
            or bool(r.get("instagram", {}).get("bio"))
        ),
        "social_media_audit": lambda r: bool(r.get("scores") or r.get("platforms")),
    }
    check = checks.get(agent_key)
    return check(result) if check else True


def _err_result(agent_key: str, exc: Exception) -> dict:
    return {
        "agent": agent_key,
        "error": str(exc),
        "status": "failed",
        "sources_used": [],
        "data_coverage": "unavailable",
        "fallbacks_used": [],
    }


# ── Bounded retry wrapper ─────────────────────────────────────────────────────

# Agents that do multi-step scraping + LLM need more time, especially under rate limiting.
_AGENT_TIMEOUTS: dict[str, int] = {
    "content_catalog":   200,  # PDP scraping (3 pages) + Shopify fallback + LLM
    "store_cro":         200,  # PageSpeed + scraping + LLM
    "social_media_audit": 200,  # IG + YT scraping + LLM
    "geo_visibility":    150,
}


async def _run_with_retry(
    agent_key: str,
    agent,
    url: str,
    brand_name: str,
    max_attempts: int = 2,
    timeout_s: int = 120,
    deep_visual: bool = False,
    progress_cb=None,
    progress_pct: int = 0,
) -> tuple[dict, str | None]:
    """Run an agent with bounded retry on quality failure.

    Retry loop logic (from transcript): constrained, binary feedback, max 2 attempts.
    Attempt 1 fails quality check → wait 3s → attempt 2 → accept whatever we get.
    Never blindly retries forever — that's the slot machine anti-pattern.

    Returns (result, error_string | None).
    """
    label = _AGENT_LABELS.get(agent_key, agent_key)
    last_result = _err_result(agent_key, RuntimeError("never ran"))
    last_error: str | None = None
    effective_timeout = _AGENT_TIMEOUTS.get(agent_key, timeout_s)

    for attempt in range(1, max_attempts + 1):
        if progress_cb:
            await progress_cb(agent_key, "running", progress_pct)

        t0 = time.monotonic()
        try:
            if agent_key == "social_media_audit":
                result = await asyncio.wait_for(
                    agent.run(url, brand_name, deep_visual=deep_visual), timeout=effective_timeout
                )
            else:
                result = await asyncio.wait_for(
                    agent.run(url, brand_name), timeout=effective_timeout
                )
            elapsed = round(time.monotonic() - t0, 2)
            last_result = result
            last_error = result.get("error")

            if _is_useful(agent_key, result):
                suffix = f" (attempt {attempt})" if attempt > 1 else ""
                print(f"    [{label}] done ({elapsed}s){suffix}", flush=True)
                if progress_cb:
                    await progress_cb(agent_key, "done", progress_pct + 5)
                return result, None

            # Quality check failed — retry if attempts remain
            if attempt < max_attempts:
                print(
                    f"    [{label}] attempt {attempt} returned poor data "
                    f"({elapsed}s) — retrying in 3s…",
                    flush=True,
                )
                await asyncio.sleep(3)
            else:
                print(
                    f"    [{label}] attempt {attempt} — accepting best-effort result "
                    f"({elapsed}s)",
                    flush=True,
                )

        except asyncio.TimeoutError:
            elapsed = round(time.monotonic() - t0, 2)
            last_error = f"Timed out after {elapsed:.0f}s"
            last_result = {
                "agent": agent_key, "error": last_error,
                "status": "timeout", "partial": True,
                "data_coverage": "unavailable", "sources_used": [], "fallbacks_used": [],
            }
            print(f"    [{label}] TIMEOUT after {elapsed:.0f}s (attempt {attempt})", flush=True)
            if attempt < max_attempts:
                await asyncio.sleep(3)

        except Exception as exc:
            elapsed = round(time.monotonic() - t0, 2)
            last_error = str(exc)
            last_result = _err_result(agent_key, exc)
            print(f"    [{label}] ERROR ({elapsed:.1f}s): {exc}", flush=True)
            if attempt < max_attempts:
                await asyncio.sleep(3)

    if progress_cb:
        await progress_cb(agent_key, "error" if last_error else "done", progress_pct + 5)
    return last_result, last_error


# ── LLM semaphore (Groq free-tier rate limit protection) ─────────────────────

_AGENT_LLM_GATE = asyncio.Semaphore(2)


async def _run_gated(
    agent_key: str, agent, url: str, brand_name: str,
    deep_visual: bool = False, progress_cb=None, progress_pct: int = 0,
) -> tuple[str, dict, str | None, float]:
    """Gate-wrapped retry runner for parallel execution."""
    label = _AGENT_LABELS.get(agent_key, agent_key)
    print(f"  [parallel] {label} — queued…", flush=True)
    t0 = time.monotonic()
    async with _AGENT_LLM_GATE:
        print(f"  [parallel] {label} — running…", flush=True)
        result, error = await _run_with_retry(
            agent_key, agent, url, brand_name,
            deep_visual=deep_visual, progress_cb=progress_cb, progress_pct=progress_pct,
        )
    return agent_key, result, error, round(time.monotonic() - t0, 2)


# ── Phase 0: Parallel prefetch (no LLM) ──────────────────────────────────────

async def _phase0_prefetch(url: str, scraper: WebScraper) -> dict:
    """Scrape homepage + pagespeed concurrently. Zero LLM calls."""
    t0 = time.monotonic()
    homepage_r, pagespeed_r = await asyncio.gather(
        scraper.scrape_page(url),
        get_scores(url),
        return_exceptions=True,
    )

    def _safe(r, source: str) -> DataResult:
        if isinstance(r, Exception):
            return DataResult(value={}, confidence="unavailable", source=source, error=str(r))
        return r

    elapsed = round(time.monotonic() - t0, 2)
    print(f"  [phase0] Prefetch done ({elapsed}s)", flush=True)

    return {
        "homepage":  _safe(homepage_r, "homepage_scrape"),
        "pagespeed": _safe(pagespeed_r, "pagespeed"),
    }


# ── Cross-agent rule-based pattern detection (zero LLM) ──────────────────────

def _cross_validate_findings(results: dict) -> list[dict]:
    """Detect cross-agent patterns — rule-based, zero LLM, runs after all agents."""
    findings: list[dict] = []

    geo_score    = _nested(results, "geo_visibility",  "analysis", "geo_score")     or 0
    cro_score    = _nested(results, "store_cro",       "analysis", "cro_score")     or 0
    mobile_score = _nested(results, "store_cro",       "pagespeed", "mobile_score") or 0
    pdp_score    = _nested(results, "content_catalog", "analysis", "pdp_quality_score") or 0
    hook_score   = _nested(results, "performance_ads", "analysis", "hook_strength_score") or 0
    ig_followers = _nested(results, "social_profile",  "instagram", "followers")    or 0
    reviews      = _nested(results, "content_catalog", "analysis", "reviews_count") or 0

    if hook_score > 6 and cro_score < 5:
        findings.append({
            "pattern": "Paid-to-site conversion leak",
            "evidence": f"Ad hook {hook_score}/10 but CRO {cro_score}/10 — paid traffic not converting.",
            "impact": "high",
            "recommendation": "Fix CRO before scaling ad spend.",
        })
    if ig_followers > 50_000 and cro_score < 5:
        findings.append({
            "pattern": "Social audience not converting",
            "evidence": f"{ig_followers:,} IG followers, CRO {cro_score}/10 — social-to-purchase funnel broken.",
            "impact": "high",
            "recommendation": "Add sticky bio link → landing page with UGC → one-click checkout.",
        })
    if pdp_score > 6 and geo_score < 5:
        findings.append({
            "pattern": "Good content, AI-invisible",
            "evidence": f"PDP {pdp_score}/10 but GEO {geo_score}/10 — not surfacing on AI search.",
            "impact": "high",
            "recommendation": "Add FAQ schema, brand-entity markup, long-form buying guides.",
        })
    if mobile_score < 50:
        findings.append({
            "pattern": "Mobile performance bottleneck",
            "evidence": f"PageSpeed mobile {mobile_score}/100 — most D2C traffic in India is mobile-first.",
            "impact": "high",
            "recommendation": "Fix LCP (hero image) and remove render-blocking scripts. Target >70.",
        })
    if pdp_score > 5 and isinstance(reviews, int) and reviews < 10:
        findings.append({
            "pattern": "Trust gap — strong product, no social proof",
            "evidence": f"PDP {pdp_score}/10 but <10 visible reviews.",
            "impact": "medium",
            "recommendation": "Post-purchase review flow (email + WhatsApp). Display avg rating on hero.",
        })
    if ig_followers > 30_000 and geo_score < 4:
        findings.append({
            "pattern": "Brand awareness not converting to search",
            "evidence": f"{ig_followers:,} IG followers, GEO {geo_score}/10 — brand not findable via search.",
            "impact": "medium",
            "recommendation": "Claim Google Business, build brand page, target '[brand] alternatives' queries.",
        })

    return findings


# ── Synthesis LLM calls ───────────────────────────────────────────────────────

_ANALYST_BRIEF_PROMPT = """\
You are a McKinsey-grade D2C brand analyst. You have structured audit data from 8 agents plus cross-validated rule-based findings.

Write a sharp analyst brief. Output ONLY valid JSON, no preamble:
{
  "verdict": "one ruthless sentence on the brand's biggest problem right now",
  "top_findings": [
    {
      "title": "short finding title",
      "evidence": "specific number from the audit",
      "business_impact": "quantified impact if not fixed",
      "urgency": "immediate|this_month|this_quarter"
    }
  ],
  "hidden_opportunity": "one underrated growth lever not obvious from the metrics",
  "risk_if_ignored": "what happens to this brand in 6 months if nothing changes"
}
Rules: exactly 3 top_findings, highest→lowest urgency, every evidence line must cite a number."""

_ROADMAP_PROMPT = """\
You are a D2C ecommerce consultant creating a 30-day action roadmap.
Output ONLY valid JSON, no preamble:
{
  "week_1": [{"days": "Day 1-2", "task": "specific action", "effort": "2-4 hours", "impact": "+X% metric", "agent": "Store & CRO"}],
  "week_2_3": [{"days": "Day 8-10", "task": "...", "effort": "1 day", "impact": "+Y%", "agent": "GEO & AI Visibility"}],
  "week_4": [{"days": "Day 22-28", "task": "...", "effort": "3-5 days", "impact": "+Z%", "agent": "Content & Catalog"}]
}
Rules: week_1=3 tasks (quick wins), week_2_3=3 tasks (medium), week_4=2 tasks (foundation).
Every task must cite a specific finding. Impact = specific metric range. Agent = one of the 6 named agents."""

_ONE_THING_PROMPT = (
    "Senior ecommerce consultant. From this complete brand audit, identify THE single highest-impact "
    "action for the next 7 days. Must be specific (not 'improve SEO'), measurable (estimate impact), "
    "low effort (1-3 days), based on the worst score easiest to fix.\n"
    "Format: ONE sentence, max 25 words, action-first. Output only the sentence."
)


async def _synthesis(llm, results: dict, cross_findings: list[dict]) -> tuple[dict, str, dict]:
    """3 parallel LLM calls for analyst brief + one_thing + roadmap."""
    scores = {
        "geo":          _nested(results, "geo_visibility",  "analysis", "geo_score"),
        "mobile":       _nested(results, "store_cro",       "pagespeed", "mobile_score"),
        "pdp":          _nested(results, "content_catalog", "analysis", "pdp_quality_score"),
        "hook":         _nested(results, "performance_ads", "analysis", "hook_strength_score"),
        "cro":          _nested(results, "store_cro",       "analysis", "cro_score"),
        "ig_followers": _nested(results, "social_profile",  "instagram", "followers"),
    }
    brand_name = _nested(results, "brand_basics", "brand_name") or _nested(results, "brand_basics", "analysis", "brand_name")

    async def _brief():
        try:
            payload = {"scores": scores, "cross_agent_patterns": cross_findings[:3], "brand_name": brand_name}
            r = await llm.analyze_structured(
                system_prompt=_ANALYST_BRIEF_PROMPT,
                user_content=f"Audit:\n{json.dumps(payload, indent=2, default=str)}",
                max_tokens=800,
            )
            return r if "_parse_error" not in r else {}
        except Exception as exc:
            print(f"  [synthesis] analyst_brief skipped — {exc}", flush=True)
            return {}

    async def _one_thing():
        try:
            payload = {**scores,
                "top_cro_fix":    _nested(results, "store_cro",      "analysis", "top_5_cro_fixes"),
                "schema_missing": _nested(results, "geo_visibility",  "analysis", "schema_missing"),
            }
            t = await llm.analyze(
                system_prompt=_ONE_THING_PROMPT,
                user_content=f"Scores:\n{json.dumps(payload, indent=2, default=str)}",
                max_tokens=120, temperature=0.2,
            )
            return t.strip().strip("\"'")
        except Exception as exc:
            print(f"  [synthesis] one_thing skipped — {exc}", flush=True)
            return ""

    async def _roadmap():
        try:
            payload = {
                "scores": scores,
                "top_cro_fixes":  (_nested(results, "store_cro",      "analysis", "top_5_cro_fixes")    or [])[:3],
                "schema_missing": (_nested(results, "geo_visibility",  "analysis", "schema_missing")      or [])[:3],
                "pdp_weaknesses": (_nested(results, "content_catalog", "analysis", "pdp_weaknesses")      or [])[:3],
                "ad_quick_wins":  (_nested(results, "performance_ads", "analysis", "top_3_ad_quick_wins") or [])[:2],
                "strategic_recs": (_nested(results, "research",        "analysis", "strategic_recommendations") or [])[:2],
            }
            r = await llm.analyze_structured(
                system_prompt=_ROADMAP_PROMPT,
                user_content=f"Audit:\n{json.dumps(payload, indent=2, default=str)}",
                max_tokens=1400,
            )
            return r if "_parse_error" not in r else {}
        except Exception as exc:
            print(f"  [synthesis] roadmap skipped — {exc}", flush=True)
            return {}

    brief, one_thing, roadmap = await asyncio.gather(_brief(), _one_thing(), _roadmap())
    return brief, one_thing, roadmap


# ── Core pipeline ─────────────────────────────────────────────────────────────

async def _run_pipeline(
    url: str,
    brand_name: str,
    llm,
    agents: dict,
    deep_visual: bool = False,
    progress_cb=None,
) -> dict:
    """
    Phased pipeline: prefetch → brand_basics → parallel core → social depth → synthesis.

    No LLM calls between agents. Bounded retry per agent (max 2 attempts).
    Total LLM calls: 3 (analyst_brief + one_thing + roadmap).
    """
    overall_start = time.monotonic()
    results: dict = {}
    agent_status: list[dict] = []
    total = len(AGENT_SEQUENCE)
    wm = WorkingMemory(brand_name=brand_name, url=url)

    def _record(key: str, result: dict, elapsed: float, error: str | None) -> None:
        results[key] = result
        wm.add_finding(key, result)
        if error:
            wm.log(f"{key} FAILED in {elapsed:.1f}s: {error}")
        else:
            wm.log(f"{key} done in {elapsed:.1f}s")
        agent_status.append({
            "agent":    key,
            "label":    _AGENT_LABELS.get(key, key),
            "status":   "error" if error else "done",
            "elapsed_s": round(elapsed, 2),
            "error":    error,
            "data_coverage": result.get("data_coverage", "unknown"),
            "quality_ok": _is_useful(key, result),
        })

    # ── Phase 0: Prefetch ──────────────────────────────────────────────────────
    print("\n  [phase0] Prefetching homepage + pagespeed…", flush=True)
    if progress_cb:
        await progress_cb("__prefetch__", "running", 0)
    prefetch = await _phase0_prefetch(url, agents["__scraper__"])
    if progress_cb:
        await progress_cb("__prefetch__", "done", 10)

    # ── Phase 1: Brand foundation ──────────────────────────────────────────────
    print("  [phase1] Brand basics…", flush=True)
    if progress_cb:
        await progress_cb("brand_basics", "running", 10)
    t0 = time.monotonic()
    bb_result, bb_error = await _run_with_retry(
        "brand_basics", agents["brand_basics"], url, brand_name,
        progress_cb=progress_cb, progress_pct=10,
    )
    _record("brand_basics", bb_result, time.monotonic() - t0, bb_error)

    # Abort if brand URL is completely unreachable (no point running 7 more agents)
    if bb_error and bb_result.get("data_coverage") == "unavailable":
        homepage_check = prefetch["homepage"]
        if homepage_check.confidence == "unavailable":
            print("  [phase1] ABORT — brand URL unreachable, skipping all remaining agents.", flush=True)
            for key in AGENT_SEQUENCE:
                if key != "brand_basics":
                    results[key] = {
                        "agent": key, "status": "skipped",
                        "skip_reason": "brand URL unreachable",
                        "data_coverage": "skipped", "sources_used": [], "fallbacks_used": [],
                    }
            return _assemble(results, agent_status, url, brand_name, overall_start)

    # ── Phase 2: Core intelligence — fully parallel ───────────────────────────
    CORE_AGENTS = ["content_catalog", "performance_ads", "geo_visibility", "store_cro", "research", "social_profile"]
    print(f"\n  [phase2] Launching {len(CORE_AGENTS)} agents in parallel…", flush=True)
    if progress_cb:
        await progress_cb("__phase2__", "running", 20)

    base_pct = 20
    tasks = [
        _run_gated(
            key, agents[key], url, brand_name,
            deep_visual=False,
            progress_cb=progress_cb,
            progress_pct=base_pct + i * 10,
        )
        for i, key in enumerate(CORE_AGENTS)
    ]
    phase2_raw = await asyncio.gather(*tasks, return_exceptions=True)

    for item in phase2_raw:
        if isinstance(item, Exception):
            print(f"  [phase2] gather exception: {item}", flush=True)
            continue
        key, result, error, elapsed = item
        _record(key, result, elapsed, error)

    if progress_cb:
        await progress_cb("__phase2__", "done", 80)

    # ── Phase 3: Social depth ─────────────────────────────────────────────────
    # social_media_audit only runs if social_profile found actual presence
    ig_data = _nested(results, "social_profile", "instagram") or {}
    has_social = bool(ig_data.get("followers") or ig_data.get("bio"))

    if has_social:
        print("\n  [phase3] Social media deep audit…", flush=True)
        if progress_cb:
            await progress_cb("social_media_audit", "running", 82)
        t0 = time.monotonic()
        sma_result, sma_error = await _run_with_retry(
            "social_media_audit", agents["social_media_audit"], url, brand_name,
            deep_visual=deep_visual,
            progress_cb=progress_cb, progress_pct=82,
        )
        _record("social_media_audit", sma_result, time.monotonic() - t0, sma_error)
    else:
        print("  [phase3] Skipping social_media_audit — no social presence found.", flush=True)
        results["social_media_audit"] = {
            "agent": "social_media_audit", "status": "skipped",
            "skip_reason": "social_profile returned no IG data",
            "data_coverage": "skipped", "sources_used": [], "fallbacks_used": [],
        }

    # ── Phase 4: Synthesis ────────────────────────────────────────────────────
    print("\n  [phase4] Cross-validating findings (rule-based)…", flush=True)
    cross_findings = _cross_validate_findings(results)
    if cross_findings:
        print(f"  [phase4] {len(cross_findings)} cross-agent patterns detected", flush=True)
        for f in cross_findings:
            print(f"    [{f['impact'].upper()}] {f['pattern']}", flush=True)

    print("  [phase4] Generating synthesis (3 parallel LLM calls)…", flush=True)
    if progress_cb:
        await progress_cb("__synthesis__", "running", 90)
    analyst_brief, one_thing, roadmap = await _synthesis(llm, results, cross_findings)
    if progress_cb:
        await progress_cb("__synthesis__", "done", 100)

    results["_analyst"] = {
        "cross_findings": cross_findings,
        "brief":          analyst_brief,
    }

    wm.meta_synthesis = {
        "pattern": cross_findings[0].get("pattern") if cross_findings else None,
        "posture": "triage" if len([s for s in agent_status if s["status"] == "error"]) >= 3 else "optimize",
        "narrative": analyst_brief.get("executive_summary", "") if analyst_brief else "",
    }
    return _assemble(results, agent_status, url, brand_name, overall_start,
                     one_thing=one_thing, roadmap=roadmap,
                     analyst_brief=analyst_brief, cross_findings=cross_findings,
                     agentic_meta=wm.to_report_dict())


def _assemble(
    results: dict,
    agent_status: list[dict],
    url: str,
    brand_name: str,
    start_time: float,
    one_thing: str = "",
    roadmap: dict = None,
    analyst_brief: dict = None,
    cross_findings: list = None,
    agentic_meta: dict = None,
) -> dict:
    total_time = round(time.monotonic() - start_time, 2)
    failed = [
        s["agent"] for s in agent_status
        if s["status"] == "error"
    ]
    skipped = [
        k for k, r in results.items()
        if isinstance(r, dict) and r.get("status") == "skipped"
    ]
    coverage = "critical_failure" if len(failed) >= 4 else ("partial" if failed else "complete")

    print(f"\n  Pipeline complete in {total_time}s", flush=True)
    if failed:
        print(f"  Failed agents: {failed}", flush=True)
    if skipped:
        print(f"  Skipped agents: {skipped}", flush=True)

    am = agentic_meta or {}
    return {
        "url":               url,
        "brand_name":        brand_name,
        "timestamp":         datetime.now(timezone.utc).isoformat(),
        "total_time_seconds": total_time,
        "overall_coverage":  coverage,
        "failed_agents":     failed,
        "skipped_agents":    skipped,
        "agent_status":      agent_status,
        "one_thing":         one_thing,
        "roadmap":           roadmap or {},
        "analyst_brief":     analyst_brief or {},
        "cross_findings":    cross_findings or [],
        "results":           results,
        # WorkingMemory fields — consumed by _render_agentic_brain_section
        "agentic_meta":      am,
        "reasoning_trace":   am.get("reasoning_trace", []),
        "signals":           am.get("signals", []),
        "cross_insights":    am.get("cross_insights", []),
        "decisions":         am.get("decisions", []),
        "pattern_detected":  am.get("pattern_detected"),
        "strategic_posture": am.get("strategic_posture"),
    }


def _build_agents(llm, scraper: WebScraper, search: SearchAgent) -> dict:
    return {
        "__scraper__":     scraper,  # stored for phase0 access
        "brand_basics":       BrandBasicsAgent(llm, scraper, search),
        "content_catalog":    ContentCatalogAgent(llm, scraper, search),
        "performance_ads":    PerformanceAdsAgent(llm, scraper, search),
        "geo_visibility":     GEOVisibilityAgent(llm, scraper, search),
        "store_cro":          StoreCROAgent(llm, scraper, search),
        "research":           ResearchAgent(llm, scraper, search),
        "social_profile":     SocialProfileAgent(llm, scraper, search),
        "social_media_audit": SocialMediaAuditAgent(llm, search),
    }


# ── Public API ────────────────────────────────────────────────────────────────

async def run_full_audit(url: str, deep_visual: bool = False) -> dict:
    """Run a complete phased audit — no database, no side effects."""
    from agents.graph_orchestrator import run_graph
    return await run_graph(url, deep_visual=deep_visual)


async def run_all(audit_id: int, deep_visual: bool = False) -> None:
    """DB-backed audit — writes progress to AuditRun, compatible with FastAPI streaming."""
    from agents.graph_orchestrator import run_graph_with_db
    await run_graph_with_db(audit_id, deep_visual=deep_visual)
