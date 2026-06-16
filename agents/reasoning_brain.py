"""Reasoning brain — the LLM-driven planning engine for the agentic audit loop.

Implements four cognitive phases of the ReAct pattern:
  1. initial_plan   — strategic hypothesis before any agent runs
  2. observe        — extract decision-relevant signals from each completed agent
  3. plan_next      — decide next action (continue / reorder / skip / stop_early)
  4. cross_synthesize — find cross-agent patterns every 3 completed agents
  5. final_synthesis  — holistic meta-narrative after all agents complete

The brain is deliberately frugal with LLM calls:
  - observe() uses rule-based fast paths before making an LLM call
  - plan_next() only calls the LLM when skip/reorder conditions exist
  - All prompts are constrained to short JSON outputs (~300-600 tokens)
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agents.working_memory import Signal, WorkingMemory, _compress

if TYPE_CHECKING:
    from llm.client import GroqClient


# ── Prompts ────────────────────────────────────────────────────────────────────

_INITIAL_PLAN_PROMPT = """\
You are the strategic planning core of an AI ecommerce intelligence analyst.
Given a D2C brand URL and name, devise an optimized investigation strategy BEFORE any data is collected.

Available agents:
  brand_basics       — founding story, platform, pricing tier, brand stage (fast)
  content_catalog    — product content quality, PDP issues, catalog depth
  performance_ads    — Meta/Instagram ad intelligence, formats, hooks
  geo_visibility     — SEO health, schema markup, AI search readiness (fast)
  store_cro          — PageSpeed, mobile UX, conversion rate blockers
  research           — competitive landscape, whitespace, strategic positioning (slow)
  social_profile     — Instagram/Twitter/YouTube follower metrics
  social_media_audit — deep content analysis, visual quality, TRIBE v2 fMRI (slowest)

Rules:
- Flag 2-3 PRIORITY agents based on brand signals in the URL/name alone
- Predict 2-3 likely issues (be specific, cite URL signals if any)
- Identify skip conditions (what early finding would make an agent pointless)
- Choose investigation posture: growth_audit | crisis_triage | competitive_intel | foundation_check

Output ONLY valid JSON:
{
  "priority_agents": ["agent_key"],
  "predicted_issues": ["specific issue with reasoning"],
  "skip_conditions": [{"agent": "agent_key", "if": "condition description"}],
  "investigation_posture": "posture",
  "opening_hypothesis": "one sentence — most likely finding based on URL/name signals"
}"""

_OBSERVE_PROMPT = """\
You are an ecommerce signal extractor. An analysis agent just completed its run.
Extract 2-4 decision-relevant signals from the result below.

A good signal:
  - Changes strategy for remaining agents (triggers a skip, deepen, or flag)
  - Cites specific data (numbers, exact findings — not generic statements)
  - Types: opportunity | risk | anomaly | confirmation
  - Severity: critical (stops/redirects audit) | high | medium | low

Output ONLY valid JSON:
{
  "signals": [
    {
      "type": "risk|opportunity|anomaly|confirmation",
      "severity": "critical|high|medium|low",
      "content": "what was found — specific, citable, max 20 words",
      "evidence": "the exact metric or data point",
      "triggers_action": "skip:agent_key | deepen:agent_key | flag:pattern_name | null"
    }
  ],
  "compressed_insight": "one sentence — what this agent found (max 15 words)"
}"""

_PLAN_NEXT_PROMPT = """\
You are the decision engine of an AI audit system. Based on findings so far, decide the next action.

Decision options:
  continue   — run the next agent in queue as planned (DEFAULT — use this unless strong reason not to)
  reorder    — run a different queued agent next (cite which and why)
  skip       — skip a specific queued agent (only when it clearly adds no value)
  stop_early — abort the audit (ONLY if 3+ agents failed or site is unreachable)

Hard rules:
  - SKIP social_media_audit ONLY if social_profile found zero followers across all platforms
  - SKIP performance_ads only if brand is confirmed <6 months old AND no ads found in brand_basics
  - REORDER: run social_media_audit immediately after social_profile IF followers > 100K
  - STOP_EARLY only if 3+ agents returned hard errors (not just low scores)
  - When in doubt: CONTINUE

Output ONLY valid JSON:
{
  "decision": "continue|reorder|skip|stop_early",
  "target_agent": "agent_key_or_null",
  "rationale": "specific reason citing actual findings — max 25 words",
  "emerging_pattern": "null | invisible_brand | ghost_advertiser | social_darling | great_product_bad_store | ai_search_gap | conversion_crisis | hidden_gem"
}"""

_CROSS_SYNTHESIS_PROMPT = """\
You are a senior ecommerce strategist reviewing combined brand intelligence data.
Find the cross-cutting insight that no single agent could see alone.

Known patterns to look for:
  invisible_brand        — poor SEO + no social + no ads = completely undiscoverable
  ghost_advertiser       — running ads but terrible landing page = burning money
  social_darling         — 100K+ followers but poor store/CRO = losing converts at the door
  great_product_bad_store — strong brand/research signal but weak CRO + content
  ai_search_gap          — decent traditional SEO but missing schema for AI search engines
  conversion_crisis      — 3+ weak scores all converging on low conversion probability
  hidden_gem             — strong fundamentals but poor discoverability = quick wins available

Output ONLY valid JSON:
{
  "pattern": "pattern_name or null",
  "insight": "2-sentence cross-agent insight citing specific data from 2+ agents",
  "highest_leverage_action": "the single action addressing the root cause, not a symptom (max 20 words)"
}"""

_FINAL_SYNTHESIS_PROMPT = """\
You are the chief strategy officer synthesizing a complete brand intelligence audit.
Write the meta-narrative — this is NOT a summary of agents, it is a holistic strategic read.

Focus on:
  1. CORE CHALLENGE — the fundamental problem (one sentence, not a symptom)
  2. ROOT CAUSE — what structural issue drives multiple weak scores
  3. HIDDEN OPPORTUNITY — specific, evidence-backed, non-obvious growth lever
  4. CONTRADICTIONS — any findings that conflict with each other
  5. EXECUTIVE NARRATIVE — 3-4 sentence story of this brand's digital health

Output ONLY valid JSON:
{
  "core_challenge": "1 sentence — the fundamental problem",
  "root_cause": "1 sentence — structural driver behind multiple symptoms",
  "hidden_opportunity": "1 sentence — specific, non-obvious, evidence-backed",
  "contradictions": ["contradiction 1 if any"],
  "confidence": "high|medium|low",
  "posture": "triage|optimize|accelerate|defend",
  "pattern": "pattern_name or null",
  "narrative": "3-4 sentence executive narrative telling the brand's digital health story"
}"""


# ── Dataclasses ────────────────────────────────────────────────────────────────

@dataclass
class InitialPlan:
    priority_agents: list[str]
    predicted_issues: list[str]
    skip_conditions: list[dict]
    investigation_posture: str
    opening_hypothesis: str


@dataclass
class NextDecision:
    decision: str           # continue | reorder | skip | stop_early
    target_agent: str | None
    rationale: str
    emerging_pattern: str | None


@dataclass
class CrossInsight:
    pattern: str | None
    insight: str
    highest_leverage_action: str


@dataclass
class FinalSynthesis:
    core_challenge: str
    root_cause: str
    hidden_opportunity: str
    contradictions: list[str]
    confidence: str
    posture: str
    pattern: str | None
    narrative: str


# ── Brain ──────────────────────────────────────────────────────────────────────

class ReasoningBrain:
    """LLM-driven planner and observer for the agentic audit loop."""

    def __init__(self, llm: "GroqClient", brand_name: str, url: str) -> None:
        self.llm = llm
        self.brand_name = brand_name
        self.url = url

    # ── Phase 0: Initial plan ──────────────────────────────────────────────────

    async def initial_plan(self) -> InitialPlan:
        raw = await self.llm.analyze_structured(
            system_prompt=_INITIAL_PLAN_PROMPT,
            user_content=f"Brand: {self.brand_name}\nURL: {self.url}",
            max_tokens=500,
        )
        if "_parse_error" in raw:
            return _default_plan()
        return InitialPlan(
            priority_agents=raw.get("priority_agents") or [],
            predicted_issues=raw.get("predicted_issues") or [],
            skip_conditions=raw.get("skip_conditions") or [],
            investigation_posture=raw.get("investigation_posture") or "foundation_check",
            opening_hypothesis=raw.get("opening_hypothesis") or "",
        )

    # ── Phase 2: Observe ───────────────────────────────────────────────────────

    async def observe(self, agent_key: str, result: dict) -> list[Signal]:
        """Extract decision-relevant signals from a completed agent's result.

        Fast rule-based path fires first; only calls LLM when rules don't cover it.
        """
        fast = _rule_based_signals(agent_key, result)
        if fast:
            return fast

        compressed = _compress(agent_key, result)
        raw = await self.llm.analyze_structured(
            system_prompt=_OBSERVE_PROMPT,
            user_content=(
                f"Agent completed: {agent_key}\n"
                f"Result:\n{json.dumps(compressed, default=str)[:2000]}"
            ),
            max_tokens=450,
        )
        if "_parse_error" in raw or "signals" not in raw:
            return []

        signals: list[Signal] = []
        for s in (raw.get("signals") or [])[:4]:
            try:
                signals.append(Signal(
                    source=agent_key,
                    type=s.get("type", "risk"),
                    severity=s.get("severity", "medium"),
                    content=s.get("content", ""),
                    evidence=s.get("evidence", ""),
                    triggers_action=s.get("triggers_action") or None,
                ))
            except Exception:
                continue
        return signals

    # ── Phase 3: Plan next ─────────────────────────────────────────────────────

    async def plan_next(
        self, memory: WorkingMemory, remaining_queue: list[str]
    ) -> NextDecision:
        """Decide the next action. Skips LLM call when no signals suggest deviation."""
        # Fast-path: if no actionable signals, just continue
        actionable = [s for s in memory.signals if s.triggers_action]
        if not actionable and len(memory.failed_agents) < 3:
            return NextDecision(
                decision="continue",
                target_agent=None,
                rationale="No actionable signals — proceeding with standard sequence",
                emerging_pattern=None,
            )

        raw = await self.llm.analyze_structured(
            system_prompt=_PLAN_NEXT_PROMPT,
            user_content=(
                f"Current findings:\n{memory.get_context_for_brain()}\n\n"
                f"Remaining queue: {', '.join(remaining_queue)}\n"
                f"Failed agents: {', '.join(memory.failed_agents) or 'none'}"
            ),
            max_tokens=250,
        )
        if "_parse_error" in raw:
            return NextDecision(
                decision="continue",
                target_agent=None,
                rationale="Brain parse error — defaulting to continue",
                emerging_pattern=None,
            )
        return NextDecision(
            decision=raw.get("decision", "continue"),
            target_agent=raw.get("target_agent") or None,
            rationale=raw.get("rationale", ""),
            emerging_pattern=raw.get("emerging_pattern") or None,
        )

    # ── Phase 3b: Cross-synthesis ──────────────────────────────────────────────

    async def cross_synthesize(self, memory: WorkingMemory) -> CrossInsight:
        """Find cross-agent patterns — called every 3 completed agents."""
        raw = await self.llm.analyze_structured(
            system_prompt=_CROSS_SYNTHESIS_PROMPT,
            user_content=f"Combined findings:\n{memory.get_context_for_brain()}",
            max_tokens=350,
        )
        if "_parse_error" in raw:
            return CrossInsight(pattern=None, insight="Cross-synthesis unavailable.", highest_leverage_action="")
        return CrossInsight(
            pattern=raw.get("pattern") or None,
            insight=raw.get("insight", ""),
            highest_leverage_action=raw.get("highest_leverage_action", ""),
        )

    # ── Phase 4: Final synthesis ───────────────────────────────────────────────

    async def final_synthesis(self, memory: WorkingMemory) -> FinalSynthesis:
        """Generate the holistic meta-narrative across all completed agents."""
        pattern_list = [s.triggers_action for s in memory.signals if s.triggers_action]
        raw = await self.llm.analyze_structured(
            system_prompt=_FINAL_SYNTHESIS_PROMPT,
            user_content=(
                f"Complete audit data:\n{memory.get_context_for_brain()}\n\n"
                f"Total signals: {len(memory.signals)}\n"
                f"Critical signals: {len(memory.critical_signals())}\n"
                f"Patterns flagged: {pattern_list}"
            ),
            max_tokens=600,
        )
        if "_parse_error" in raw:
            return FinalSynthesis(
                core_challenge="Synthesis unavailable.",
                root_cause="",
                hidden_opportunity="",
                contradictions=[],
                confidence="low",
                posture="triage",
                pattern=None,
                narrative="Full synthesis unavailable due to parsing error.",
            )
        return FinalSynthesis(
            core_challenge=raw.get("core_challenge", ""),
            root_cause=raw.get("root_cause", ""),
            hidden_opportunity=raw.get("hidden_opportunity", ""),
            contradictions=raw.get("contradictions") or [],
            confidence=raw.get("confidence", "medium"),
            posture=raw.get("posture", "optimize"),
            pattern=raw.get("pattern") or None,
            narrative=raw.get("narrative", ""),
        )


# ── Rule-based fast signal extraction (no LLM) ────────────────────────────────

def _rule_based_signals(agent_key: str, result: dict) -> list[Signal]:
    """Deterministic signals for high-confidence, well-known conditions.

    These fire instantly and save LLM tokens for genuinely ambiguous cases.
    """
    signals: list[Signal] = []
    has_error = "error" in result or result.get("status") == "failed"

    if has_error:
        signals.append(Signal(
            source=agent_key,
            type="risk",
            severity="high",
            content=f"{agent_key} failed — this audit dimension has no data",
            evidence=f"status={result.get('status')}, error={str(result.get('error',''))[:80]}",
        ))
        return signals  # no point extracting more from a failed agent

    a = result.get("analysis") or {}

    # performance_ads: no ads running
    if agent_key == "performance_ads":
        active = a.get("active_ads_count") or a.get("ads_running") or 0
        if not active:
            signals.append(Signal(
                source=agent_key,
                type="anomaly",
                severity="medium",
                content="No active paid ads detected — brand relies entirely on organic channels",
                evidence="active_ads_count = 0",
            ))
            return signals

    # store_cro: mobile PageSpeed critical
    if agent_key == "store_cro":
        psp = result.get("pagespeed") or {}
        mobile = psp.get("mobile_score")
        if mobile is not None and mobile < 40:
            signals.append(Signal(
                source=agent_key,
                type="risk",
                severity="critical",
                content=f"Mobile PageSpeed critically low ({mobile}/100) — direct conversion loss",
                evidence=f"mobile_score = {mobile}",
                triggers_action="flag:conversion_crisis",
            ))
            return signals
        if mobile is not None and mobile < 60:
            signals.append(Signal(
                source=agent_key,
                type="risk",
                severity="high",
                content=f"Mobile PageSpeed poor ({mobile}/100) — below industry conversion threshold",
                evidence=f"mobile_score = {mobile}",
            ))
            return signals

    # geo_visibility: AI search invisibility
    if agent_key == "geo_visibility":
        geo = a.get("geo_score")
        if geo is not None and geo < 30:
            missing = (a.get("schema_missing") or [])[:2]
            signals.append(Signal(
                source=agent_key,
                type="risk",
                severity="critical",
                content=f"GEO score {geo}/100 — brand nearly invisible to AI search engines",
                evidence=f"geo_score={geo}, missing_schemas={missing}",
                triggers_action="flag:ai_search_gap",
            ))
            return signals

    # social_profile: no presence → skip social_media_audit
    if agent_key == "social_profile":
        platforms = result.get("platforms") or {}
        ig_f = (platforms.get("instagram") or {}).get("followers") or 0
        yt_s = (platforms.get("youtube") or {}).get("subscribers") or 0
        tw_f = (platforms.get("twitter") or {}).get("followers") or 0
        total = ig_f + yt_s + tw_f

        if total == 0:
            signals.append(Signal(
                source=agent_key,
                type="anomaly",
                severity="high",
                content="Zero detectable social presence — brand is socially invisible",
                evidence="followers=0 on Instagram, YouTube, Twitter",
                triggers_action="skip:social_media_audit",
            ))
            return signals

        if max(ig_f, yt_s, tw_f) > 100_000:
            top = max((ig_f, "Instagram"), (yt_s, "YouTube"), (tw_f, "Twitter"), key=lambda x: x[0])
            signals.append(Signal(
                source=agent_key,
                type="opportunity",
                severity="high",
                content=f"Strong social following ({top[0]:,} on {top[1]}) — prioritize deep content audit",
                evidence=f"instagram={ig_f}, youtube={yt_s}, twitter={tw_f}",
                triggers_action="deepen:social_media_audit",
            ))
            return signals

    # brand_basics: site unreachable
    if agent_key == "brand_basics":
        if result.get("data_coverage") == "unavailable" and result.get("source_confidence") == "low":
            signals.append(Signal(
                source=agent_key,
                type="risk",
                severity="critical",
                content="Brand homepage appears inaccessible — all downstream agents will be degraded",
                evidence=f"data_coverage=unavailable, source_confidence=low",
                triggers_action="flag:site_unreachable",
            ))
            return signals

    return []  # no fast signal — let LLM observe


# ── Fallback plan ──────────────────────────────────────────────────────────────

def _default_plan() -> InitialPlan:
    return InitialPlan(
        priority_agents=["brand_basics", "store_cro", "geo_visibility"],
        predicted_issues=["Unable to generate opening hypothesis — proceeding with standard audit"],
        skip_conditions=[],
        investigation_posture="foundation_check",
        opening_hypothesis="Standard D2C audit — hypothesis generation unavailable.",
    )
