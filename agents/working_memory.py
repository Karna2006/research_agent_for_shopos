"""Working memory for the agentic orchestrator.

Accumulates per-agent findings, cross-agent signals, and reasoning decisions
across the ReAct loop. Provides the compressed context fed into the brain LLM.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Literal

SignalType = Literal["opportunity", "risk", "anomaly", "confirmation"]
Severity = Literal["critical", "high", "medium", "low"]


@dataclass
class Signal:
    source: str           # which agent produced this
    type: SignalType
    severity: Severity
    content: str          # what was found (human-readable)
    evidence: str         # specific data point that supports this
    triggers_action: str | None = None  # "skip:agent_key" | "deepen:agent_key" | "flag:pattern"


@dataclass
class Decision:
    timestamp: float      # seconds since audit start
    step: str             # "initial_plan" | "observe:brand_basics" | "plan_next:3" | etc.
    rationale: str
    action_taken: str


# ── Per-agent finding compressors ──────────────────────────────────────────────
# Each extractor pulls only the decision-relevant fields so the brain LLM gets
# ~200 tokens of signal instead of 2000 tokens of raw JSON.

def _compress(agent_key: str, result: dict) -> dict:
    a = result.get("analysis") or {}
    status = result.get("status", "unknown")
    has_error = "error" in result or status == "failed"

    if agent_key == "brand_basics":
        return {
            "status": status,
            "has_error": has_error,
            "platform": result.get("platform"),
            "source_confidence": result.get("source_confidence"),
            "brand_stage": a.get("brand_stage") or a.get("brand_tier"),
            "founded_year": a.get("founded_year"),
            "price_tier": a.get("price_positioning") or a.get("price_tier"),
            "hero_products": (a.get("hero_products") or [])[:2],
        }
    if agent_key == "content_catalog":
        return {
            "status": status,
            "has_error": has_error,
            "pdp_quality_score": a.get("pdp_quality_score"),
            "homepage_score": a.get("homepage_score"),
            "total_products_found": a.get("total_products_found"),
            "pdp_weaknesses": (a.get("pdp_weaknesses") or [])[:2],
            "top_3_improvements": (a.get("top_3_improvements") or [])[:2],
        }
    if agent_key == "performance_ads":
        active = a.get("active_ads_count") or a.get("ads_running") or 0
        return {
            "status": status,
            "has_error": has_error,
            "hook_strength_score": a.get("hook_strength_score"),
            "active_ads_count": active,
            "no_ads_running": not bool(active),
            "ad_formats": (a.get("ad_formats") or [])[:3],
            "top_3_ad_quick_wins": (a.get("top_3_ad_quick_wins") or [])[:2],
        }
    if agent_key == "geo_visibility":
        return {
            "status": status,
            "has_error": has_error,
            "geo_score": a.get("geo_score"),
            "ai_search_readiness": a.get("ai_search_readiness"),
            "schema_missing": (a.get("schema_missing") or [])[:3],
            "critical_gaps": (a.get("critical_gaps") or [])[:2],
            "geo_improvement_roadmap": (a.get("geo_improvement_roadmap") or [])[:1],
        }
    if agent_key == "store_cro":
        psp = result.get("pagespeed") or {}
        mobile = psp.get("mobile_score")
        return {
            "status": status,
            "has_error": has_error,
            "mobile_score": mobile,
            "desktop_score": psp.get("desktop_score"),
            "cro_score": a.get("cro_score"),
            "mobile_critical": (mobile or 100) < 40,
            "mobile_poor": 40 <= (mobile or 100) < 60,
            "top_fix": (a.get("top_5_cro_fixes") or [None])[0],
        }
    if agent_key == "research":
        return {
            "status": status,
            "has_error": has_error,
            "research_score": a.get("research_score"),
            "main_competitors": (a.get("main_competitors") or [])[:3],
            "whitespace": result.get("whitespace"),
            "strategic_recs": (a.get("strategic_recommendations") or [])[:2],
        }
    if agent_key == "social_profile":
        platforms = result.get("platforms") or {}
        ig = platforms.get("instagram") or {}
        yt = platforms.get("youtube") or {}
        tw = platforms.get("twitter") or {}
        ig_f = ig.get("followers") or 0
        yt_s = yt.get("subscribers") or 0
        tw_f = tw.get("followers") or 0
        return {
            "status": status,
            "has_error": has_error,
            "instagram_followers": ig_f,
            "youtube_subscribers": yt_s,
            "twitter_followers": tw_f,
            "has_social_presence": bool(ig_f or yt_s or tw_f),
            "large_following": max(ig_f, yt_s, tw_f) > 100_000,
            "social_score": result.get("social_score"),
        }
    if agent_key == "social_media_audit":
        scores = result.get("scores") or {}
        return {
            "status": status,
            "has_error": has_error,
            "overall_social_score": scores.get("overall"),
            "engagement_score": scores.get("engagement"),
            "content_quality_score": scores.get("content_quality"),
            "tribe_available": result.get("tribe_available", False),
            "top_3_gaps": (result.get("top_3_gaps") or [])[:2],
        }

    # Fallback for unknown agents
    return {"status": status, "has_error": has_error}


# ── WorkingMemory ──────────────────────────────────────────────────────────────

class WorkingMemory:
    """Accumulates findings, signals, and reasoning trace across the agentic loop."""

    def __init__(self, brand_name: str, url: str) -> None:
        self.brand_name = brand_name
        self.url = url
        self.findings: dict[str, dict] = {}        # agent_key → compressed findings
        self.raw_results: dict[str, dict] = {}     # agent_key → full result (NOT sent to LLM)
        self.signals: list[Signal] = []
        self.decisions: list[Decision] = []
        self.trace: list[str] = []
        self.cross_insights: list[str] = []
        self.meta_synthesis: dict = {}
        self._t0 = time.monotonic()

    # ── Mutation ───────────────────────────────────────────────────────────────

    def add_finding(self, agent_key: str, result: dict) -> None:
        self.raw_results[agent_key] = result
        self.findings[agent_key] = _compress(agent_key, result)

    def add_signal(self, signal: Signal) -> None:
        self.signals.append(signal)

    def record_decision(self, step: str, rationale: str, action: str) -> None:
        self.decisions.append(Decision(
            timestamp=round(time.monotonic() - self._t0, 1),
            step=step,
            rationale=rationale,
            action_taken=action,
        ))

    def log(self, message: str) -> None:
        elapsed = round(time.monotonic() - self._t0, 1)
        self.trace.append(f"[{elapsed}s] {message}")

    def add_cross_insight(self, insight: str) -> None:
        self.cross_insights.append(insight)
        self.log(f"[cross-insight] {insight}")

    # ── Query ──────────────────────────────────────────────────────────────────

    @property
    def agents_completed(self) -> list[str]:
        return list(self.findings.keys())

    @property
    def failed_agents(self) -> list[str]:
        return [k for k, f in self.findings.items() if f.get("has_error")]

    def has_signal(self, triggers_action: str) -> bool:
        return any(s.triggers_action == triggers_action for s in self.signals)

    def critical_signals(self) -> list[Signal]:
        return [s for s in self.signals if s.severity == "critical"]

    def get_context_for_brain(self, max_chars: int = 3000) -> str:
        """Compressed context formatted for LLM consumption."""
        lines = [
            f"Brand: {self.brand_name}",
            f"URL: {self.url}",
            f"Agents completed: {', '.join(self.agents_completed) or 'none'}",
            "",
        ]
        for key, f in self.findings.items():
            lines.append(f"[{key}]: {json.dumps(f, default=str)}")

        if self.signals:
            lines.append("\nActive signals:")
            for s in self.signals[-8:]:  # cap at last 8 to stay under token budget
                lines.append(
                    f"  {s.severity.upper()} {s.type} ({s.source}): {s.content}"
                    + (f" → {s.triggers_action}" if s.triggers_action else "")
                )

        if self.cross_insights:
            lines.append("\nCross-agent insights:")
            for ci in self.cross_insights[-3:]:
                lines.append(f"  • {ci}")

        full = "\n".join(lines)
        return full[:max_chars]

    # ── Report export ──────────────────────────────────────────────────────────

    def to_report_dict(self) -> dict:
        """Summary included in the final audit JSON — powers the reasoning trace UI."""
        return {
            "reasoning_trace": self.trace,
            "signals": [
                {
                    "source": s.source,
                    "type": s.type,
                    "severity": s.severity,
                    "content": s.content,
                    "evidence": s.evidence,
                    "triggers_action": s.triggers_action,
                }
                for s in self.signals
            ],
            "cross_insights": self.cross_insights,
            "decisions": [
                {
                    "step": d.step,
                    "at_seconds": d.timestamp,
                    "rationale": d.rationale,
                    "action": d.action_taken,
                }
                for d in self.decisions
            ],
            "agents_skipped": [
                d.action_taken.replace("skip:", "")
                for d in self.decisions
                if d.action_taken.startswith("skip:")
            ],
            "pattern_detected": self.meta_synthesis.get("pattern"),
            "strategic_posture": self.meta_synthesis.get("posture"),
            "meta_narrative": self.meta_synthesis.get("narrative", ""),
        }
